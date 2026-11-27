"""Human oversight of PRD links (GRPH-P104 S5): the org view and the acts a person takes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security.deps import get_current_user
from app.services import links as links_svc
from app.services import oversight as oversight_svc
from app.services import suggestions as suggestions_svc
from app.services import threads as threads_svc

router = APIRouter(tags=["oversight"])


def _v(fn, *args, **kwargs):
    """A service's ValueError is the caller's mistake (a blank reason, a read-only thread)."""
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class Reason(BaseModel):
    reason: str = ""


class Answer(BaseModel):
    body: str


class StaleAge(BaseModel):
    hours: int


@router.get("/orgs/{org_id}/link-oversight")
def link_oversight(org_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    return oversight_svc.overview(db, user, org_id)


@router.put("/orgs/{org_id}/link-oversight/stale-age")
def set_stale_age(org_id: str, body: StaleAge, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    return {"stale_after_hours": _v(oversight_svc.set_stale_hours, db, user, org_id, body.hours)}


@router.post("/prd-links/{link_id}/accept")
def accept_link(link_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    link = links_svc.accept_prd_link(db, oversight_svc.caller_for(db, user), link_id)
    return {"status": link.status}


@router.post("/prd-links/{link_id}/decline")
def decline_link(link_id: str, body: Reason, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    link = _v(links_svc.decline_prd_link, db, oversight_svc.caller_for(db, user), link_id,
             reason=body.reason)
    return {"status": link.status}


@router.post("/prd-links/{link_id}/messages/{message_id}/answer")
def answer_question(link_id: str, message_id: int, body: Answer, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    return _v(threads_svc.post, db, oversight_svc.caller_for(db, user), link_id, kind="note",
                 body=body.body, reply_to=message_id)


@router.post("/prd-links/{link_id}/messages/{message_id}/file")
def file_suggestion(link_id: str, message_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    return suggestions_svc.file_suggestion(db, oversight_svc.caller_for(db, user), link_id,
                                           message_id)


@router.post("/prd-links/{link_id}/messages/{message_id}/decline")
def decline_suggestion(link_id: str, message_id: int, body: Reason, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    return _v(suggestions_svc.decline_suggestion, db, oversight_svc.caller_for(db, user),
             link_id, message_id, reason=body.reason)
