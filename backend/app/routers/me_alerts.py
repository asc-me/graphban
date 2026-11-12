"""The signed-in user's alerts (GRPH-1527)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security.deps import get_current_user
from app.services import alerts as alerts_svc
from app.services import push as push_svc

router = APIRouter(tags=["alerts"])


@router.get("/me/alerts")
def my_alerts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return alerts_svc.list_for_user(db, user.id)


@router.post("/me/alerts/{alert_id}/read")
def read_alert(alert_id: str, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    if not alerts_svc.mark_read(db, user.id, alert_id):
        raise HTTPException(404, "no such alert")
    return alerts_svc.list_for_user(db, user.id)


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeIn(BaseModel):
    endpoint: str
    keys: PushKeys


class PushUnsubscribeIn(BaseModel):
    endpoint: str


@router.get("/me/push")
def push_status(user: User = Depends(get_current_user)):
    return push_svc.status()


@router.post("/me/push/subscribe")
def push_subscribe(body: PushSubscribeIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    if not push_svc.configured():
        raise HTTPException(503, "browser notifications are not set up on this deployment")
    push_svc.subscribe(db, user.id, body.endpoint, body.keys.p256dh, body.keys.auth)
    return push_svc.status()


@router.post("/me/push/unsubscribe")
def push_unsubscribe(body: PushUnsubscribeIn, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    push_svc.unsubscribe(db, user.id, body.endpoint)
    return push_svc.status()
