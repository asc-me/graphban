"""Per-dev slice pull REST (PRD-P10 / GRPH-190).

Spoke-initiated: the local (OSS) AgentLedger instance authenticates to the hub
and pulls ONLY that dev's slice — tickets assigned to them. The spoke grounds
each in the local checkout and runs the in-app assistant.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security.deps import get_current_user
from app.services import slice_pull

logger = logging.getLogger("graphban.slice_pull")

router = APIRouter(prefix="/slice", tags=["slice"])


@router.get("/my")
def my_slice(
    project_id: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pull the authenticated user's slice from the hub mirror.

    Returns the mirrored issues assigned to this user. Under metadata_only
    governance, body fields (title, description) are dropped — the spoke
    must fetch them from the tracker directly.
    """
    if not project_id:
        raise HTTPException(422, "project_id is required")

    link = slice_pull.get_link_for_project(db, project_id=project_id)
    if link is None:
        raise HTTPException(404, "no tracker link for this project")

    # Use the user's id as the assignee filter. In a real deployment, this
    # would map from the Graphban user to the Linear assignee id via the
    # tracker link's identity mapping. For now, we use the user id directly.
    assignee_id = user.id

    issues = slice_pull.get_slice_with_governance(
        db, link=link, assignee_id=assignee_id,
    )
    return {
        "project_id": project_id,
        "assignee_id": assignee_id,
        "count": len(issues),
        "issues": issues,
        "storage_tier": link.storage_tier,
    }


@router.get("/count")
def slice_count(
    project_id: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Count the issues in the authenticated user's slice."""
    if not project_id:
        raise HTTPException(422, "project_id is required")

    link = slice_pull.get_link_for_project(db, project_id=project_id)
    if link is None:
        raise HTTPException(404, "no tracker link for this project")

    count = slice_pull.count_slice(db, link_id=link.id, assignee_id=user.id)
    return {"project_id": project_id, "assignee_id": user.id, "count": count}
