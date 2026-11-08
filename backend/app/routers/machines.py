"""`/api/machines` — reports from `gban setup` / `gban doctor` and Settings → Machines (GRPH-1442)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import authz
from app.security.deps import get_current_user
from app.services import machines as svc

router = APIRouter(prefix="/machines", tags=["machines"])


class LineIn(BaseModel):
    status: str
    name: str = ""
    detail: str = ""


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_id: str
    label: str = ""
    os: str = ""
    gban_version: str = ""
    gbfleet_version: str = ""
    harnesses: list[str] = Field(default_factory=list)
    project: str = ""
    lines: list[LineIn]


@router.post("/report")
def post_report(body: ReportIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if body.project:
        authz.require_readable(db, user.id, body.project)
    try:
        m = svc.report(db, user, **{**body.model_dump(), "lines": [l.model_dump() for l in body.lines]})
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return svc.view(m)


@router.get("")
def list_machines(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return svc.list_for(db, user)


@router.delete("/{machine_row_id}", status_code=204)
def remove_machine(machine_row_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    if not svc.remove(db, user, machine_row_id):
        raise HTTPException(404, "machine not found")
