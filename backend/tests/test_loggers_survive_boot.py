"""The application's loggers still work after the app has started (GRPH-525).

**Found while a log-assertion test passed on SQLite and failed on Postgres.** The warning was
emitted both times; only one run could see it. The cause was not the test:

`alembic/env.py` calls `fileConfig(config.config_file_name)`, and `disable_existing_loggers`
defaults to **True** — it sets `disabled = True` on every logger that already exists. On
Postgres, `run_migrations()` runs inside `lifespan` after the app's modules have been imported,
so ten of the twelve `graphban.*` loggers were silenced for the entire life of the process.

**Postgres is production.** SQLite installs use `create_all` and never reach this, which is why
every local run and every test suite looked healthy — the engine that mattered was the one
nobody could observe.

What it cost, concretely: PRD-25 S2b's background retry loop catches every exception and logs
`"credential retry pass failed; continuing"`, with a docstring arguing that "a persistent fault
produces a persistent log line instead of silence". That sentence was false in production. The
loop would have failed in exactly the silence its own error handling was written to prevent —
and an operator checking `docker logs` for retry errors would have found nothing and concluded
all was well.

This is the module's defect class exactly: **absence read as clean.** An empty log looks
identical whether nothing went wrong or nothing could be reported.

**ON SQLITE THIS FILE PASSES VACUOUSLY.** That engine uses `create_all` and never calls
`fileConfig`, so there is nothing here for it to catch and a green SQLite run says nothing
about this defect. Verified by reverting the fix: both tests fail on Postgres and both still
pass on SQLite. The Postgres job is the only one that covers it — which is the same reason
the original bug survived unnoticed.
"""
from __future__ import annotations

import logging

#: The loggers whose silence would hide something an operator needs. Not an exhaustive list of
#: every `graphban.*` name — a list that has to be updated whenever a module is added would rot.
#: These are the ones where "no output" is a claim about the system rather than about logging.
MUST_SPEAK = (
    "graphban.main",              # the background retry loop's failures
    "graphban.platform",          # a project falling back off its own credential (PRD-25 §4)
    "graphban.credential_retry",  # probe outcomes
    "graphban.mcp",               # tool errors
)


def test_the_apps_loggers_are_not_disabled_after_startup(client):
    """`client` runs the real lifespan, including `run_migrations()` on Postgres.

    Asserts `disabled` directly rather than by capturing output, because that is the exact
    attribute `fileConfig` sets and the one whose value is invisible in every other test.
    """
    silenced = [name for name in MUST_SPEAK if logging.getLogger(name).disabled]

    assert not silenced, (
        f"{silenced} are disabled after boot — anything they log is discarded, so an empty "
        "log file means nothing. Check `disable_existing_loggers` in alembic/env.py."
    )


def test_a_warning_actually_reaches_a_handler_after_startup(client):
    """The property the test above is a proxy for. `disabled` is the mechanism; this is the
    consequence, and asserting only the mechanism would miss any other way to lose the output.
    """
    seen: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    logger = logging.getLogger("graphban.platform")
    handler = Capture(level=logging.WARNING)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        logger.warning("canary")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert seen == ["canary"], (
        "a WARNING emitted after startup reached no handler — the app cannot report anything"
    )
