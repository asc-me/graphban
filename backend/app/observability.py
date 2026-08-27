"""Observability floor (Phase 5, AL-56): structured logging + request correlation.

- Every request gets an id (an inbound ``X-Request-ID`` is honored, else one is
  generated), stashed in a contextvar, echoed on the response, and stamped on every
  log line emitted while handling it — so logs across the API and MCP dispatcher for
  one request line up.
- Every request is logged once, on the way out, with method, path, status and duration.
  Railway's log search is the only place an operator can ask "what did this box serve?",
  and before GRPH-33 the answer was nothing at all: `configure_logging` silenced
  `uvicorn.access` on the grounds that it "duplicates what we already see", and nothing
  else emitted a request line. Measured: three requests including a 404 produced zero log
  lines. The comment is true NOW, because `_log_access` below is the thing that sees it.
- Logging is human-readable text by default; ``LOG_JSON=true`` switches to one JSON
  object per line for ingestion by a log platform. ``LOG_LEVEL`` sets the threshold.

The middleware is pure ASGI (not BaseHTTPMiddleware) so it never buffers the
response body — the SSE streaming endpoints keep streaming.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid

from app.config import settings

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        # Structured fields ride on the record rather than inside the message. A log
        # platform can filter on `http.status`; it cannot filter on a sentence.
        extra = getattr(record, "http", None)
        if extra:
            payload["http"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    """Install the app's root log handler. Idempotent — safe to call on each boot."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(_JsonFormatter() if settings.log_json else logging.Formatter(_TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # TAKE UVICORN'S LOGGERS OFF UVICORN'S HANDLERS, or half the stream stays plain text.
    # uvicorn calls `dictConfig(LOGGING_CONFIG)` when the server boots, which is BEFORE the
    # app's lifespan calls this function. That config puts a plain-text handler on the
    # parent `uvicorn` logger with `propagate=False`, so every uvicorn line stops there and
    # never reaches the root handler installed above. Setting only root left LOG_JSON=true
    # emitting app lines as JSON and uvicorn's own lines — including every startup and
    # shutdown banner — as `INFO:     ...`. One stream, two formats, which is worse for a
    # log platform than either alone (GRPH-33).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # NOW the old claim holds: uvicorn's access line really would duplicate `_log_access`.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


access_logger = logging.getLogger("graphban.access")


def _log_access(scope, status: int, elapsed_ms: float) -> None:
    """One line per request: what was asked, what came back, how long it took.

    **THE PATH ONLY — NEVER THE QUERY STRING.** This is a security boundary, not a
    formatting preference. The public share link carries its unguessable token as a query
    parameter (`/api/public/roadmap?token=...`, see `routers/public.py::_public_project`),
    and that token IS the credential: holding it is what distinguishes a reader from the
    404 everyone else gets. Logging `scope["query_string"]` would copy live credentials
    into Railway's log store, where they are searchable by anyone with log access, outlive
    the request by the retention window, and are invisible to every revocation path the app
    has. `raw_path` is not used for the same reason.

    NO CLIENT IP, deliberately. Which forwarded hop is the real caller is an open question
    (GRPH-517) — `client_ip` reads the leftmost, which is the one an attacker writes — so a
    field named `ip` here would be authoritative-looking and sometimes attacker-chosen.
    Adding it belongs with the answer, not before it.

    Emitted for EVERY request including health checks. Suppressing the noisy ones is what
    produced the original hole: `uvicorn.access` was silenced to cut volume and took all
    request visibility with it. Volume belongs to LOG_LEVEL, which an operator can turn
    down knowing what they lose. A health check that stops arriving is also signal.
    """
    access_logger.info(
        "%s %s %s %.1fms",
        scope.get("method", "-"), scope.get("path", "-"), status, elapsed_ms,
        extra={"http": {
            "method": scope.get("method", "-"),
            "path": scope.get("path", "-"),
            "status": status,
            "duration_ms": round(elapsed_ms, 1),
        }},
    )


class RequestIdMiddleware:
    """ASGI middleware: assign/propagate a request id, echo it, and log the exchange."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id")
        rid = incoming.decode() if incoming else uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        # 500 rather than 0, because the one case where no response start is ever sent is
        # an exception propagating out of the app. Recording that as `0` would invent a
        # status nothing served; recording it as 500 says what the client actually got.
        seen = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # BEFORE the contextvar is reset, so `_RequestIdFilter` stamps this line with
            # the same id as everything else the request emitted. After the reset it would
            # log `-` and be the one line you cannot correlate.
            _log_access(scope, seen["status"], (time.perf_counter() - started) * 1000)
            request_id_var.reset(token)
