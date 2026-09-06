"""The Harness page's server half (PRD-38 PR 2).

One read that answers the page's whole question — how each vendor x model x lane x tier x task
class x size band has actually turned out, week by week, with the sample count, the sampling
skew and the cost proxy attached to every number.

Session-authenticated, like the Fleet view beside it: the caller is a person deciding whether
to change a preference, not an agent working inside one. The supervisor's own view of the same
facts is `fleet_status.measured`, which it already reads, and `gbfleet doctor` prints.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import authz
from app.security.deps import get_current_user
from app.services import harness as harness_svc

router = APIRouter(prefix="/harness", tags=["harness"])


@router.get("")
def harness_report(project_id: str, window_days: int | None = None,
                   versions: str = Query("current", pattern="^(current|all)$"),
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Every cell in the window, with its weekly series.

    `versions=current` (the default) keeps the newest binary version per vendor+model and names
    the rest in `versions_seen`; `versions=all` returns every version as its own cell, because
    two versions of one harness are two things and pooling them would hide a regression inside
    an average.
    """
    authz.require_readable(db, user.id, project_id)
    if window_days is not None and (window_days < 1 or window_days > 1000):
        raise HTTPException(422, "window_days must be between 1 and 1000")
    return harness_svc.report(db, project_id, window_days=window_days, versions=versions)
