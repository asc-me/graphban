"""Baseline bench runs, for an agent key (GRPH-P98 §4 and §6, GRPH-1856)."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import authz
from app.models import User
from app.security.deps import get_agent_key, get_user_or_agent_key
from app.services import bench as bench_svc
from app.services.harness import AttemptRefused

router = APIRouter(prefix="/bench", tags=["bench"])


def _project(key, project_id: str | None, db: Session) -> str:
    pid = project_id or getattr(key, "project_id", None)
    if not pid:
        raise HTTPException(422, "name a project_id")
    authz.require_writable(db, key.user_id, pid)
    return pid


@router.post("/runs")
def open_run(body: dict, project_id: str | None = None, db: Session = Depends(get_db),
             key=Depends(get_agent_key)):
    """Open a bench run: a scratch project and one bound seat per task."""
    pid = _project(key, project_id, db)
    try:
        out = bench_svc.open_run(db, project_id=pid, user_id=key.user_id, body=body)
    except AttemptRefused as e:
        raise HTTPException(e.status, str(e))
    db.commit()
    return out


@router.post("/seat")
def task_seat(body: dict, project_id: str | None = None, db: Session = Depends(get_db),
              key=Depends(get_agent_key)):
    """A fresh seat for one task of an open run, minted as the task starts (GRPH-1888)."""
    pid = _project(key, project_id, db)
    try:
        out = bench_svc.issue_task_seat(db, project_id=pid, body=body)
    except AttemptRefused as e:
        raise HTTPException(e.status, str(e))
    db.commit()
    return out


@router.post("/results", status_code=201)
def post_result(body: dict, project_id: str | None = None, db: Session = Depends(get_db),
                key=Depends(get_agent_key)):
    """Store one baseline record. Never touches telemetry or a grid rate."""
    pid = _project(key, project_id, db)
    try:
        out = bench_svc.post_result(db, project_id=pid, body=body)
    except AttemptRefused as e:
        raise HTTPException(e.status, str(e))
    db.commit()
    return out


@router.get("/results")
def list_results(project_id: str | None = None, vendor: str | None = None,
                 model: str | None = None, db: Session = Depends(get_db),
                 principal=Depends(get_user_or_agent_key)):
    """A read a person makes too: baselines are shown beside the grid (P98 D11)."""
    user_id = principal.id if isinstance(principal, User) else principal.user_id
    pid = project_id or getattr(principal, "project_id", None)
    authz.require_readable(db, user_id, pid)
    return {"results": bench_svc.list_results(db, pid, vendor=vendor, model=model)}


def _suite_write(fn, body: dict, project_id: str | None, db: Session, key):
    pid = _project(key, project_id, db)
    try:
        out = fn(db, project_id=pid, body=body)
    except bench_svc.SuiteConflict as e:
        db.rollback()
        return JSONResponse(status_code=409, content={
            "error": "conflict", "code": e.code, "detail": str(e), "hint": e.hint,
            "current_version": e.current_version})
    except AttemptRefused as e:
        db.rollback()
        raise HTTPException(e.status, str(e))
    db.commit()
    return out


@router.get("/suite")
def get_suite(project_id: str | None = None, db: Session = Depends(get_db),
              principal=Depends(get_user_or_agent_key)):
    """The repo suite: its current version and tasks (GRPH-P98 §3)."""
    user_id = principal.id if isinstance(principal, User) else principal.user_id
    pid = project_id or getattr(principal, "project_id", None)
    authz.require_readable(db, user_id, pid)
    return bench_svc.get_suite(db, pid)


@router.post("/suite/adopt")
def adopt(body: dict, project_id: str | None = None, db: Session = Depends(get_db),
          key=Depends(get_agent_key)):
    """Adopt tasks; `expected_version` must match or 409 with the current version."""
    return _suite_write(bench_svc.adopt_tasks, body, project_id, db, key)


@router.post("/suite/remove")
def remove(body: dict, project_id: str | None = None, db: Session = Depends(get_db),
           key=Depends(get_agent_key)):
    """Remove tasks by id; `expected_version` must match or 409 with the current version."""
    return _suite_write(bench_svc.remove_tasks, body, project_id, db, key)
