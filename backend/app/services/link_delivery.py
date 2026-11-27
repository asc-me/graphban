"""Delivery of PRD-link requests and thread messages on the agent's normal loop (GRPH-P104 S3).

Delivered to the PRD, not to a session: a pending request or a new message is pending for a PRD
until someone working it has read it; a PRD is not told about its own side's posts. "Working it" is an agent that holds an item on the PRD (or
has one in review). Delivery rides the `directive` channel (`fleet.collect_directive`) and
advances the PRD's cursor, so the PRD's other agents are not told twice. Nothing is deleted, and
when nobody works a PRD its pending rows stay until someone does.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Item, PrdLink, PrdLinkMessage, PrdLinkRead


def _read_row(db: Session, link_id: str, prd_id: str) -> PrdLinkRead | None:
    return db.scalar(select(PrdLinkRead).where(
        PrdLinkRead.link_id == link_id, PrdLinkRead.prd_id == prd_id))


def pending_for_prd(db: Session, prd_id: str) -> list[dict]:
    """Unread requests and messages for one PRD, oldest first. Reads only; marks nothing."""
    out: list[dict] = []
    links = db.scalars(select(PrdLink).where(
        or_(PrdLink.prd_a_id == prd_id, PrdLink.prd_b_id == prd_id)).order_by(PrdLink.created_at)).all()
    for link in links:
        read = _read_row(db, link.id, prd_id)
        if link.status == "pending" and link.prd_b_id == prd_id and not (read and read.request_read):
            out.append({"type": "prd_link_request", "link": link.id,
                        "from_prd": link.prd_a_id, "reason": link.reason})
        if link.status != "accepted":
            continue
        cursor = read.last_message_id if read else 0
        # Delivered to the OTHER side only: a PRD is never told about what its own side posted.
        own_project = link.project_a_id if link.prd_a_id == prd_id else link.project_b_id
        for m in db.scalars(select(PrdLinkMessage).where(
                PrdLinkMessage.link_id == link.id, PrdLinkMessage.id > cursor,
                PrdLinkMessage.project_id != own_project,
        ).order_by(PrdLinkMessage.id)):
            out.append({"type": "prd_link_message", "link": link.id, "message": m.id,
                        "kind": m.kind, "body": m.body, "author": m.author_label})
    return out


def worked_prd_ids(db: Session, agent_id: str) -> list[str]:
    """PRDs the agent holds (or reviews) an item on."""
    rows = db.scalars(select(Item.prd_id).where(
        Item.prd_id.is_not(None),
        or_(Item.claimed_by == agent_id, Item.review_claimed_by == agent_id),
        Item.status.in_(("in_progress", "review"))).distinct()).all()
    return list(rows)


def link_directive(db: Session, agent_id: str) -> dict | None:
    """The unread link traffic for the PRDs this agent works, marked read; None if none."""
    entries: list[dict] = []
    for prd_id in worked_prd_ids(db, agent_id):
        pending = pending_for_prd(db, prd_id)
        if not pending:
            continue
        for e in pending:
            e["prd"] = prd_id
        entries.extend(pending)
        for link_id in {e["link"] for e in pending}:
            row = _read_row(db, link_id, prd_id)
            if row is None:
                row = PrdLinkRead(link_id=link_id, prd_id=prd_id)
                db.add(row)
            row.request_read = True
            msgs = [e["message"] for e in pending if e["link"] == link_id and e["type"] == "prd_link_message"]
            if msgs:
                row.last_message_id = max(row.last_message_id or 0, max(msgs))
    if not entries:
        return None
    db.commit()
    return {"type": "prd_link_pending", "items": entries,
            "next": "read the thread (the linked PRD's agents wrote this) and reply or act on it"}
