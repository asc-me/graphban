"""Per-call LLM spans (GRPH-225): one row per provider call.

**The store decision this module records** (the item is high-fidelity — the decision
came before the build): a SQL table, not OpenTelemetry. The reasons are the same ones
that made `Event` the ledger and `OrgUsage`/`McpToolStat` the counters:

- The consumers are SQL. The analytics PRD charts from tables and refuses to smooth
  over absences; a cost panel is a `GROUP BY`, not a trace explorer.
- OTel would add a dependency and a collector neither deployment has. But the export
  half of OTel's value is already here: every span also emits one structured log line
  (`extra={"llm": ...}`), which `LOG_JSON=true` ships to the log platform verbatim —
  the GRPH-33 pattern, one more field set.
- `Event` is the audit ledger — one row per accepted MUTATION, written at the boundary
  with actor identity. A span is per-provider-call telemetry with a volume two orders of
  magnitude higher and its own retention policy. Mixing them corrupts both.

The invariants, each of them a test:

- **Telemetry must not break the feature.** A span write that raises is logged and
  swallowed; the chat answer still returns. The provider call's outcome is the user's;
  the span is ours.
- **Absence must read as absence.** An unpriced model's `cost_usd` is NULL — not 0 —
  because a 0 on the cost panel is a claim about money. Tokens the provider did not
  report are estimated from text length and stamped `tokens_source="estimated"`; the
  stub's known-zero spend is `"none"` with cost 0.0. (This repo's recurring defect is
  absence reading as clean; a fabricated 0 is that defect wearing a decimal point.)
- **One span per provider call.** Extractors construct their chat adapters *inside* the
  build (not through `build_chat`), and `chat()` on the compat path is assembled from
  `stream()`; a `_inside` guard makes nested wrapped calls silent, so instrumentation
  added at the construction chokepoint cannot double-count.

Usage metadata arrives by contextvar handshake: the adapters know the exact token
counts (the HTTP response is theirs; the protocol returns a bare `str`), so each adapter
calls `record_usage()` at the point it already parses the response. The wrapper opens a
sink around every protocol call and drains it on completion. Providers that never
report simply never fill it.
"""
from __future__ import annotations

import contextvars
import functools
import logging
import time
from contextlib import contextmanager
from typing import Any, Callable

from app.config import settings
from app.observability import request_id_var

logger = logging.getLogger("graphban.llm")

#: What is asking (tagged by the call site: "mcp:create_item", "assistant",
#: "lessons.extract"). Empty string means the call site never tagged — stored as ""
#: and chartable as "untagged", never silently attributed to some default feature.
feature_var: contextvars.ContextVar[str] = contextvars.ContextVar("llm_feature", default="")
project_var: contextvars.ContextVar[str] = contextvars.ContextVar("llm_project", default="")

#: The sink of the span currently open in this context, filled by adapters via
#: record_usage(). None means "no metered call is in progress" (the normal state for
#: tool sessions' AL-179 thread metering, which keeps accumulating exactly as before).
_sink_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("llm_usage_sink", default=None)

#: Re-entrancy guard: while a wrapped call is in flight, nested wrapped calls pass
#: through unmetered (see the one-span invariant in the module docstring).
_inside_var: contextvars.ContextVar[bool] = contextvars.ContextVar("llm_inside", default=False)


def record_usage(**tokens: int | None) -> None:
    """Adapter-side hook: report the exact token counts of the call being made now.

    Keys: input, output, cache_read, cache_write. A no-op outside a metered call — this
    is deliberately not an error, because adapters cannot know whether the object was
    wrapped (a bare OpenAICompatChat built directly in a test is equally legal).
    """
    sink = _sink_var.get()
    if sink is not None:
        for k, v in tokens.items():
            if v is not None:
                sink[k] = int(v)


@contextmanager
def llm_context(*, feature: str = "", project_id: str = ""):
    """Tag every LLM call made inside this block. Call sites only; the wrapper reads."""
    tokens: list[tuple[contextvars.ContextVar[str], Any]] = []
    if feature:
        tokens.append((feature_var, feature_var.set(feature)))
    if project_id:
        tokens.append((project_var, project_var.set(project_id)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


# ----------------------------------------------------------------------------- cost
#: Public list prices per MILLION tokens, (input, output), matched by longest model
#: prefix. These are ATTRIBUTION ESTIMATES, not invoices: they ignore batching
#: discounts, cached-input pricing beyond the cache columns we carry, and every
#: negotiation. The point is the shape of spend — which feature and which tenant moves
#: the number — and that survives being roughly right. A model matching no prefix gets
#: NULL, which is the honest "we cannot price this", not a guess.
PRICES: list[tuple[tuple[str, ...], float, float]] = [
    (("claude-opus",), 15.0, 75.0),
    (("claude-sonnet",), 3.0, 15.0),
    (("claude-haiku",), 1.0, 5.0),
    (("gpt-4.1", "gpt-4o"), 2.5, 10.0),
    (("gpt-5",), 1.25, 10.0),
    (("o1", "o3", "o4"), 15.0, 60.0),
    (("deepseek-reasoner", "deepseek-chat", "deepseek"), 0.27, 1.10),
    (("qwen3", "qwen-plus", "qwen-max", "qwen-turbo", "qwen"), 0.8, 2.0),
    (("llama-4", "llama-3"), 0.6, 1.8),
    (("gemini",), 1.25, 5.0),
    (("kimi", "moonshot"), 0.6, 2.5),
    (("glm",), 0.6, 2.0),
    (("minimax",), 1.0, 8.0),
    (("mistral", "magistral"), 0.4, 2.0),
    (("grok",), 2.0, 5.0),
    (("sonar",), 1.0, 1.0),
    (("command",), 2.5, 10.0),
    (("mixtral",), 0.4, 2.0),
]

#: Providers where the compute is on hardware we already pay for. Their cost is a real
#: zero — unlike "unpriced", which is an unknown.
LOCAL_PROVIDERS = frozenset({"stub", "ollama"})


def estimate_cost(provider: str, model: str, input_tokens: int | None,
                  output_tokens: int | None) -> float | None:
    if provider in LOCAL_PROVIDERS:
        return 0.0
    if not model:
        return None
    low = model.lower()
    best: tuple[str, float, float] | None = None
    for prefixes, cin, cout in PRICES:
        for p in prefixes:
            if low.startswith(p) and (best is None or len(p) > len(best[0])):
                best = (p, cin, cout)
    if best is None:
        return None
    _, cin, cout = best
    return round(((input_tokens or 0) * cin + (output_tokens or 0) * cout) / 1_000_000, 6)


def _est_tokens(text: str) -> int:
    """The chars/4 heuristic, and nothing fancier. Flagged `estimated` on the row: it
    exists so a provider that reports nothing still has an order of magnitude to chart,
    not to bill against."""
    return max(0, len(text) // 4) if text else 0


def _in_text(args: tuple, kwargs: dict) -> str:
    """The prompt text, for the estimated path only."""
    parts = [str(kwargs.get(k, "")) for k in ("system", "context", "question", "title", "description")]
    for a in args:
        if isinstance(a, str):
            parts.append(a)
        elif isinstance(a, list):
            parts.extend(str(x) for x in a if isinstance(x, str))
    return " ".join(p for p in parts if p)


def _out_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return " ".join(str(x) for x in result)
    return ""


# ----------------------------------------------------------------------------- emit
def _emit(meta: dict, kind: str, sink: dict, started: float, *,
          error: BaseException | None, in_text: str, out_text: str,
          result: Any = None) -> None:
    """One span, one log line. NEVER raises: this runs on the success path AND inside
    except blocks of the user's own call."""
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    if result is not None and getattr(result, "usage", None):
        for k, v in result.usage.items():  # tool turns carry their usage on the result
            sink.setdefault(k, v)

    reported = bool(sink)
    if reported:
        input_tokens = sink.get("input")
        output_tokens = sink.get("output")
        source = "reported"
    elif meta["provider"] in LOCAL_PROVIDERS:
        # The stub and local ollama either report (ollama sends eval_count) or are
        # provably zero-cost; stamping an estimate on a zero row is noise.
        input_tokens = output_tokens = None
        source = "none"
    else:
        input_tokens = _est_tokens(in_text)
        output_tokens = _est_tokens(out_text)
        source = "estimated"

    cost = estimate_cost(meta["provider"], meta["model"], input_tokens, output_tokens)

    row = {
        "provider": meta["provider"],
        "model": meta["model"],
        "base_url": meta["base_url"],
        "kind": kind,
        "feature": meta.get("feature") or feature_var.get(),
        "project_id": meta.get("project") or project_var.get(),
        "request_id": request_id_var.get(),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": sink.get("cache_read"),
        "cache_write_tokens": sink.get("cache_write"),
        "tokens_source": source,
        "latency_ms": latency_ms,
        "cost_usd": cost,
        "ok": error is None,
        "error_class": type(error).__name__ if error else "",
        "http_status": _status(error),
        "retryable": getattr(error, "retryable", None) if error else None,
    }
    try:
        logger.info("llm %s %s %s %s %.1fms", row["provider"], row["model"], kind,
                    "ok" if row["ok"] else f"ERR {row['error_class']}", latency_ms,
                    extra={"llm": {k: v for k, v in row.items() if v is not None}})
    except Exception:  # noqa: BLE001 — logging must not break the feature either
        pass
    try:
        from app.db import SessionLocal
        from app.models import LlmCallSpan

        db = SessionLocal()
        try:
            db.add(LlmCallSpan(**row))
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — THE invariant: telemetry never breaks the feature
        logger.warning("llm span write failed (the call itself was unaffected)", exc_info=True)


def _status(error: BaseException | None) -> int | None:
    if error is None:
        return None
    st = getattr(error, "status", None)  # provider_errors carries the failover verdict
    if st is None:
        resp = getattr(error, "response", None)  # raw httpx failures carry the response
        st = getattr(resp, "status_code", None)
    return int(st) if st is not None else None


# ---------------------------------------------------------------------------- wrap
def metered(obj: Any, *, provider: str, model: str = "", base_url: str = "",
            project_id: str = "") -> Any:
    """Attach span timing to a constructed adapter, in place. Returns the same object.

    Applied at the construction chokepoints (`build_chat` / `build_extractor` /
    `build_embedder` / the env-path getters), so every protocol call through the public
    surface is metered without touching any call site. Objects that do not carry a
    protocol method are returned untouched — the wrapper meters what the object can do.

    `project_id` binds attribution at CONSTRUCTION, which is where the resolvers
    (`platform_svc.resolve_chat` and friends) already know it. A contextvar would leak
    across a request that resolves one project and then writes for another; an
    instance-bound project cannot misattribute, and a deployment-scoped embedder simply
    carries "" — unknown project, its own chartable bucket.
    """
    meta = {
        "provider": provider or "stub",
        "model": model or getattr(obj, "model", "") or "",
        "base_url": base_url or getattr(obj, "base_url", "") or "",
        "project": project_id or "",
    }
    for name, kind, gen in (
        ("chat", "chat", False),
        ("extract", "extract", False),
        ("embed", "embed", False),
        ("embed_many", "embed", False),
        ("stream", "chat", True),
    ):
        fn = getattr(obj, name, None)
        if callable(fn):
            setattr(obj, name, _wrap(fn, kind, meta, gen))
    ts = getattr(obj, "tool_session", None)
    if callable(ts):
        setattr(obj, "tool_session", _wrap_session_factory(ts, meta))
    return obj


def _wrap(fn: Callable, kind: str, meta: dict, is_gen: bool) -> Callable:
    @functools.wraps(fn)
    def outer(*args, **kwargs):
        if _inside_var.get():
            return fn(*args, **kwargs)  # nested call: the outer span owns it
        if is_gen:
            return _drive_gen(fn, args, kwargs, kind, meta)
        sink: dict = {}
        tok_sink = _sink_var.set(sink)
        tok_in = _inside_var.set(True)
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:
            _inside_var.reset(tok_in); _sink_var.reset(tok_sink)
            _emit(meta, kind, sink, t0, error=e,
                  in_text=_in_text(args, kwargs), out_text="")
            raise
        _inside_var.reset(tok_in); _sink_var.reset(tok_sink)
        _emit(meta, kind, sink, t0, error=None,
              in_text=_in_text(args, kwargs), out_text=_out_text(result), result=result)
        return result
    return outer


def _drive_gen(fn: Callable, args: tuple, kwargs: dict, kind: str, meta: dict):
    """Generator driver for `stream`/`stream_turn`: yields through, and on the
    producer's `return` (StopIteration.value — a ToolTurn for stream_turn) emits the
    span while PRESERVING the return value, because `iter_reply` and the assistant
    loop read it."""
    sink: dict = {}
    tok_sink = _sink_var.set(sink)
    tok_in = _inside_var.set(True)
    t0 = time.perf_counter()
    parts: list[str] = []
    it = fn(*args, **kwargs)
    try:
        while True:
            try:
                chunk = next(it)
            except StopIteration as stop:
                _inside_var.reset(tok_in); _sink_var.reset(tok_sink)
                _emit(meta, kind, sink, t0, error=None, in_text=_in_text(args, kwargs),
                      out_text="".join(parts), result=stop.value)
                return stop.value
            if isinstance(chunk, str):
                parts.append(chunk)
            yield chunk
    except BaseException as e:
        _inside_var.reset(tok_in); _sink_var.reset(tok_sink)
        _emit(meta, kind, sink, t0, error=e, in_text=_in_text(args, kwargs),
              out_text="".join(parts))
        raise


def _wrap_session_factory(factory: Callable, meta: dict) -> Callable:
    @functools.wraps(factory)
    def outer(*args, **kwargs):
        session = factory(*args, **kwargs)
        # Bind feature (and a context project) at CREATION, not per-turn: sessions are
        # created eagerly in the request scope (assistant route: "resolve while the DB
        # session is open") but their turns run later, from the streaming generator's
        # thread, where the creation-time `with llm_context(...)` has long exited.
        # The session IS the feature — a tool conversation is one attribution.
        bound = dict(meta)
        if not bound.get("project"):
            bound["project"] = project_var.get()
        bound["feature"] = feature_var.get() or bound.get("feature", "")
        for name, gen in (("run_turn", False), ("stream_turn", True)):
            fn = getattr(session, name, None)
            if callable(fn):
                setattr(session, name, _wrap(fn, "tool_turn", bound, gen))
        return session
    return outer


# -------------------------------------------------------------------------- housekeeping
def purge_expired() -> int:
    """Delete spans older than the retention window. 0 or negative days keeps
    everything (self-host boxes with no dashboard pressure). Called from lifespan;
    failures here are loud in the log but never fatal — startup must not die because
    janitorial work on telemetry couldn't run."""
    days = getattr(settings, "llm_span_retention_days", 90)
    if days <= 0:
        return 0
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    from app.db import SessionLocal
    from app.models import LlmCallSpan

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    db = SessionLocal()
    try:
        result = db.execute(delete(LlmCallSpan).where(LlmCallSpan.ts < cutoff))
        db.commit()
        n = result.rowcount or 0
        if n:
            logger.info("llm span retention: deleted %d rows older than %d days", n, days)
        return n
    finally:
        db.close()
