"""Observe Live (PRD-33 D5). JWT only — same privacy posture as GET /fleet/presence."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import authz
from app.security.deps import get_current_user
from app.services import agent_calls as calls_svc
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


@router.get("/{agent_id}/feed")
def get_live_feed(agent_id: str, project_id: str, limit: int = 50,
                  db: Session = Depends(get_db),
                  current: User = Depends(get_current_user)):
    """What one agent did, newest first (PRD-34 D6).

    JWT only, same posture as the board. 404 for an agent that is not on this project —
    the feed is a map of a person's agent onto queries and files, and a readable project
    is the boundary. `state: "never"` with no rows is the empty; the service cannot
    produce `rows: []` with `state: "ok"` (D11).
    """
    authz.require_readable(db, current.id, project_id)
    out = calls_svc.feed(db, project_id, agent_id, limit=limit)
    if out is None:
        raise HTTPException(404, "no such agent on this project")
    return out


@router.post("/sweep")
def sweep_live_feed(project_id: str,
                    db: Session = Depends(get_db),
                    current: User = Depends(get_current_user)):
    """Run the feed's retention sweep by hand (PRD-34 D18) — the same code path the
    amortised sweep uses, for when `/health` says it has been failing. Writable
    membership on the project, which is the closest thing the project plane has to
    "admin"; raising this to org admin would lock the self-host single user out of
    their own maintenance. Raises rather than counting: the caller asked and wants
    the error."""
    authz.require_writable(db, current.id, project_id)
    return {"project_id": project_id, "deleted": calls_svc.sweep(db, project_id),
            "retention_days": calls_svc.retention_days()}
