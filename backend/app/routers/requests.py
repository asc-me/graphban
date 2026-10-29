from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Request, RequestAction, User
from app.schemas import (
    DuplicateHintOut,
    RequestAcceptIn,
    RequestAcceptOut,
    RequestCreate,
    RequestDeclineIn,
    RequestLinkIn,
    RequestMergeIn,
    RequestOut,
    RequestSnoozeIn,
    RequestUndoIn,
    RequestVoteIn,
    TriageRequestOut,
)
from app.schemas import ItemOut
from app.security import authz
from app.security.deps import get_current_user
from app.services import duplicates as dup_svc
from app.services import keys
from app.services import requests as req_svc

router = APIRouter(prefix="/requests", tags=["requests"])


@router.get("", response_model=list[RequestOut])
def list_requests(
    project_id: str | None = None,
    type: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_readable(db, user.id, project_id)
    return req_svc.list_requests(db, project_id=project_id, type_=type)


@router.post("", response_model=RequestOut, status_code=201)
def create_request(body: RequestCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, body.project_id)
    try:
        return req_svc.create_request(
            db, type_=body.type, title=body.title, detail=body.detail, by=body.by,
            project_id=body.project_id,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/{request_id}/comments")
def list_request_comments(request_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """PRD-60 D9: the operator thread — public and internal comments, with author and role."""
    req = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if req is None:
        raise HTTPException(404, "request not found")
    authz.require_readable(db, user.id, req.project_id)
    return req_svc.list_comment_thread(db, req.id)


@router.post("/{request_id}/vote", response_model=RequestOut)
def vote(request_id: str, body: RequestVoteIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    existing = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if existing is None:
        raise HTTPException(404, "request not found")
    authz.require_writable(db, user.id, existing.project_id, "request")
    req = req_svc.vote_request(db, request_id, body.delta)
    if req is None:
        raise HTTPException(404, "request not found")
    return req


@router.post("/{request_id}/link", response_model=RequestOut)
def link(request_id: str, body: RequestLinkIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    existing = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if existing is None:
        raise HTTPException(404, "request not found")
    authz.require_writable(db, user.id, existing.project_id, "request")
    try:
        req = req_svc.link_request(db, request_id, body.item_id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if req is None:
        raise HTTPException(404, "request not found")
    return req


@router.get("/triage", response_model=list[TriageRequestOut])
def triage_queue(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """What is waiting to be triaged, each row carrying its closest duplicate.

    Only `new` requests: once linked to an item the report has been dispositioned and
    belongs to the tracker, not the queue. An empty queue therefore means "everything
    reported has been looked at" — which is why the client says that rather than "no
    requests", a sentence that would also be true of a project nobody has ever reported
    against.

    The duplicate is advisory and never applied. Two people reporting the same bug is
    the normal case, and deciding they are the same report is a human call.

    **Cost, stated rather than discovered:** `find_duplicates` embeds every request and
    item in the project on each call, so this is O(queue x corpus) embeddings per load.
    With the offline stub that is free and at beta scale it is fine; with a real provider
    and a long queue it is not. The fix is persisted embeddings rather than a cap here —
    capping would make some rows report `duplicate: null` without having compared, which
    is precisely the reading this field is shaped to prevent.
    """
    authz.require_readable(db, user.id, project_id)
    out: list[TriageRequestOut] = []
    for req in req_svc.list_requests(db, project_id=project_id):
        if not req_svc.is_untriaged(req):
            continue
        hits = dup_svc.find_duplicates(
            db, req.title, project_id=project_id, exclude_request_id=req.id, top_k=1
        )
        best = hits[0] if hits else None
        out.append(TriageRequestOut(
            request=RequestOut.model_validate(req),
            duplicate=DuplicateHintOut(
                kind=best["kind"], id=best["id"], title=best["title"], score=best["score"],
            ) if best else None,
        ))
    return out


def _writable(db: Session, user: User, request_id: str) -> Request:
    existing = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if existing is None:
        raise HTTPException(404, "request not found")
    authz.require_writable(db, user.id, existing.project_id, "request")
    return existing


def _out(db: Session, req: Request, action_id: str | None = None) -> RequestOut:
    out = RequestOut.model_validate(req)
    out.merged_into = req_svc.merged_target_key(db, req)
    out.action_id = action_id
    return out


def _last_action_id(db: Session, req: Request) -> str | None:
    return db.scalar(
        select(RequestAction.id).where(RequestAction.request_id == req.id)
        .order_by(RequestAction.created_at.desc()).limit(1)
    )


@router.post("/{request_id}/accept", response_model=RequestAcceptOut)
def accept(
    request_id: str,
    body: RequestAcceptIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Triage a request into tracked work — one transaction, not two calls.

    `status` (`backlog` default, or `next`) is where the item lands; `prd_id` optionally
    links it to a PRD. Idempotent: accepting an already-linked request returns its existing
    item instead of minting a second, so a double-click cannot fork one report into two
    pieces of work.
    """
    _writable(db, user, request_id)
    body = body or RequestAcceptIn()
    try:
        result = req_svc.accept_request(
            db, request_id, reporter={"name": user.name, "handle": user.handle, "avatar": user.avatar},
            status=body.status, prd_id=body.prd_id, actor=user,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    if result is None:
        raise HTTPException(404, "request not found")
    req, item = result
    return RequestAcceptOut(
        request=_out(db, req), item=ItemOut.model_validate(item), action_id=_last_action_id(db, req),
    )


def _action(db: Session, fn, *args, **kw) -> Request:
    try:
        req = fn(db, *args, **kw)
    except req_svc.ActionRefused as e:
        raise HTTPException(409, str(e))
    if req is None:
        raise HTTPException(404, "request not found")
    return req


@router.post("/{request_id}/merge", response_model=RequestOut)
def merge(request_id: str, body: RequestMergeIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Merge into a duplicate item or request; votes carry over (deduplicated by voter)."""
    _writable(db, user, request_id)
    req = _action(db, req_svc.merge_request, request_id, body.target_id, actor=user)
    return _out(db, req, _last_action_id(db, req))


@router.post("/{request_id}/decline", response_model=RequestOut)
def decline(request_id: str, body: RequestDeclineIn | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _writable(db, user, request_id)
    req = _action(db, req_svc.decline_request, request_id, reason=(body.reason if body else ""), actor=user)
    return _out(db, req, _last_action_id(db, req))


@router.post("/{request_id}/snooze", response_model=RequestOut)
def snooze(request_id: str, body: RequestSnoozeIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _writable(db, user, request_id)
    req = _action(db, req_svc.snooze_request, request_id, body.until, actor=user)
    return _out(db, req, _last_action_id(db, req))


@router.post("/{request_id}/undo", response_model=RequestOut)
def undo(request_id: str, body: RequestUndoIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Reverse an action. 409 with the reason when the record changed since."""
    _writable(db, user, request_id)
    req = _action(db, req_svc.undo_action, request_id, body.action_id, actor=user)
    return _out(db, req)
