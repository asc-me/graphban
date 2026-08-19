from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Request, User
from app.schemas import (
    DuplicateHintOut,
    RequestAcceptOut,
    RequestCreate,
    RequestLinkIn,
    RequestOut,
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
        if req.status != "new":
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


@router.post("/{request_id}/accept", response_model=RequestAcceptOut)
def accept(request_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Triage a request into tracked work — one transaction, not two calls.

    Idempotent: accepting an already-linked request returns its existing item instead of
    minting a second, so a double-click cannot fork one report into two pieces of work.
    """
    existing = db.get(Request, keys.resolve_request(db, request_id) or request_id)
    if existing is None:
        raise HTTPException(404, "request not found")
    authz.require_writable(db, user.id, existing.project_id, "request")
    result = req_svc.accept_request(
        db, request_id, reporter={"name": user.name, "handle": user.handle, "avatar": user.avatar}
    )
    if result is None:
        raise HTTPException(404, "request not found")
    req, item = result
    return RequestAcceptOut(request=RequestOut.model_validate(req), item=ItemOut.model_validate(item))
