"""The thing that actually RUNS the learning loop (GRPH-353 / PRD-16).

PRD-16 shipped a complete engine and no driver. `ingest()`, `classify()` and
`draft_pending()` were invoked from the test suite and from nowhere else in the repo, so on
a running instance the loop had never executed and never would. Every step a **human**
performs was reachable through the API; every step the **machine** performs was not. That
was the whole defect, and this module is the whole fix.

**Two stages, not one, and the human boundary sits between them.** Ingest writes
`status="candidate"`; `artifacts.unclassified` reads `status="published"`. Running both as a
single pass would do nothing at all on the first night, because the artifact stage would
find nothing anyone had triaged yet. Two stages on one schedule is the correct shape and it
is self-correcting: the artifact stage picks up whatever ingest produced and a human
approved since the last run, however far apart those two events were.

**Neither stage may end on a bad record.** The pieces below already defend their own inner
loops — `ingest` skips an unreadable source, `classify` skips an unparseable batch, `draft`
swallows a failed model call — so what is left for the driver is the layer above: an adapter
whose `discover()` throws takes its own stats down and nothing else. A run that dies on the
first of four harnesses is a run nobody leaves switched on, which is how a self-improving
system quietly stops improving.

**Re-running is free.** The watermark advances only after events are written, classification
skips anything already classified, and drafting is keyed on a hash of the lesson text. A
second invocation with nothing new spends zero provider calls — deliberately, because a
scheduled job that bills for re-deriving yesterday's answers is a scheduled job somebody
turns off.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services import artifacts as art_svc
from app.services.ingest import IngestAdapter
from app.services.ingest.claude_code import ClaudeCodeAdapter
from app.services.ingest.runner import ingest

logger = logging.getLogger(__name__)

# The stages a caller may ask for, plus `all`. Named here rather than spelled out at each
# call site so the CLI, the route and the service cannot drift into offering different sets.
STAGES = ("ingest", "artifacts")


class UnknownStage(ValueError):
    """A stage nobody implements. Raised rather than silently running everything —
    `stage=artifact` (singular) must not quietly ingest 40k transcript lines."""


def default_adapters() -> list[IngestAdapter]:
    """Every harness this instance can read.

    A list rather than a chain of `if`s in the driver: a second harness is added here and
    nothing downstream changes. Built fresh on each run so a transcript root that appears
    between runs is picked up without restarting the process.
    """
    return [ClaudeCodeAdapter()]


def run_ingest(db: Session, *, project_id: str = "core",
               adapters: list[IngestAdapter] | None = None,
               limit_sources: int | None = None) -> dict:
    """Stage A: transcripts in, candidate shards out.

    Counts are summed across adapters and `failed_adapters` is reported separately rather
    than folded into `skipped_sources`. The two mean different things — one source was
    unreadable, versus an entire harness never ran — and a single number would let the
    second read as the first, which is a whole harness silently contributing nothing while
    the run reports success.
    """
    totals = {"adapters": 0, "sources": 0, "events": 0, "recorded": 0,
              "skipped_sources": 0, "failed_adapters": 0}
    for adapter in (default_adapters() if adapters is None else adapters):
        name = getattr(adapter, "name", adapter.__class__.__name__)
        try:
            stats = ingest(db, adapter, project_id=project_id,
                           limit_sources=limit_sources)
        except Exception:  # noqa: BLE001 — one broken harness must not end the run
            # Rollback for the same reason `_record` does it: a failed write leaves the
            # session poisoned, and every later adapter would then die with
            # PendingRollbackError while the stats reported a clean zero.
            db.rollback()
            logger.warning("learning: ingest adapter %s failed; skipping", name,
                           exc_info=True)
            totals["failed_adapters"] += 1
            continue
        totals["adapters"] += 1
        for key in ("sources", "events", "recorded", "skipped_sources"):
            totals[key] += stats.get(key, 0)
    return totals


def run_artifacts(db: Session, *, project_id: str | None = None) -> dict:
    """Stage B: published lessons in, drafted recommendations out.

    `drafted` and `reused` are reported apart on purpose. A run where everything was reused
    made no model calls, and that is the steady state this stage is supposed to reach — a
    single "10 recommendations" figure would look identical whether it had cost nothing or
    cost ten drafts, and the whole reason re-running is safe is that the difference is real.
    """
    created = art_svc.classify(db, project_id)
    # Captured BEFORE drafting: `draft` mutates the row in place, so a hash read afterwards
    # is the new one and every recommendation would count as re-rendered.
    before = {r.id: r.draft_hash for r in art_svc.pending(db, project_id)}
    rows = art_svc.draft_pending(db, project_id)
    drafted = sum(1 for r in rows if r.draft_hash and r.draft_hash != before.get(r.id))
    return {"classified": len(created), "queued": len(rows),
            "drafted": drafted, "reused": len(rows) - drafted}


def run(db: Session, *, stage: str = "all", project_id: str = "core",
        adapters: list[IngestAdapter] | None = None,
        limit_sources: int | None = None) -> dict:
    """One entry point, shared by the CLI and the HTTP route.

    Shared deliberately: PRD-16 asks that the loop be driven "through the same service layer
    the web app calls", and two drivers is how a `graphban learn run` and a cron-hit endpoint
    end up doing measurably different things a year apart.
    """
    if stage not in STAGES + ("all",):
        raise UnknownStage(f"unknown stage {stage!r} — expected one of "
                           f"{', '.join(STAGES + ('all',))}")
    out: dict = {"stage": stage, "project_id": project_id}
    if stage in ("ingest", "all"):
        out["ingest"] = run_ingest(db, project_id=project_id, adapters=adapters,
                                   limit_sources=limit_sources)
    if stage in ("artifacts", "all"):
        out["artifacts"] = run_artifacts(db, project_id=project_id)
    return out
