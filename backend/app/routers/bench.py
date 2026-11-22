"""Baseline bench runs, for an agent key (GRPH-P98 §4 and §6, GRPH-1856)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import authz
from app.security.deps import get_agent_key
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
                 key=Depends(get_agent_key)):
    pid = _project(key, project_id, db)
    return {"results": bench_svc.list_results(db, pid, vendor=vendor, model=model)}
