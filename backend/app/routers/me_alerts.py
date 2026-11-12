"""The signed-in user's alerts (GRPH-1527)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security.deps import get_current_user
from app.services import alerts as alerts_svc

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
