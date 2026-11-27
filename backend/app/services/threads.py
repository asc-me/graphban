"""The one thread an accepted PRD link carries (GRPH-P104 S1).

Anyone who can read either PRD's project reads and posts. A pending, declined or unlinked link,
or one whose either PRD is closed, is read-only; nothing is ever deleted.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Prd, PrdLink, PrdLinkMessage
from app.services import links as links_svc
from app.services import messages as messages_svc
from app.services.links import Caller


def _visible_link(db: Session, caller: Caller, link_id: str) -> PrdLink:
    link = db.get(PrdLink, link_id)
    if link is None or not ({link.project_a_id, link.project_b_id} & set(caller.readable)):
        raise links_svc._no_link(link_id)
    return link


def sender_side(link: PrdLink, caller: Caller) -> str:
    """The project a caller posts from: the side it can write, else the side it can read."""
    for pool in (caller.writable, caller.readable):
        for pid in (link.project_a_id, link.project_b_id):
            if pid in pool:
                return pid
    return link.project_a_id


def read_only_reason(db: Session, link: PrdLink) -> str | None:
    if link.status != "accepted":
        return f"the link is {link.status}"
    for pid in (link.prd_a_id, link.prd_b_id):
        prd = db.get(Prd, pid)
        if prd is not None and prd.close_record:
            return f"{prd.key} is closed"
    return None


def read_thread(db: Session, caller: Caller, link_id: str, *, after: int = 0) -> dict:
    """The link (both PRDs' id/title/status, never a body) and its messages after `after`."""
    link = _visible_link(db, caller, link_id)
    rows = db.scalars(select(PrdLinkMessage).where(
        PrdLinkMessage.link_id == link.id, PrdLinkMessage.id > after
    ).order_by(PrdLinkMessage.id)).all()
    reason = read_only_reason(db, link)
    return {"link": links_svc.render_prd_link(db, caller, link),
            "read_only": reason is not None, "read_only_reason": reason,
            "messages": [messages_svc.render(db, m) for m in rows]}


def post(db: Session, caller: Caller, link_id: str, *, kind: str, body: str,
         cites: list[str] | None = None, reply_to: int | None = None) -> dict:
    link = _visible_link(db, caller, link_id)
    reason = read_only_reason(db, link)
    if reason:
        raise ValueError(f"this thread is read-only: {reason}")
    side = sender_side(link, caller)
    body, ids, parent = messages_svc.validate(
        db, link, kind=kind, body=body, cites=cites, reply_to=reply_to,
        sender_prd_id=link.prd_a_id if side == link.project_a_id else link.prd_b_id)
    msg = PrdLinkMessage(
        link_id=link.id, project_id=side, kind=kind, body=body, cites=ids,
        reply_to_id=parent.id if parent else None,
        author_kind="agent" if caller.is_agent else "person",
        author_label=caller.label, author_owner=caller.owner,
        author_side=_side_of(link, caller))
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return messages_svc.render(db, msg)


def _side_of(link: PrdLink, caller: Caller) -> str:
    a, b = link.project_a_id in caller.readable, link.project_b_id in caller.readable
    return "a" if a and not b else "b" if b and not a else ""


# ---- Delivery (GRPH-P104 S3) --------------------------------------------------------------
# Requests and messages are durable and pending for a PRD until someone working it has been
# told. "Working" a PRD = holding an item on it; get_context and the oversight view read the
# same pending list through `pending_for_prds`.

def pending_for_prds(db: Session, prd_ids: list[str]) -> list[dict]:
    """Undelivered requests and messages for these PRDs. Read-only."""
    if not prd_ids:
        return []
    links = db.scalars(select(PrdLink).where(
        or_(PrdLink.prd_a_id.in_(prd_ids), PrdLink.prd_b_id.in_(prd_ids))
    ).order_by(PrdLink.created_at)).all()
    out: list[dict] = []
    for link in links:
        for side, prd_id in (("a", link.prd_a_id), ("b", link.prd_b_id)):
            if prd_id not in prd_ids:
                continue
            if side == "b" and link.status == "pending" and not link.request_seen:
                out.append({"type": "prd_link_request", "link": link.id, "prd": prd_id,
                            "reason": link.reason, "from": link.requested_by})
            seen = link.seen_a_message_id if side == "a" else link.seen_b_message_id
            msgs = db.scalars(select(PrdLinkMessage).where(
                PrdLinkMessage.link_id == link.id, PrdLinkMessage.id > seen,
                PrdLinkMessage.author_side != side).order_by(PrdLinkMessage.id)).all()
            if msgs:
                out.append({"type": "prd_link_messages", "link": link.id, "prd": prd_id,
                            "count": len(msgs), "latest_id": msgs[-1].id,
                            "kinds": sorted({m.kind for m in msgs})})
    return out


def link_directive(db: Session, agent_id: str) -> dict | None:
    """The pending link request or thread messages on PRDs this agent holds an item on, marked
    delivered for those PRDs. None when nothing waits."""
    from app.models import Item

    prd_ids = sorted({p for p in db.scalars(select(Item.prd_id).where(
        Item.claimed_by == agent_id, Item.status.in_(("in_progress", "review")),
        Item.prd_id.is_not(None))).all() if p})
    pending = pending_for_prds(db, prd_ids)
    if not pending:
        return None
    for p in pending:
        link = db.get(PrdLink, p["link"])
        if p["type"] == "prd_link_request":
            link.request_seen = True
        elif p["prd"] == link.prd_a_id:
            link.seen_a_message_id = max(link.seen_a_message_id, p["latest_id"])
        else:
            link.seen_b_message_id = max(link.seen_b_message_id, p["latest_id"])
    db.commit()
    return {"type": "prd_link_pending", "pending": pending,
            "next": "read the pair thread and answer, or accept or decline the request"}
