"""What the supervisor says about each child, and where it says it.

PRD-22 S6. Structured lines on stdout and a rotating file in the state dir — **never a
telemetry endpoint**, which would put a network dependency in the component whose job is
to keep working when the network is gone (D-i).

Per child: adapter and resolved binary version, seat id (never the code), worktree path,
branch, pid, registration latency, exit code, and reap disposition.

**Registration latency is the load-bearing field.** A child that never registers is a
process that runs, burns money and produces nothing, while the roster simply shows one
agent fewer than expected. Without a number, that is indistinguishable from a slow start
— so `None` is rendered as `never_registered` rather than omitted or zeroed. A missing
field reads as "nothing to report"; zero reads as "instant". Neither is what happened.

`reap` is the same shape: `left_dirty` has to be visible as a distinct outcome, or disk
fills while every line reads fine.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .state import state_root

LOGGER = "gbfleet"
LOG_FILE = "gbfleet.log"
#: Small enough that a laptop never notices, large enough to hold a few waves.
MAX_BYTES = 2_000_000
BACKUPS = 3

#: Rendered for a child that never got far enough to register. Not omitted and not zero:
#: an absent field reads as "nothing to report" and a zero reads as "instant", and the
#: one thing this field exists to say is that neither happened.
NEVER_REGISTERED = "never_registered"


@dataclass
class ChildRecord:
    """One child's life, as one line.

    `seat_id` is the enrolment's row id. **Never the code** — the code is single-use and
    short-lived, but a log file is neither, and a credential in a rotating file on disk
    outlives every protection the seat design gives it.
    """

    adapter: str
    binary_version: str
    worktree: str
    branch: str
    pid: int
    seat_id: str | None = None
    agent_id: str | None = None
    registration_latency: float | str = NEVER_REGISTERED
    exit_code: int | None = None
    #: Vendor words for `exit_code` (`finished`, `stopped by signal (...)`, …).
    #: The raw Windows CTRL_BREAK status is 3221225786; recording only that number
    #: makes every clean stop look like a crash (GRPH-588 bounce).
    exit_meaning: str | None = None
    stopped_because: str | None = None
    reap: str | None = None
    salvage_commit: str | None = None
    #: Seat files found in the branch's history. Loud on purpose: salvage cannot undo a
    #: credential the worker committed itself, so the only useful thing left is to say so.
    credential_in_history: list[str] = field(default_factory=list)
    #: Files this worker actually changed (S5 / P30 D10). Measured off its branch. The
    #: supervisor reports this and does not patch the item; a writer with standing unions
    #: a non-empty list onto `touchpoints`. Empty here is "changed nothing", not "safe to
    #: parallelise", and is not a write.
    touched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "gbfleet", None)
        if payload is None:
            return json.dumps({"event": record.getMessage()})
        return json.dumps({"event": record.getMessage(), **payload}, default=str)


def configure(state: Path | None = None, stream=None) -> logging.Logger:
    """One logger, writing JSON lines to stdout and to a rotating file.

    Idempotent: called once per `up`, and calling it twice must not double every line.
    `RotatingFileHandler` is stdlib — reaching for it rather than writing rotation is
    the whole of the reasoning, and rotation matters here because the supervisor is the
    one component expected to run unattended for a long time.
    """
    logger = logging.getLogger(LOGGER)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = _JsonLines()

    console = logging.StreamHandler(stream or sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    directory = Path(state) if state else state_root()
    directory.mkdir(parents=True, exist_ok=True)
    rotating = logging.handlers.RotatingFileHandler(
        directory / LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
    )
    rotating.setFormatter(formatter)
    logger.addHandler(rotating)
    return logger


def emit(event: str, **fields: Any) -> None:
    logging.getLogger(LOGGER).info(event, extra={"gbfleet": fields})


def child(record: ChildRecord) -> None:
    emit("child", **record.as_dict())


def pulse(child_key: str, adapter: str, pid: int, **fields: Any) -> None:
    """One live reading of a child mid-run, as opposed to `child()`'s post-mortem.

    Emitted every poll under `--debug` only. Always measuring but not always printing is
    deliberate: the measurement is one `stat` per file and is what lets the wave summary
    name a child that went quiet, while printing it every second for an hour would bury
    the lines that matter in the same file.
    """
    emit("pulse", child=child_key, adapter=adapter, pid=pid, **fields)


def debug_gap(adapter: str, reason: str) -> None:
    """Say that `--debug` bought less than the operator asked for.

    `cursor-agent` and `gbagent` have no debug flag, so a child on either gets output
    sampling and nothing more. Announcing that is the whole point: partial coverage that
    looks like full coverage is how somebody concludes a silent child is fine.
    """
    emit("debug_unavailable", adapter=adapter, reason=reason)
