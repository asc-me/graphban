"""Driving the learning loop over HTTP (GRPH-353 / PRD-16).

Separate from `routers/artifacts.py`, which is the *review* surface a person clicks through.
This is the *operational* one: something on a timer asks the instance to go and look for new
lessons. They authenticate differently for that reason.

**API key, not a session.** The intended caller is cron, a systemd timer, a launchd plist or
a hosted scheduler — none of which can hold a browser session or refresh a 30-minute access
token. Same reasoning as `/artifacts/{id}/used`, which authenticates a generated hook the
same way. A person who wants to trigger a run by hand mints a key like any other agent.

There is deliberately no GET that runs anything. A loop that ingests on a read is a loop
something's link-prefetcher eventually triggers.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey
from app.security import authz
from app.security.deps import get_agent_key
from app.services import learning as learning_svc

router = APIRouter(prefix="/learning", tags=["learning"])


class RunIn(BaseModel):
    # `all` runs both stages. That is the right default for a nightly timer: the two are
    # either side of a human triage step, so the artifact stage simply finds nothing on the
    # nights nobody reviewed anything, at the cost of one query.
    stage: str = "all"
    project_id: str | None = None
    # A first run over a large transcript archive can be thousands of sources. Present so an
    # operator can take a bite out of it and watch what lands before turning the timer on.
    limit_sources: int | None = None


@router.post("/run")
def run_loop(body: RunIn, db: Session = Depends(get_db),
             key: ApiKey = Depends(get_agent_key)):
    """Run a stage of the loop and report what it did.

    Returns counts rather than the shards or recommendations themselves — a first run over a
    real transcript set produces thousands, and a scheduler that wanted them would be holding
    the whole corpus in memory to log a number.
    """
    project_id = body.project_id or key.project_id or "core"
    # Writable, not readable: this stage creates shards and recommendations in the project.
    authz.require_writable(db, key.user_id, project_id)
    try:
        return learning_svc.run(db, stage=body.stage, project_id=project_id,
                                limit_sources=body.limit_sources)
    except learning_svc.UnknownStage as e:
        raise HTTPException(422, str(e))
