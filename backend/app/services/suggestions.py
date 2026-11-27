"""Suggested items on a PRD link's thread, decided by the receiving side (GRPH-P104 S2).

A `suggested_item` is a thread message (S1) carrying the item for the OTHER side's PRD. The
receiving side files it with its own caller, so the new item is written in the receiving project
and the sender never gets write access to it. By default only a person files; the org's one
`prd_link_accept_agents` setting lets a receiving agent too. The decision is made once: the first
`file` or `decline` wins, and a later or concurrent one gets `already_decided` with the stored
outcome and changes nothing. There is no retry — a corrected suggestion is a new message.
"""
from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import AppError, Refused
from app.models import Item, Link, Organization, PrdLinkMessage, utcnow
from app.services import events as events_svc
from app.services import items as items_svc
from app.services import links as links_svc
from app.services import messages as messages_svc
from app.services import threads
from app.services.links import PRD_LINK_WRITE_RULE, Caller


NOT_ALLOWED = "not_allowed"
ALREADY_DECIDED = "already_decided"


def suggest(db: Session, caller: Caller, link_id: str, suggestion: dict, *,
            reply_to: int | None = None) -> dict:
    """Post a `suggested_item` from the caller's side. Creates nothing in the other project."""
    return threads.post(db, caller, link_id, kind="suggested_item",
                        body=json.dumps(suggestion), reply_to=reply_to)


def _message(db: Session, caller: Caller, link_id: str, message_id: int):
    link = threads._visible_link(db, caller, link_id)
    msg = db.get(PrdLinkMessage, message_id)
    if msg is None or msg.link_id != link.id or msg.kind != "suggested_item":
        raise ValueError(f"message {message_id} is not a suggested_item on this thread")
    return link, msg


def _receiver(link, msg) -> tuple[str, str]:
    """(project, PRD) on the side that did not send the suggestion."""
    if msg.project_id == link.project_a_id:
        return link.project_b_id, link.prd_b_id
    return link.project_a_id, link.prd_a_id


def _stored(msg: PrdLinkMessage) -> dict:
    return {"decided": False, "code": ALREADY_DECIDED, "outcome": messages_svc.render_outcome(msg)}


def _authorize(db: Session, caller: Caller, link, msg) -> None:
    project, _ = _receiver(link, msg)
    if project not in caller.writable:
        raise Refused(
            403, f"{NOT_ALLOWED}: a suggestion is decided by someone who can write the receiving "
            "project", rule_id=PRD_LINK_WRITE_RULE, hint=NOT_ALLOWED)
    if caller.is_agent and not db.scalar(select(Organization.prd_link_accept_agents).where(
            Organization.id == links_svc._org_of(db, project))):
        links_svc._needs_person(
            "a suggested item is filed by a person with write access to the receiving "
            "project; this organization does not let an agent file it")


def _record(db: Session, caller: Caller, link, msg: PrdLinkMessage, *, outcome: str, code: str,
            message: str, item_id: str = "") -> dict | None:
    """Store the one decision and its reply on the thread, in the caller's transaction. None
    if another decision got there first (nothing is committed or changed)."""
    won = db.execute(update(PrdLinkMessage).where(
        PrdLinkMessage.id == msg.id, PrdLinkMessage.outcome == "").values(
        outcome=outcome, outcome_code=code, outcome_message=message, outcome_item_id=item_id,
        decided_by=caller.who, decided_at=utcnow())).rowcount
    if not won:
        db.rollback()
        db.refresh(msg)
        return None
    db.add(PrdLinkMessage(
        link_id=link.id, kind="note", body=f"{outcome}: {code}. {message}", cites=[],
        reply_to_id=msg.id, author_kind="agent" if caller.is_agent else "person",
        author_label=caller.label, author_owner=caller.owner, project_id=_receiver(link, msg)[0]))
    db.commit()
    db.refresh(msg)
    events_svc.record_prd_link(
        db, caller, action="prd_link_suggestion_filed" if outcome == "filed"
        else "prd_link_suggestion_declined", link=link,
        meta={"message": msg.id, "outcome": outcome, "code": code, "item": item_id})
    return {"decided": True, "code": code, "outcome": messages_svc.render_outcome(msg)}


def _refusal(exc: Exception) -> tuple[str, str]:
    """The code and full message create_item refused with, so the sender can correct it."""
    code = getattr(exc, "mcp_code", None) or getattr(exc, "code", None)
    if not code or code == "internal":
        code = "validation" if isinstance(exc, ValueError) else "conflict"
    detail = getattr(exc, "detail", None) or str(exc)
    hint = getattr(exc, "hint", None)
    return str(code), f"{detail}" + (f" (hint: {hint})" if hint else "")


def file_suggestion(db: Session, caller: Caller, link_id: str, message_id: int) -> dict:
    """File the suggestion as an item in the receiving project, on the receiving PRD, with the
    caller's own authority. Returns `{decided, code, outcome}`; `outcome.item` is the new key."""
    link, msg = _message(db, caller, link_id, message_id)
    _authorize(db, caller, link, msg)
    project, prd_id = _receiver(link, msg)
    if msg.outcome:
        return _stored(msg)
    why = threads.read_only_reason(db, link)
    if why:
        code = "unlinked" if link.status != "accepted" else "prd_closed"
        return _record(db, caller, link, msg, outcome="conflict", code=code,
                       message=why) or _stored(msg)
    data = json.loads(msg.body)
    try:
        kwargs = {k: data[k] for k in ("kind", "description", "touchpoints", "tags", "status")
                  if k in data}
        item = items_svc.create_item(
            db, title=data["title"], project_id=project, prd_id=prd_id, commit=False,
            reporter={"suggested_by": msg.author_label, "owner": msg.author_owner,
                      "link": link.id}, **kwargs)
        if data.get("link"):
            other, direction = data["link"]["item"], data["link"]["direction"]
            far = db.get(Item, other)
            if far is None:
                raise ValueError(f"the linked item {other!r} no longer exists")
            # Stored in the SOURCE's project, as link_items stores a cross-project edge
            # (GRPH-1063): it belongs to the item it constrains. The accepted PRD link is what
            # lets the filer write it on the sender's side (S4).
            a, b, home = ((item.id, other, project) if direction == "depends_on"
                          else (other, item.id, far.project_id))
            db.add(Link(project_id=home, a=a, b=b, type="dependency", confidence=1.0,
                        reason=f"suggested on PRD link {link.id}"))
        db.flush()
        item_id, key = item.id, item.key
    except (AppError, Refused, ValueError) as exc:
        db.rollback()
        code, message = _refusal(exc)
        return _record(db, caller, link, msg, outcome="conflict", code=code,
                       message=message) or _stored(msg)
    done = _record(db, caller, link, msg, outcome="filed", code="filed",
                   message=f"filed as {key}", item_id=item_id)
    return done or _stored(msg)


def decline_suggestion(db: Session, caller: Caller, link_id: str, message_id: int, *,
                       reason: str) -> dict:
    """Decline with a reason; creates nothing. The suggestion and its reason stay on the thread."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a decline needs a reason")
    link, msg = _message(db, caller, link_id, message_id)
    _authorize(db, caller, link, msg)
    if msg.outcome:
        return _stored(msg)
    return _record(db, caller, link, msg, outcome="declined", code="declined",
                   message=reason) or _stored(msg)
