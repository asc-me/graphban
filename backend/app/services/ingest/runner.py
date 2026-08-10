"""Run an adapter and turn its events into evidence (GRPH-304 / PRD-16).

Two properties this owes the promotion ladder above it, and both are load-bearing:

- **A re-run must not duplicate evidence.** The ladder counts corroborating shards to
  decide what is real, so duplicated evidence does not merely waste work — it manufactures
  corroboration, promoting a lesson that only ever happened once.
- **A bad record must not end the run.** A single truncated line at the tail of a live
  transcript is the normal state of a session in progress, not a corruption. An ingest that
  dies there is one nobody leaves switched on.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IngestWatermark
from app.services.ingest import Event, IngestAdapter

logger = logging.getLogger(__name__)

# Below this, a line is a fragment rather than a lesson — "ok", "yes", a bare path. Not a
# setting: we do not know the right value yet, and a slider is how you avoid finding out.
MIN_EVIDENCE_CHARS = 40


def ingest(db: Session, adapter: IngestAdapter, *, project_id: str = "core",
           limit_sources: int | None = None) -> dict:
    """Pull new events from every source the adapter can see, and record them as candidates.

    Returns counts rather than the shards themselves: a run over a real transcript set
    produces thousands, and a caller that wanted them would be holding the whole corpus in
    memory to look at a number.
    """
    from app.services import memory as mem_svc

    sources = adapter.discover()
    if limit_sources is not None:
        sources = sources[:limit_sources]

    stats = {"sources": 0, "events": 0, "recorded": 0, "skipped_sources": 0}
    for source in sources:
        mark = db.scalar(select(IngestWatermark).where(
            IngestWatermark.adapter == adapter.name, IngestWatermark.source == source))
        try:
            events, new_mark = adapter.parse(source, mark.watermark if mark else None)
        except Exception:  # noqa: BLE001 — one unreadable source must not end the run
            logger.warning("ingest: adapter failed on %s; skipping", source, exc_info=True)
            stats["skipped_sources"] += 1
            continue

        stats["sources"] += 1
        stats["events"] += len(events)
        for ev in events:
            if _record(db, mem_svc, ev, project_id):
                stats["recorded"] += 1

        # Advanced only after the events are written. A crash between the two re-reads
        # them, which duplicates work; advancing first would LOSE them, and a lesson that
        # was never recorded is invisible in a way a duplicate is not.
        if mark is None:
            mark = IngestWatermark(adapter=adapter.name, source=source)
            db.add(mark)
        mark.watermark = new_mark or ""
        mark.events_seen = (mark.events_seen or 0) + len(events)
        db.commit()
    return stats


def _record(db: Session, mem_svc, ev: Event, project_id: str) -> bool:
    """One event to a candidate shard, or nothing.

    Enters as `candidate`, never `published`: this is machine-mined evidence from a
    transcript, and PRD-16's non-goal is explicit that Graphban's existing triage path
    stays the sole owner of "is this worth keeping". Publishing here would run a second
    lifecycle beside the one that already exists.

    Text is NOT scrubbed here — `add_memory` does it on the write path, so every producer
    inherits it rather than each remembering to ask (GRPH-305).
    """
    text = (ev.text or "").strip()
    if len(text) < MIN_EVIDENCE_CHARS:
        return False
    try:
        mem_svc.add_memory(
            db, text_body=text, scope="global", project_id=project_id,
            source=f"transcript:{ev.harness}:{ev.session_id}",
            status="candidate", origin=f"ingest:{ev.harness}",
            # The ladder scores it later; scoring every line at write time would spend the
            # provider budget on material most of which is never promoted.
            auto_triage=False,
        )
        return True
    except Exception:  # noqa: BLE001 — one bad row must not end the run
        logger.warning("ingest: could not record an event from %s", ev.session_id,
                       exc_info=True)
        return False
