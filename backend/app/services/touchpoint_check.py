"""Warn when a declared touchpoint matches nothing in the project's code graph (GRPH-1393).

Items were filed with touchpoints that do not exist (`backend/app/models.py` when models is a
package), and the fleet's PR gate then refused the build for "changed files outside every
declared touchpoint". Nothing checked the declaration when it was written; this does.

A WARNING, never a refusal: a new file legitimately names a path the graph has not seen yet.
A project with no described files has nothing to judge against, so it warns about nothing.
"""

from __future__ import annotations

import difflib
import fnmatch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CodeNode


def _file_paths(db: Session, project_id: str) -> list[str]:
    rows = db.scalars(select(CodeNode.path).where(CodeNode.project_id == project_id)).all()
    return sorted({p.split("::", 1)[0] for p in rows if p})


def _matches(area: str, path: str) -> bool:
    a = area.strip().rstrip("/")
    return (a == path or path.startswith(a + "/")
            or (any(c in a for c in "*?[") and fnmatch.fnmatch(path, a)))


def closest(area: str, paths: list[str]) -> str | None:
    """The existing path nearest `area`: a package for a module name, else the best fuzzy match."""
    a = area.strip().rstrip("/")
    if a.endswith(".py"):
        stem = a[:-3] + "/"
        pkg = [p for p in paths if p.startswith(stem)]
        if pkg:
            return next((p for p in pkg if p.endswith("/__init__.py")), pkg[0])
    got = difflib.get_close_matches(a, paths, n=1, cutoff=0.0)
    return got[0] if got else None


def warnings(db: Session, project_id: str | None, touchpoints: list[str] | None) -> list[dict]:
    if not project_id or not touchpoints:
        return []
    paths = _file_paths(db, project_id)
    if not paths:
        return []
    out = []
    for tp in touchpoints:
        if not (tp or "").strip() or any(_matches(tp, p) for p in paths):
            continue
        near = closest(tp, paths)
        out.append({
            "touchpoint": tp,
            "closest": near,
            "message": f"touchpoint {tp!r} matches nothing in the code graph"
                       + (f"; closest existing path: {near}" if near else ""),
        })
    return out
