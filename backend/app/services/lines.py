"""The maintenance line (GRPH-1681): at most one per project, identity only.

A line is a row with a frozen id, a per-project number and a title. It has no body, status,
version, grill, baseline or close, so nothing that reads a PRD (`coverage`, `completeness`,
`scope_drift`, `close_prd`) can read it, and `get_prd` on its key is not found: the key
renders with its own kind letter (`<TAG>-L<n>`). Retagging a project leaves the stored id
alone, exactly as for an item (PRD-13).

Nothing creates the line implicitly. A create, an accept or a drain that needs a line and
finds none is refused, and `NO_LINE` is the sentence it refuses with.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import errors
from app.errors import Refused
from app.models import Line, Project
from app.services import keys

DEFAULT_TITLE = "Maintenance"
#: The registry rule (`services/rules.py`) a second create is refused under.
ONE_LINE_RULE = "line.one_per_project"
NO_LINE = "this project has no maintenance line — create it with `create_line` first"


def _refuse_second(existing: Line) -> None:
    raise Refused(
        409, f"this project already has a maintenance line: {existing.key}",
        rule_id=ONE_LINE_RULE,
        hint=f"use {existing.key}; a project has at most one line")


def get_line(db: Session, project_id: str) -> Line | None:
    return db.scalar(select(Line).where(Line.project_id == project_id))


def create_line(db: Session, project_id: str, title: str | None = None,
                created_by: str | None = None) -> Line:
    """Create the project's one line, or refuse naming the existing one."""
    if db.get(Project, project_id) is None:
        raise errors.NotFound(f"unknown project: {project_id!r}")
    title = (title or "").strip() or DEFAULT_TITLE
    existing = get_line(db, project_id)
    if existing is not None:
        _refuse_second(existing)
    line_id, number = keys.mint(db, project_id, "line")
    line = Line(id=line_id, project_id=project_id, number=number, title=title,
                created_by=created_by)
    db.add(line)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with another creator: the unique project_id is the backstop.
        db.rollback()
        existing = get_line(db, project_id)
        if existing is None:
            raise
        _refuse_second(existing)
    db.refresh(line)
    return line


def line_dict(line: Line) -> dict:
    return {"id": line.key, "title": line.title}


def context_field(db: Session, project_id: str | None) -> dict | None:
    """`get_context.maintenance_line`. None means the line has not been created — it does
    NOT mean the project has no bugs."""
    if not project_id:
        return None
    line = get_line(db, project_id)
    return line_dict(line) if line is not None else None


def resolve_for_item(db: Session, line_id: str, project_id: str) -> str:
    """The frozen id a caller's line key means, which must be this project's line."""
    stored = keys.resolve_line(db, line_id)
    line = db.get(Line, stored) if stored else None
    if line is None:
        raise ValueError(f"unknown line: {line_id!r} — {NO_LINE}")
    if line.project_id != project_id:
        raise ValueError(f"{line_id} belongs to another project, not {project_id!r}")
    return line.id
