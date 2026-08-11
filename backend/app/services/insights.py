"""Cross-cutting agent operations: explicit lesson extraction and progress digests."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import items as items_svc
from app.services import memory as memory_svc
from app.services import platform as platform_svc


def _outcome_text(item) -> str:
    """What the item turned out to be, drawn from the fields written AFTER the work.

    `evidence[]` is filled when an item is marked done and is the closest thing the tracker
    holds to a record of what actually happened; the PR link and final status corroborate it.
    Empty for an item nobody left evidence on, which is the case the caller falls back for.
    """
    parts: list[str] = []
    for ev in (item.evidence or []):
        if not isinstance(ev, dict):
            continue
        detail = str(ev.get("detail") or "").strip()
        url = str(ev.get("url") or "").strip()
        if detail or url:
            kind = str(ev.get("kind") or "note")
            parts.append(f"- {kind}: {detail}" + (f" ({url})" if url else ""))
    link = _evidence_link(item)
    if link and not any(link in p for p in parts):
        parts.append(f"- link: {link}")
    if parts:
        parts.append(f"- final status: {item.status}")
    return "\n".join(parts)


def _extraction_source(item) -> str:
    """The document the extractor distils from (GRPH-358).

    **Outcome first, proposal second and labelled.** An item's DESCRIPTION is written before
    the work: it holds the proposal, the options weighed, and framing the build then revised.
    Distilling it produces a lesson about what somebody INTENDED, filed as though it were
    what happened — which is how closing GRPH-354 produced a confident, specific claim that
    the code merged the same hour contradicts.

    Ordered rather than filtered, and the honesty matters: the outcome fields this tracker
    holds are mostly links and test counts, so dropping the description entirely would
    usually leave nothing durable to learn from. The description is still the richest prose
    on the item — it is just not authoritative about how things ended up. So it is kept,
    placed second, and named as possibly stale.

    This is an IMPROVEMENT, not a guarantee: whether a model honours the labelling depends on
    the model, and the configured extractor may be an 8B local one. The guarantee lives in
    `memory.may_auto_publish`, which stops any of this reaching the trusted retrieval path
    without a human regardless of what the extractor concludes.
    """
    outcome = _outcome_text(item)
    if not outcome:
        return item.description or ""
    return "\n\n".join([
        "WHAT ACTUALLY HAPPENED (authoritative — prefer this where the two disagree):",
        outcome,
        "ORIGINAL PROPOSAL (written BEFORE the work; the build may have revised or reversed "
        "it — do not state anything from here as settled fact):",
        item.description or "(none)",
    ])


def extract_lessons(db: Session, item_id: str) -> list[dict]:
    """Explicitly distill lessons from an item into memory shards (MCP extract_lessons)."""
    item = items_svc.get_item(db, item_id)
    if item is None:
        raise ValueError(f"item not found: {item_id}")
    lessons = platform_svc.extractor_for(db, item.project_id).extract(
        title=item.title, description=_extraction_source(item))
    created = []
    for text in lessons:
        # Auto-extracted lessons are agent telemetry — enter as candidates for
        # human review, not straight into the trusted retrieval path (AL-49).
        #
        # `auto_triage` still runs, and should: it auto-REJECTS near-duplicates, which keeps
        # the review queue readable. What it may no longer do is publish — see
        # `memory.may_auto_publish` (GRPH-358).
        shard = memory_svc.add_memory(
            db, text_body=text, scope="item", source=f"lesson from {item.id}",
            item_id=item.id, project_id=item.project_id, fresh=True,
            status="candidate", origin="agent:auto-extract",
        )
        created.append({"id": shard.id, "text": shard.text, "status": shard.status})
    return created


def _evidence_link(it) -> str:
    """A verifiable artifact for an item — a linked issue/PR — or "" if none."""
    if it.github_url:
        return it.github_url
    if isinstance(it.pr, dict):
        if it.pr.get("url"):
            return it.pr["url"]
        if it.pr.get("number"):
            return f"PR #{it.pr['number']}"
    return ""


def generate_digest(db: Session, project_id: str | None = None) -> str:
    """A decision-ready escalation packet, not a status dump (AL-52).

    The continuous-maintenance thesis: escalation should let the reader supply
    judgment without reconstructing the trajectory. So the digest leads with state,
    then the trajectory (attempted → evidence → risk), and ends on the single
    smallest unresolved choice — the one decision that moves things forward."""
    all_items = items_svc.list_items(db, project_id=project_id)
    by_status: dict[str, int] = {}
    for it in all_items:
        by_status[it.status] = by_status.get(it.status, 0) + 1

    done = by_status.get("done", 0)
    total = len(all_items)
    pct = round(100 * done / total) if total else 0
    scope = project_id or "all projects"

    lines = [f"# Decision packet · {scope}", ""]
    state = " · ".join(
        f"{by_status[s]} {s.replace('_', ' ')}" for s in
        ("done", "in_progress", "review", "blocked", "next", "backlog") if by_status.get(s)
    )
    lines.append(f"**State** — {state or 'no items yet'} ({pct}% done)")

    # Attempted — the trajectory currently in flight.
    in_progress = [it for it in all_items if it.status == "in_progress"]
    lines += ["", "**Attempted** — in flight"]
    if in_progress:
        lines += [f"- {it.id} {it.title}" + (f" (claimed by {it.claimed_by})" if it.claimed_by else "")
                  for it in in_progress]
    else:
        lines.append("- nothing in flight")

    # Evidence — verifiable results to judge (in-review + recently done, links when present).
    review = [it for it in all_items if it.status == "review"]
    recent_done = sorted(
        (it for it in all_items if it.status == "done"), key=lambda it: it.updated_at, reverse=True
    )[:5]
    lines += ["", "**Evidence** — verifiable now"]
    if review or recent_done:
        for it in review:
            link = _evidence_link(it)
            lines.append(f"- {it.id} {it.title} — in review" + (f" · {link}" if link else ""))
        for it in recent_done:
            link = _evidence_link(it)
            lines.append(f"- {it.id} {it.title} — done" + (f" · {link}" if link else ""))
    else:
        lines.append("- no completed or in-review work yet")

    # Risk — what threatens the trajectory.
    blocked = [it for it in all_items if it.status == "blocked"]
    open_high = [it for it in all_items
                 if it.fidelity == "high" and it.status in ("backlog", "next", "in_progress")]
    lines += ["", "**Risk** — threats to the trajectory"]
    if blocked or open_high:
        lines += [f"- {it.id} {it.title} — blocked: {it.blocker or 'no reason given'}" for it in blocked]
        if open_high:
            ids = ", ".join(it.id for it in open_high[:5])
            lines.append(f"- {len(open_high)} open high-fidelity item(s) need a prototype first ({ids})")
    else:
        lines.append("- no blockers flagged")

    # Smallest unresolved choice — the one decision that unblocks progress.
    lines += ["", "**Smallest unresolved choice**"]
    if review:
        more = f" ({len(review) - 1} more awaiting review)" if len(review) > 1 else ""
        lines.append(f"Review {review[0].id} {review[0].title} — accept, or send back?{more}")
    elif blocked:
        lines.append(f"Unblock {blocked[0].id} — {blocked[0].blocker or 'decide how to proceed'}.")
    else:
        nxt = items_svc.suggest_next(db, project_id=project_id)
        if nxt:
            lines.append(f"Start {nxt.id} {nxt.title} (effort {nxt.effort}) — or repick?")
        else:
            lines.append("Nothing pending — the queue is clear.")

    return "\n".join(lines)
