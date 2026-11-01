"""The production entrypoint, whose only job is to configure logging BEFORE uvicorn speaks.

`configure_logging()` runs in the app's lifespan, which is after uvicorn has already logged
"Started server process" and "Waiting for application startup." — so with LOG_JSON=true those
two lines went out as plain text on an otherwise-JSON stream. Measured: 2 non-JSON lines per
boot, and they are the two an operator most wants when a box is failing to start. An ingester
that drops malformed lines loses the only evidence the process came up at all.

`log_config=None` tells uvicorn not to call `dictConfig(LOGGING_CONFIG)`, so the handler
installed here stays authoritative from the first line rather than being layered over.

`configure_logging` still reparents uvicorn's loggers itself, and that is not redundant: the
`uvicorn` CLI is what docker-compose and every dev shell use, and there uvicorn's dictConfig
does run. This module makes the container's boot lines match; that loop makes the CLI's match.
"""
from __future__ import annotations

import logging
import os

import uvicorn

from app.config import settings
from app.observability import configure_logging


def effective_workers() -> int:
    """`WEB_CONCURRENCY`, defaulting to min(4, cpu_count); SQLite is always 1 because
    several writer processes on one file risk "database is locked" (GRPH-1241)."""
    if settings.is_sqlite:
        if settings.web_concurrency > 1:
            logging.getLogger("graphban.serve").warning(
                "WEB_CONCURRENCY=%d ignored: SQLite supports one worker", settings.web_concurrency)
        return 1
    if settings.web_concurrency > 0:
        return settings.web_concurrency
    return min(4, os.cpu_count() or 1)


def main() -> None:
    configure_logging()
    uvicorn.run(
        "app.main:app",
        # HARDCODED, and it must stay that way. `web/nginx.conf.template` sets `ipv6=off`
        # on its resolver *because* this bind is IPv4-only, and says in as many words that
        # whoever changes the bind changes that in the same commit. An env override here
        # would let the two drift with no commit to notice — an AAAA answer would point
        # nginx at an address nothing listens on.
        host="0.0.0.0",  # noqa: S104 — a container binds all interfaces  # nosec B104
        port=int(os.environ.get("PORT", "8000")),
        log_config=None,
        workers=effective_workers(),
    )


if __name__ == "__main__":
    main()
