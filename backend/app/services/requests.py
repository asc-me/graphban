"""Feature/bug request (triage) service."""
from __future__ import annotations

import hashlib
from secrets import token_urlsafe

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, Request, RequestComment
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


def _derive_by(account: dict | None, email: str) -> str:
    """PRD-43 D2: triage display handle. Prefer account.name, else email, else empty.
    Never write 'public' — absence of identity is a distinct state."""
    if account:
        name = account.get("name", "")
        if name:
            return name
        acct_email = account.get("email", "")
        if acct_email:
            return acct_email
    if email:
        return email
    return ""


def _mint_track_token() -> tuple[str, str]:
    """PRD-43 D3: mint an unguessable tracking token. Returns (plaintext, hash)."""
    plain = token_urlsafe(32)
    return plain, hashlib.sha256(plain.encode()).hexdigest()


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
    account: dict | None = None,
    email: str = "",
) -> Request:
    if type_ not in REQUEST_TYPES:
        raise ValueError(f"invalid request type: {type_}")
    # See create_item: the id is frozen identity, `number` is what the key renders from.
    req_id, number = keys.mint(db, project_id, "request")

    # PRD-43 D2: derive `by` from account/email, never default to "public".
    derived_by = _derive_by(account, email) if (account or email) else by

    meta = dict(meta or {})
    # PRD-43 D2: store account in meta.account if provided.
    if account:
        meta["account"] = {k: v for k, v in account.items() if v}
    # PRD-43 D2: identity state — "present" or "absent".
    if derived_by:
        meta["identity"] = "present"
    else:
        meta["identity"] = "absent"

    # PRD-43 D3: mint tracking token.
    track_plain, track_hash = _mint_track_token()

    req = Request(
        id=req_id,
        number=number,
        project_id=project_id,
        type=type_,
        title=title,
        detail=detail,
        by=derived_by,
        votes=0,
        status="new",
        ago=ago,
        source_url=source_url,
        meta=meta,
        attachment_ids=attachment_ids or [],
        track_token_hash=track_hash,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    # Stash the plaintext on the object so the router can build track_url.
    req._track_token_plain = track_plain  # type: ignore[attr-defined]
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
        # RESOLVE BEFORE STORING, not merely to check existence. `items.id` is frozen at issue
        # time while the key a human sees is rendered from the project's current tag, so the
        # two diverge the moment a project is retagged — and `requests.linked_to` carries a
        # foreign key to `items(id)`. Storing the caller's spelling put `GRPH-141` in a column
        # whose rows are keyed `AL-141`, so every link made through the UI died on
        # `requests_linked_to_fkey` (GRPH-459). `ItemOut.id` is aliased from `key`, so the
        # rendered key is exactly what the client has to send.
        #
        # Same rule `_stored_prd_id` follows for `Item.prd_id` (GRPH-319); this writer was
        # missed when that class was closed. Unlike that one, there is no degrade-as-given
        # path here: the foreign key means an unresolvable value cannot be stored at all, and
        # the check below has already rejected it.
        resolved = keys.resolve_item(db, item_id) or item_id
        if db.get(Item, resolved) is None:
            raise ValueError(f"item not found: {item_id}")
        req.linked_to = resolved
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


# ---- PRD-43: publish, comments, tracking, boards ----

def publish_request(db: Session, request_id: str) -> Request | None:
    """PRD-43 D5: mark a request as published (visible on public boards)."""
    from app.models import utcnow
    req = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if req is None:
        return None
    if req.published_at is None:
        req.published_at = utcnow()
        db.commit()
        db.refresh(req)
    return req


def unpublish_request(db: Session, request_id: str) -> Request | None:
    """PRD-43 D5: remove from public boards. Tracking page still works."""
    req = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if req is None:
        return None
    if req.published_at is not None:
        req.published_at = None
        db.commit()
        db.refresh(req)
    return req


def create_comment(
    db: Session,
    *,
    request_id: str,
    author_user_id: str,
    body: str,
    visibility: str = "private",
) -> RequestComment:
    """PRD-43 D7: operator comment. Default private; must be explicitly tagged public."""
    from app.services import keys as _keys
    req_id = _keys.resolve_request(db, request_id) or request_id
    if db.get(Request, req_id) is None:
        raise ValueError(f"request not found: {request_id}")
    if visibility not in ("private", "public"):
        raise ValueError(f"invalid visibility: {visibility}")
    comment = RequestComment(
        id=f"rcom_{token_urlsafe(12)}",
        request_id=req_id,
        author_user_id=author_user_id,
        body=body,
        visibility=visibility,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment


def list_comments(db: Session, request_id: str, *, visibility: str | None = None) -> list[RequestComment]:
    """List comments. If visibility is given, filter to that; else return all."""
    req_id = keys.resolve_request(db, request_id) or request_id
    stmt = select(RequestComment).where(RequestComment.request_id == req_id)
    if visibility:
        stmt = stmt.where(RequestComment.visibility == visibility)
    return list(db.scalars(stmt.order_by(RequestComment.created_at)).all())


def resolve_track_token(db: Session, token: str) -> Request | None:
    """PRD-43 D3: find the request for a tracking token (plaintext lookup via hash)."""
    h = hashlib.sha256(token.encode()).hexdigest()
    return db.scalar(select(Request).where(Request.track_token_hash == h))


def public_board(
    db: Session,
    project_id: str,
    *,
    types: list[str],
) -> list[Request]:
    """PRD-43 D5: published requests of given types for public boards."""
    stmt = (
        select(Request)
        .where(Request.project_id == project_id)
        .where(Request.published_at.isnot(None))
        .where(Request.type.in_(types))
        .order_by(Request.created_at.desc())
    )
    return list(db.scalars(stmt).all())


def serialize_public_row(req: Request, comments: list[RequestComment] | None = None) -> dict:
    """PRD-43 D5: allow-list serializer for public board rows. No secrets."""
    from app.schemas import PublicCommentOut, PublicBoardRow
    pub_comments = [c for c in (comments or []) if c.visibility == "public"]
    linked_status = None
    if req.linked_to:
        # Would need a session to fetch the item; caller should provide it.
        linked_status = None
    return PublicBoardRow(
        id=req.id,
        type=req.type,
        title=req.title,
        votes=req.votes,
        created_at=req.created_at,
        linked_status=linked_status,
        comments=[
            PublicCommentOut(id=c.id, body=c.body, created_at=c.created_at)
            for c in pub_comments
        ],
    ).model_dump()


def serialize_tracking(req: Request, comments: list[RequestComment] | None = None) -> dict:
    """PRD-43 D3: tracking page serializer. Allow-list — no secrets."""
    from app.schemas import PublicCommentOut, TrackingOut
    pub_comments = [c for c in (comments or []) if c.visibility == "public"]
    linked_status = None
    if req.linked_to and req.status == "linked":
        linked_status = None  # caller should enrich with item status
    return TrackingOut(
        title=req.title,
        type=req.type,
        status=req.status,
        linked_status=linked_status,
        votes=req.votes,
        comments=[
            PublicCommentOut(id=c.id, body=c.body, created_at=c.created_at)
            for c in pub_comments
        ],
    ).model_dump()
