"""The maintenance line REST surface (GRPH-1681).

Create takes the permission that accepts a request (`require_writable`), so anyone who can
triage can open the line. There is no update and no delete: a line never becomes a PRD.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import errors
from app.db import get_db
from app.models import User
from app.security import authz
from app.security.deps import get_current_user
from app.services import lines as lines_svc

router = APIRouter(prefix="/projects/{project_id}/line", tags=["lines"])


class LineCreate(BaseModel):
    title: str | None = None


class LineOut(BaseModel):
    id: str
    title: str


@router.get("", response_model=LineOut | None)
def get_line(project_id: str, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id, "project")
    line = lines_svc.get_line(db, project_id)
    return lines_svc.line_dict(line) if line else None


@router.post("", response_model=LineOut, status_code=201)
def create_line(project_id: str, body: LineCreate | None = None,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, project_id, "project")
    try:
        line = lines_svc.create_line(
            db, project_id, (body.title if body else None), created_by=user.id)
    except errors.NotFound as e:
        raise HTTPException(404, str(e))
    return lines_svc.line_dict(line)


class UnscopedOut(BaseModel):
    ids: list[str]
    count: int


@router.get("/unscoped_fixes", response_model=UnscopedOut)
def unscoped_fixes(project_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """GRPH-1686: fixes with no PRD and no line. Nothing moves them; setting line_id does."""
    authz.require_readable(db, user.id, project_id, "project")
    return lines_svc.unscoped_fixes(db, project_id)
