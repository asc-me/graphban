"""The one thread an accepted PRD link carries (GRPH-P104 S1).

Anyone who can read either PRD's project reads and posts. A pending, declined or unlinked link,
or one whose either PRD is closed, is read-only; nothing is ever deleted.
"""
from __future__ import annotations

from sqlalchemy import select
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
        author_label=caller.label, author_owner=caller.owner)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return messages_svc.render(db, msg)
