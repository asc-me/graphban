"""Observe Live (PRD-33 D5). JWT only — same privacy posture as GET /fleet/presence."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import authz
from app.security.deps import get_current_user
from app.services import live as live_svc

router = APIRouter(prefix="/live", tags=["live"])


@router.get("")
def get_live(project_id: str, user: str | None = None,
             db: Session = Depends(get_db),
             current: User = Depends(get_current_user)):
    """Who is working this project, grouped by human.

    JWT only. `require_readable` is enough (D15). The router does not join
    (D5 / A12). Fail closed if the aggregation raises (D14).
    """
    authz.require_readable(db, current.id, project_id)
    try:
        return live_svc.board(db, project_id, user_filter=user, viewer_id=current.id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "live board could not be composed") from exc
