"""Feature/bug request (triage) service."""
from __future__ import annotations

import hashlib
from secrets import token_urlsafe

from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import Item, Request, RequestAction, RequestComment, RequestVote, User, utcnow
from app.services import email as email_svc
from app.services import events as events_svc
from app.services import items as items_svc
from app.services import keys

REQUEST_TYPES = ["bug", "feature", "enhancement", "feedback"]

# The triage queue (GET /requests/triage) is requests still `new`. `triaging` and
# `linked` have been dispositioned and are not "N new". shell_counts.triage_new
# counts this same status — do not copy the string into a second predicate.
UNTRIAGED_STATUS = "new"

# Where `accept` may land the new item (PRD-60 D6).
ACCEPT_STATUSES = ("backlog", "next")


def untriaged_clause():
    """SQL form of the triage queue. `is_untriaged` is the same rule on a row.

    A snoozed request is hidden only until its date: once `snoozed_until` has passed it is
    back in the queue without anything having to wake it."""
    return or_(
        Request.status == UNTRIAGED_STATUS,
        and_(Request.status == "snoozed", Request.snoozed_until <= utcnow()),
    )


def is_untriaged(req: Request) -> bool:
    if req.status == "snoozed" and req.snoozed_until is not None:
        return _aware(req.snoozed_until) <= utcnow()
    return req.status == UNTRIAGED_STATUS


def untriaged_count(db: Session, project_id: str) -> int:
    """How many requests the Triage view would list for this project. One COUNT."""
    n = db.scalar(
        select(func.count()).select_from(Request).where(
            Request.project_id == project_id,
            untriaged_clause(),
        )
    )
    return int(n or 0)


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
    # PRD-60 D9: where a public reply is mailed. Kept out of every public serializer.
    contact = email or (account or {}).get("email", "")
    if contact:
        meta["contact_email"] = contact
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


def accept_request(
    db: Session,
    request_id: str,
    *,
    reporter: dict | None = None,
    status: str = "backlog",
    prd_id: str | None = None,
    actor: User | None = None,
) -> tuple[Request, Item] | None:
    """Triage a request into tracked work: create the item and link it, atomically.

    Doing this as two calls from the client leaves a window where the item exists and the
    link failed — an orphan item plus a request still sitting in the queue, which reads as
    "nobody triaged this" while the work is already on the board. One commit, or neither.

    Idempotent: a request already linked returns its existing item rather than minting a
    second one, so a double-click cannot fork the same report into two pieces of work.

    `status` is where the item lands (`backlog` or `next`); `prd_id`, when given, links the
    item to that PRD.
    """
    if status not in ACCEPT_STATUSES:
        raise ValueError(f"accept status must be one of {', '.join(ACCEPT_STATUSES)}")
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
        status=status,
        prd_id=prd_id,
        commit=False,
    )
    prior = _state(req)
    req.linked_to = item.id
    req.status = "linked"
    db.flush()
    db.refresh(item)
    _log(db, req, "accept", prior, actor, data={"item_id": item.id, "item_updated_at": _iso(item.updated_at)},
         meta={"item": item.id, "status": status, "prd_id": prd_id})
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


def comment_role(c: RequestComment) -> str:
    """PRD-60 D9: submitter | team (public operator reply) | internal (private note)."""
    if c.author_kind == "submitter":
        return "submitter"
    return "team" if c.visibility == "public" else "internal"


def create_comment(
    db: Session,
    *,
    request_id: str,
    author_user_id: str | None,
    body: str,
    visibility: str = "private",
    author_kind: str = "operator",
) -> RequestComment:
    """PRD-43 D7: operator comment. Default private; must be explicitly tagged public.

    PRD-60 D9: a public operator reply notifies the submitter once; an internal note and a
    submitter's own comment notify no one (voters are never notified in v1). Every comment
    writes an event."""
    from app.services import keys as _keys
    req_id = _keys.resolve_request(db, request_id) or request_id
    if db.get(Request, req_id) is None:
        raise ValueError(f"request not found: {request_id}")
    if visibility not in ("private", "public"):
        raise ValueError(f"invalid visibility: {visibility}")
    if author_kind not in ("operator", "submitter"):
        raise ValueError(f"invalid author_kind: {author_kind}")
    if author_kind == "submitter":
        visibility = "public"  # a submitter cannot write an internal note
    elif author_user_id is None:
        raise ValueError("operator comment needs an author")
    if not (body or "").strip():
        raise ValueError("comment body is required")
    comment = RequestComment(
        id=f"rcom_{token_urlsafe(12)}",
        request_id=req_id,
        author_user_id=author_user_id,
        author_kind=author_kind,
        body=body,
        visibility=visibility,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)

    req = db.get(Request, req_id)
    role = comment_role(comment)
    notified = False
    if role == "team":
        notified = _notify_submitter(req, comment)
    events_svc.record(
        db,
        actor_type="user" if author_user_id else "system",
        actor_id=author_user_id or "",
        actor_label="submitter" if author_kind == "submitter" else "",
        surface="public" if author_kind == "submitter" else "rest",
        action="request_comment",
        target_type="request",
        target_id=req_id,
        project_id=req.project_id,
        meta={"comment_id": comment.id, "role": role, "notified": notified},
    )
    return comment


def _notify_submitter(req: Request, comment: RequestComment) -> bool:
    """Mail the submitter a public reply. No address -> no mail; the reply is still on the
    tracking page and the event records `notified: false`."""
    to = (req.meta or {}).get("contact_email", "")
    if not to:
        return False
    return email_svc.send_email(
        to,
        f"Update on your request: {req.title}",
        f"The team replied to your request \"{req.title}\":\n\n{comment.body}\n\n"
        "Open your tracking link to read the thread and reply.",
    )


def list_comment_thread(db: Session, request_id: str) -> list[dict]:
    """PRD-60 D9: operator thread — every comment with author display name and role."""
    out = []
    for c in list_comments(db, request_id):
        if c.author_kind == "submitter":
            req = db.get(Request, c.request_id)
            author = (req.by if req else "") or "Submitter"
        else:
            u = db.get(User, c.author_user_id) if c.author_user_id else None
            author = u.name if u else ""
        out.append({
            "id": c.id, "body": c.body, "visibility": c.visibility,
            "role": comment_role(c), "author": author, "created_at": c.created_at,
        })
    return out


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


# ---- PRD-60 D6: triage actions (accept / merge / decline / snooze) and undo ----

class ActionRefused(ValueError):
    """An action or undo that cannot proceed; the message is the reason shown to the user."""


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _iso(dt: datetime | None) -> str | None:
    return _aware(dt).isoformat() if dt is not None else None


def _state(req: Request) -> dict:
    """The request fields a triage action can change, JSON-safe."""
    return {
        "status": req.status,
        "linked_to": req.linked_to,
        "votes": req.votes,
        "snoozed_until": _iso(req.snoozed_until),
        "merged_into": req.merged_into,
    }


def _apply_state(req: Request, state: dict) -> None:
    req.status = state["status"]
    req.linked_to = state["linked_to"]
    req.votes = state["votes"]
    req.snoozed_until = datetime.fromisoformat(state["snoozed_until"]) if state["snoozed_until"] else None
    req.merged_into = state["merged_into"]


def _log(db: Session, req: Request, action: str, prior: dict, actor: User | None, *,
         data: dict | None = None, meta: dict | None = None) -> RequestAction:
    """Record the action for undo and write its one event row. Caller commits."""
    row = RequestAction(
        id=f"ract_{token_urlsafe(12)}", request_id=req.id, action=action,
        prior=prior, after=_state(req), data=data or {},
    )
    db.add(row)
    # events.record commits; flush first so the action row and the request change land together.
    db.flush()
    _event(db, actor, f"request_{action}", req, {"action_id": row.id, **(meta or {})})
    return row


def _event(db: Session, actor: User | None, action: str, req: Request, meta: dict) -> None:
    if actor is None:
        events_svc.record(db, actor_type="system", surface="rest", action=action,
                          target_type="request", target_id=req.key, project_id=req.project_id, meta=meta)
    else:
        events_svc.record_user(db, actor, action=action, target_type="request",
                               target_id=req.key, project_id=req.project_id, meta=meta)


def _get(db: Session, request_id: str) -> Request | None:
    return db.get(Request, keys.resolve_request(db, request_id) or request_id)


def _require_open(req: Request, action: str) -> None:
    if req.status in ("linked", "merged", "declined"):
        raise ActionRefused(f"cannot {action} a request that is already {req.status}; undo that first")


def _final_target(db: Session, source: Request, target_id: str) -> Request | Item:
    """Resolve a merge target, following merged requests to the end of the chain."""
    seen = {source.id}
    current: Request | Item | None = None
    nxt = target_id
    while True:
        rid = keys.resolve_request(db, nxt)
        current = db.get(Request, rid or nxt)
        if current is None:
            iid = keys.resolve_item(db, nxt)
            item = db.get(Item, iid or nxt)
            if item is None:
                raise ActionRefused(f"merge target not found: {target_id}")
            if item.project_id != source.project_id:
                raise ActionRefused("merge target is in a different project")
            return item
        if current.id in seen:
            raise ActionRefused("merge refused: it would create a cycle")
        seen.add(current.id)
        if current.status != "merged" or not current.merged_into:
            if current.project_id != source.project_id:
                raise ActionRefused("merge target is in a different project")
            return current
        nxt = current.merged_into


def merge_request(db: Session, request_id: str, target_id: str, *, actor: User | None = None) -> Request | None:
    """Merge a request into a duplicate item or request.

    Into a request: the voter rows move (deduplicated by voter_key) and the vote count becomes
    the union of the two voter sets. Into an item there are no votes to carry. A target that is
    itself merged is followed to the end of its chain; a chain leading back here is refused."""
    req = _get(db, request_id)
    if req is None:
        return None
    _require_open(req, "merge")
    target = _final_target(db, req, target_id)
    prior = _state(req)
    data: dict = {}
    if isinstance(target, Request):
        src_rows = list(db.scalars(select(RequestVote).where(RequestVote.request_id == req.id)))
        have = set(db.scalars(select(RequestVote.voter_key).where(RequestVote.request_id == target.id)))
        added = []
        for row in src_rows:
            if row.voter_key not in have:
                db.add(RequestVote(request_id=target.id, voter_key=row.voter_key, created_at=row.created_at))
                added.append(row.voter_key)
                have.add(row.voter_key)
        overlap = len(src_rows) - len(added)
        data = {
            "target_votes_before": target.votes,
            "source_rows": [[r.voter_key, _iso(r.created_at)] for r in src_rows],
            "added_keys": added,
        }
        for row in src_rows:
            db.delete(row)
        # Counts can exceed rows (operator votes carry no voter row); keep that surplus.
        target.votes = target.votes + req.votes - overlap
        req.votes = 0
        data["target_votes_after"] = target.votes
    req.status = "merged"
    req.merged_into = target.id
    data["target_id"] = target.id
    data["target_kind"] = "request" if isinstance(target, Request) else "item"
    db.flush()
    _log(db, req, "merge", prior, actor, data=data, meta={"target": target.key})
    db.commit()
    db.refresh(req)
    return req


def _notify_decline(req: Request, reason: str) -> bool:
    """Tell the submitter through the PRD-43 path: email when we hold an address, else the
    event row alone (the tracking page shows the `declined` status either way)."""
    addr = ((req.meta or {}).get("account") or {}).get("email") or (req.by if "@" in (req.by or "") else "")
    if not addr:
        return False
    text = f'Your request "{req.title}" was declined.'
    if reason:
        text += f"\n\nReason: {reason}"
    return email_svc.send_email(addr, f"Your request was declined: {req.title}", text)


def decline_request(db: Session, request_id: str, *, reason: str = "", actor: User | None = None) -> Request | None:
    req = _get(db, request_id)
    if req is None:
        return None
    _require_open(req, "decline")
    prior = _state(req)
    req.status = "declined"
    req.snoozed_until = None
    db.flush()
    notified = _notify_decline(req, reason)
    _log(db, req, "decline", prior, actor, meta={"reason": reason, "notified": notified})
    db.commit()
    db.refresh(req)
    return req


def snooze_request(db: Session, request_id: str, until: datetime, *, actor: User | None = None) -> Request | None:
    req = _get(db, request_id)
    if req is None:
        return None
    _require_open(req, "snooze")
    until = _aware(until)
    if until <= utcnow():
        raise ActionRefused("snooze date must be in the future")
    prior = _state(req)
    req.status = "snoozed"
    req.snoozed_until = until
    db.flush()
    _log(db, req, "snooze", prior, actor, meta={"until": _iso(until)})
    db.commit()
    db.refresh(req)
    return req


def undo_action(db: Session, request_id: str, action_id: str, *, actor: User | None = None) -> Request | None:
    """Reverse one action, restoring the request (and its votes / created item) exactly.

    Refused, with the reason, when the record has changed since the action — undo never
    overwrites someone else's edit."""
    req = _get(db, request_id)
    if req is None:
        return None
    row = db.get(RequestAction, action_id)
    if row is None or row.request_id != req.id:
        raise ActionRefused("action not found for this request")
    if row.undone_at is not None:
        raise ActionRefused("action was already undone")
    later = db.scalar(
        select(func.count()).select_from(RequestAction).where(
            RequestAction.request_id == req.id, RequestAction.undone_at.is_(None),
            RequestAction.created_at > row.created_at, RequestAction.id != row.id,
        )
    )
    if later:
        raise ActionRefused("a later action exists on this request; undo that first")
    if _state(req) != row.after:
        raise ActionRefused("the request was changed after this action; undo would overwrite that change")

    data = row.data or {}
    if row.action == "accept":
        item = db.get(Item, data["item_id"])
        if item is not None:
            if _iso(item.updated_at) != data["item_updated_at"]:
                raise ActionRefused("the created item was edited since; it is kept and the accept stays")
            others = db.scalar(
                select(func.count()).select_from(Request).where(
                    Request.linked_to == item.id, Request.id != req.id)
            )
            if others:
                raise ActionRefused("another request is linked to the created item; it is kept")
        _apply_state(req, row.prior)
        db.flush()
        if item is not None:
            db.delete(item)
    elif row.action == "merge" and data.get("target_kind") == "request":
        target = db.get(Request, data["target_id"])
        if target is None or target.votes != data["target_votes_after"]:
            raise ActionRefused("the merge target's votes changed since; undo would overwrite them")
        for key in data["added_keys"]:
            db.execute(RequestVote.__table__.delete().where(
                RequestVote.request_id == target.id, RequestVote.voter_key == key))
        target.votes = data["target_votes_before"]
        for key, ts in data["source_rows"]:
            db.add(RequestVote(request_id=req.id, voter_key=key, created_at=datetime.fromisoformat(ts)))
        _apply_state(req, row.prior)
    else:
        _apply_state(req, row.prior)
    row.undone_at = utcnow()
    db.flush()
    _event(db, actor, "request_undo", req, {"action_id": row.id, "undone": row.action})
    db.commit()
    db.refresh(req)
    return req


def merged_target_key(db: Session, req: Request) -> str | None:
    """The rendered key a merged request points at, for the board and tracking page."""
    if req.status != "merged" or not req.merged_into:
        return None
    target = db.get(Request, req.merged_into) or db.get(Item, req.merged_into)
    return target.key if target is not None else None
