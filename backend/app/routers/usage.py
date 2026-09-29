"""Deployment-wide usage aggregate (PRD-47 S14 / GRPH-965)."""
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security.deps import get_current_user
from app.services import usage as usage_svc

router = APIRouter(tags=["usage"])


@router.get("/usage")
def usage(
    range_days: int = 30,
    format: str = "json",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One aggregate for the Usage page. ``range_days`` is 7, 30, or 90.
    ``format=csv`` exports the by-project table only."""
    payload = usage_svc.aggregate(db, user.id, range_days=range_days)
    if format == "csv":
        return PlainTextResponse(
            usage_svc.to_csv(payload),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="usage-by-project.csv"'},
        )
    return payload
