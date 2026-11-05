"""The Spec reader's REST surface (GRPH-1319 / GRPH-P66).

Thin: reads come from `services/specs.py`, writes from the services that own each record —
`design_sets.put_preamble`, `design_sets.accept_set_constraints`, `design_sets.compile_prompts`.
Until this router the preamble, set constraints and design sets were reachable only over MCP.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import errors
from app.db import get_db
from app.models import DesignSet, User
from app.security import authz
from app.security.deps import get_current_user
from app.services import design_sets as design_svc
from app.services import specs as specs_svc

router = APIRouter(prefix="/specs", tags=["specs"])


def _call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except errors.NotFound as e:
        raise HTTPException(404, str(e)) from e
    except errors.Validation as e:
        raise HTTPException(422, str(e) + (f" — {e.hint}" if e.hint else "")) from e


def _set_project(db: Session, user: User, set_id: str, write: bool) -> str:
    ds = db.get(DesignSet, set_id)
    if ds is None:
        raise HTTPException(404, "design set not found")
    (authz.require_writable if write else authz.require_readable)(db, user.id, ds.project_id)
    return ds.project_id


def _person(user: User) -> str:
    return (user.name or user.email or user.handle).strip()


@router.get("/prds/{prd_id}")
def prd_facts(prd_id: str, project_id: str, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id, "prd")
    return _call(specs_svc.prd_facts, db, project_id, prd_id)


@router.get("/preamble")
def get_preamble(project_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    out = specs_svc.preamble_view(db, project_id)
    if out is None:
        raise HTTPException(404, "no preamble in this project")
    return out


class PreambleIn(BaseModel):
    project_id: str
    body: str
    cites_prd_id: str


@router.put("/preamble")
def put_preamble(body: PreambleIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, body.project_id)
    # A signed-in person is stamped `user:<id>`, the repo's provenance for a person. Not
    # `key:` — that names an API-key credential, and `key:<name>` resolves by key name.
    _call(design_svc.put_preamble, db, body.project_id, body.body, body.cites_prd_id,
          created_by=f"user:{user.id}")
    return specs_svc.preamble_view(db, body.project_id)


@router.get("/design-sets/{set_id}")
def get_design_set(set_id: str, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    pid = _set_project(db, user, set_id, write=False)
    return _call(specs_svc.design_set_view, db, pid, set_id)


@router.post("/design-sets/{set_id}/accept-constraints")
def accept_constraints(set_id: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """The person in this session accepts the constraints as written."""
    pid = _set_project(db, user, set_id, write=True)
    _call(design_svc.accept_set_constraints, db, pid, set_id, f"user:{user.id}", _person(user))
    return specs_svc.design_set_view(db, pid, set_id)


class CompileIn(BaseModel):
    screen: str | None = None


@router.post("/design-sets/{set_id}/compile")
def compile_prompts(set_id: str, body: CompileIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    pid = _set_project(db, user, set_id, write=True)
    _call(design_svc.compile_prompts, db, pid, set_id, screen=body.screen,
          actor=f"user:{user.id}")
    return specs_svc.design_set_view(db, pid, set_id)
