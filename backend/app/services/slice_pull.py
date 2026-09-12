"""Per-dev slice pull (PRD-P10 / GRPH-190).

The local (OSS) AgentLedger instance authenticates to the hub and pulls ONLY
that dev's slice — tickets assigned to them. The spoke grounds each in the
local checkout (code-map, memory, embeddings), runs the in-app assistant, and
pushes status/comments back through the hub.

Slice definition (settled in grill v1.0): after an accepted board claim, Linear
assignee is "mine" and that is what the spoke pulls. Unaccepted claim is
Graphban-only intent — not written to Linear, not visible to other spokes.

Offline: the local instance keeps working on the last-known slice. Do not
refuse to load tickets because the hub is unreachable. Tracker-owned field
edits made locally while offline are discarded on reconnect (tracker wins).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.models import TrackerMirror, TrackerLink
from app.services.governance import filter_mirror_data, is_metadata_only

logger = logging.getLogger("graphban.slice_pull")


@dataclass
class SliceIssue:
    """A mirrored issue in the dev's slice."""
    issue_id: str
    identifier: str
    title: str | None
    description: str | None
    canonical_status: str
    assignee_id: str | None
    assignee_name: str
    labels: list[str]
    tracker_updated_at: str
    url: str
    grounded: bool = False


def get_slice(
    db: Session,
    *,
    link_id: str,
    assignee_id: str,
) -> list[SliceIssue]:
    """Return the mirrored issues assigned to a specific dev.

    The slice is the set of TrackerMirror rows for the given link where
    assignee_id matches. This is the spoke's view of "my tickets".
    """
    stmt = select(TrackerMirror).where(
        and_(
            TrackerMirror.link_id == link_id,
            TrackerMirror.assignee_id == assignee_id,
        )
    ).order_by(TrackerMirror.mirrored_at.desc())
    rows = db.scalars(stmt).all()
    return [
        SliceIssue(
            issue_id=r.issue_id,
            identifier=r.identifier,
            title=r.title,
            description=r.description,
            canonical_status=r.canonical_status,
            assignee_id=r.assignee_id,
            assignee_name=r.assignee_name,
            labels=r.labels or [],
            tracker_updated_at=r.tracker_updated_at,
            url=r.url,
        )
        for r in rows
    ]


def get_slice_with_governance(
    db: Session,
    *,
    link: TrackerLink,
    assignee_id: str,
) -> list[dict]:
    """Return the slice with governance filtering applied.

    Under metadata_only, body fields (title, description) are dropped from
    the response — the spoke must fetch them from the tracker directly.
    """
    stmt = select(TrackerMirror).where(
        and_(
            TrackerMirror.link_id == link.id,
            TrackerMirror.assignee_id == assignee_id,
        )
    ).order_by(TrackerMirror.mirrored_at.desc())
    rows = db.scalars(stmt).all()

    result = []
    for r in rows:
        data = {
            "issue_id": r.issue_id,
            "identifier": r.identifier,
            "title": r.title,
            "description": r.description,
            "canonical_status": r.canonical_status,
            "assignee_id": r.assignee_id,
            "assignee_name": r.assignee_name,
            "labels": r.labels or [],
            "tracker_updated_at": r.tracker_updated_at,
            "url": r.url,
        }
        filtered = filter_mirror_data(link, data)
        result.append(filtered)
    return result


def count_slice(db: Session, *, link_id: str, assignee_id: str) -> int:
    """Count the issues in a dev's slice."""
    stmt = select(TrackerMirror).where(
        and_(
            TrackerMirror.link_id == link_id,
            TrackerMirror.assignee_id == assignee_id,
        )
    )
    return len(db.scalars(stmt).all())


def get_link_for_project(db: Session, *, project_id: str, tracker_kind: str = "linear") -> TrackerLink | None:
    """Find the tracker link for a project. Returns None if not linked."""
    stmt = select(TrackerLink).where(
        and_(
            TrackerLink.project_id == project_id,
            TrackerLink.tracker_kind == tracker_kind,
        )
    )
    return db.scalars(stmt).first()
