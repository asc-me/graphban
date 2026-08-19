"""Feature/bug request (triage) service."""
from __future__ import annotations


from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, Request
from app.services import items as items_svc
from app.services import keys

REQUEST_TYPES = ["bug", "feature", "enhancement", "feedback"]


def list_requests(db: Session, project_id: str | None = None, type_: str | None = None) -> list[Request]:
    stmt = select(Request)
    if project_id:
        stmt = stmt.where(Request.project_id == project_id)
    if type_:
        stmt = stmt.where(Request.type == type_)
    return list(db.scalars(stmt.order_by(Request.created_at.desc())).all())


def create_request(
    db: Session,
    *,
    type_: str,
    title: str,
    detail: str = "",
    by: str = "",
    project_id: str = "core",
    ago: str = "just now",
    source_url: str = "",
    meta: dict | None = None,
    attachment_ids: list[str] | None = None,
) -> Request:
    if type_ not in REQUEST_TYPES:
        raise ValueError(f"invalid request type: {type_}")
    # See create_item: the id is frozen identity, `number` is what the key renders from.
    req_id, number = keys.mint(db, project_id, "request")
    req = Request(
        id=req_id,
        number=number,
        project_id=project_id,
        type=type_,
        title=title,
        detail=detail,
        by=by,
        votes=0,
        status="new",
        ago=ago,
        source_url=source_url,
        meta=meta or {},
        attachment_ids=attachment_ids or [],
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def vote_request(db: Session, request_id: str, delta: int = 1) -> Request | None:
    req = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if req is None:
        return None
    req.votes = max(0, req.votes + delta)
    db.commit()
    db.refresh(req)
    return req


def link_request(db: Session, request_id: str, item_id: str | None) -> Request | None:
    req = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if req is None:
        return None
    if item_id:
        if db.get(Item, keys.resolve_item(db, item_id) or item_id) is None:
            raise ValueError(f"item not found: {item_id}")
        req.linked_to = item_id
        req.status = "linked"
    else:
        req.linked_to = None
        req.status = "new"
    db.commit()
    db.refresh(req)
    return req


def accept_request(db: Session, request_id: str, *, reporter: dict | None = None) -> tuple[Request, Item] | None:
    """Triage a request into tracked work: create the item and link it, atomically.

    Doing this as two calls from the client leaves a window where the item exists and the
    link failed — an orphan item plus a request still sitting in the queue, which reads as
    "nobody triaged this" while the work is already on the board. One commit, or neither.

    Idempotent: a request already linked returns its existing item rather than minting a
    second one, so a double-click cannot fork the same report into two pieces of work.
    """
    req = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if req is None:
        return None
    if req.linked_to:
        existing = db.get(Item, keys.resolve_item(db, req.linked_to) or req.linked_to)
        if existing is not None:
            return req, existing

    item = items_svc.create_item(
        db,
        title=req.title,
        description=req.detail or "",
        # The request's type is what a triager sorts by, so it survives onto the item.
        tags=[req.type] if req.type else [],
        project_id=req.project_id,
        reporter=reporter,
        commit=False,
    )
    req.linked_to = item.id
    req.status = "linked"
    db.commit()
    db.refresh(req)
    db.refresh(item)
    return req, item


def set_status(db: Session, request_id: str, status: str) -> Request | None:
    req = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if req is None:
        return None
    req.status = status
    db.commit()
    db.refresh(req)
    return req
