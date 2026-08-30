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
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IngestWatermark, Project
from app.services.ingest import Event, IngestAdapter

logger = logging.getLogger(__name__)

# Below this, a line is a fragment rather than a lesson — "ok", "yes", a bare path. Not a
# setting: we do not know the right value yet, and a slider is how you avoid finding out.
MIN_EVIDENCE_CHARS = 40

# Prose has to clear a far higher bar than a failure does, and the asymmetry is the point.
# A two-line stderr is a complete lesson; two lines of prose is "on it" or "let me check
# that". 400 is the observed floor for a message that states a decision rather than
# narrating one — measured against the real corpus, not chosen for roundness.
MIN_PROSE_CHARS = 400

# And an upper bound, because the corpus contains a single 804,441-character record. Past a
# few thousand characters a shard is a transcript rather than a lesson: the embedder cannot
# represent it, and the ladder cannot compare it to anything. Truncated rather than dropped
# — the opening of a long message is usually the point of it.
MAX_EVIDENCE_CHARS = 4000


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
        # Per-directory attribution (GRPH-356). `project_id` is storage locality —
        # often "core" for every transcript — and must not become a counted project.
        cwd = next((ev.project for ev in events if (ev.project or "").strip()), None)
        attributed = _attributed_project_id(
            db, adapter, source, project_id=project_id, cwd=cwd)
        for ev in events:
            if _record(db, mem_svc, ev, project_id, attributed_project_id=attributed):
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


def _origin_of(ev: Event) -> str:
    """Where a shard came from, and — for an episode — whether it was ever RESOLVED
    (GRPH-349).

    Carried on `origin` so the distinction survives into the store. A resolved episode
    shows what was done differently and is a lesson; an unresolved failure is evidence
    that something is repeatedly painful and is not. Filing them under one origin would
    let the second read as the first, which is the defect class this whole PRD keeps
    turning up.
    """
    base = f"ingest:{ev.harness}"
    if ev.kind != "episode":
        return base
    # `resolved` | `unresolved` | `transient` — three different claims about one failure.
    return f"{base}:{(ev.metadata or {}).get('episode', 'unresolved')}"


def is_evidence(ev: Event) -> bool:
    """Does this event carry something worth learning from (GRPH-345)?

    **The filter exists because the first real run recorded 1,088 shards from ONE
    transcript** — pytest summaries, branch deletions, assertion fragments. PRD-16 says the
    loop exists so the corpus "does not become landfill", and at that rate it WAS the
    landfill. GRPH-342 was the immediate cause: teaching the adapter to read tool results
    recovered the entire failure signal, but it piped every SUCCESSFUL command's stdout in
    alongside it.

    Three rules, and each is a claim about where lessons actually come from:

    - **A tool result is evidence when it FAILED.** Successful stdout is the normal state of
      work — `124 passed` is what is supposed to happen, and a corpus of things going to plan
      teaches nothing. This is the cut that does the heavy lifting.
    - **A user message is evidence** once it is long enough to state something. A transcript's
      corrections, constraints and rejected approaches all arrive here; it is the one place
      somebody says what they actually wanted.
    - **Assistant prose is not evidence.** It is narration of work in progress, and it is
      also where a confident wrong explanation looks exactly like a right one. The durable
      form of anything worth keeping is what the human then confirmed or corrected — which
      the rule above already captures.

    Deliberately generic, driven by fields every adapter fills. Putting it here rather than
    in `claude_code.py` keeps ONE seam to test and stops a second harness quietly inventing
    a different answer to the same question.
    """
    text = (ev.text or "").strip()
    outcome = (ev.metadata or {}).get("outcome", "unknown")

    if outcome == "failed":
        return len(text) >= MIN_EVIDENCE_CHARS
    if outcome == "ok":
        return False
    # No tool result at all: prose, and only a person's counts.
    if ev.kind == "user":
        return len(text) >= MIN_PROSE_CHARS
    return False


def _attributed_project_id(
    db: Session, adapter: IngestAdapter, source: str, *,
    project_id: str, cwd: str | None,
) -> str | None:
    """Map a Claude Code transcript directory onto a known Graphban project.

    None when the directory does not uniquely match. Never falls back to the
    ingest `project_id` (often "core") — that collapse is why distinct_projects
    was permanently 1.
    """
    lookup = getattr(adapter, "repo_dir_for", None)
    repo_dir = lookup(source) if callable(lookup) else None
    return _map_transcript_dir(db, repo_dir, project_id=project_id, cwd=cwd)


def _last_component(cwd: str | None) -> str | None:
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    name = Path(cwd.strip()).name
    return name.lower() if name else None


def _project_tokens(project: Project) -> set[str]:
    tokens = {project.id.lower()}
    name_slug = re.sub(r"[^a-z0-9]+", "-", project.name.lower()).strip("-")[:32]
    if name_slug:
        tokens.add(name_slug)
    return {t for t in tokens if t}


def _map_transcript_dir(
    db: Session, repo_dir: str | None, *, project_id: str, cwd: str | None,
) -> str | None:
    """Map a per-repo transcript folder onto a sibling project.

    Candidates are `sibling_project_ids` of the ingest storage project — a
    foreign org id is an independence input, not a folder name. Ambiguous
    (two projects tied) → None, not a guess. Unmapped → None, not "core".
    """
    if not isinstance(repo_dir, str) or not repo_dir.strip():
        return None
    # Lazy: memory imports nothing from ingest; ingest already lazy-imports
    # add_memory. A top-level import would cycle through that same load.
    from app.services.memory import sibling_project_ids

    sibling_ids = sibling_project_ids(db, project_id)
    if not sibling_ids:
        return None
    encoded = repo_dir.strip().lower()
    last = _last_component(cwd)
    hits: list[tuple[int, str]] = []
    for project in db.scalars(select(Project).where(Project.id.in_(sibling_ids))).all():
        n = _transcript_dir_match_len(
            encoded, project, last=last, storage_id=project_id)
        if n:
            hits.append((n, project.id))
    if not hits:
        return None
    best = max(n for n, _ in hits)
    winners = [pid for n, pid in hits if n == best]
    return winners[0] if len(winners) == 1 else None


def _transcript_dir_match_len(
    encoded: str, project: Project, *, last: str | None, storage_id: str,
) -> int:
    tokens = _project_tokens(project)
    best = 0
    for token in tokens:
        if last is not None:
            # Encoded suffix treats `acme-core` as seed `core`. cwd's last
            # component is the real folder name and is not lossy.
            if last == token:
                best = max(best, len(token))
            continue
        # No cwd: suffix-match siblings, but never the storage id — we cannot
        # prove the last component equals it, and inventing that is the collapse.
        if project.id == storage_id:
            continue
        if encoded == token or encoded.endswith("-" + token):
            best = max(best, len(token))
    return best


def _record(db: Session, mem_svc, ev: Event, project_id: str,
            attributed_project_id: str | None = None) -> bool:
    """One event to a candidate shard, or nothing.

    Enters as `candidate`, never `published`: this is machine-mined evidence from a
    transcript, and PRD-16's non-goal is explicit that Graphban's existing triage path
    stays the sole owner of "is this worth keeping". Publishing here would run a second
    lifecycle beside the one that already exists.

    `attributed_project_id` is the per-source mapping (GRPH-356). Actor is omitted —
    transcripts do not record a user, and inventing one would make distinct_users a
    count of 1 rather than unmeasured (GRPH-306).

    Text is NOT scrubbed here — `add_memory` does it on the write path, so every producer
    inherits it rather than each remembering to ask (GRPH-305).
    """
    text = (ev.text or "").strip()[:MAX_EVIDENCE_CHARS]
    if not is_evidence(ev):
        return False
    try:
        mem_svc.add_memory(
            db, text_body=text, scope="global", project_id=project_id,
            source=f"transcript:{ev.harness}:{ev.session_id}",
            status="candidate", origin=_origin_of(ev),
            # A structured shard must be COMPARED on its content, or the ladder clusters
            # on the format every episode shares (GRPH-350).
            embed_text=(ev.metadata or {}).get("embed_text"),
            # The ladder scores it later; scoring every line at write time would spend the
            # provider budget on material most of which is never promoted.
            auto_triage=False,
            attributed_project_id=attributed_project_id,
        )
        return True
    except Exception:  # noqa: BLE001 — one bad row must not end the run
        # THE rollback, and it is the whole of "must not end the run". Without it a failed
        # write leaves the session in a poisoned state and EVERY later event dies with
        # PendingRollbackError — so a single bad row still ends the run, just noisily
        # instead of cleanly, and the stats report zero while looking like they tried.
        db.rollback()
        logger.warning("ingest: could not record an event from %s", ev.session_id,
                       exc_info=True)
        return False
