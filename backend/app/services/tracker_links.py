"""Tracker linking and authority (PRD-10 / GRPH-186).

Config to link an external tracker (Linear first, Jira in v3) to an AgentLedger
project at org scope. When linked, the tracker is AUTHORITATIVE: AgentLedger
mirrors read-heavy and writes back only canonical status transitions,
comments/links, and the triage-board assignee.

This slice owns the link record, authority flag, and per-link field mapping.
The sync engine, adapter, and write-back paths are separate items.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TrackerLink, utcnow


TRACKER_KINDS = {"linear"}  # "jira" in v3


def _gen_id() -> str:
    return f"trl_{uuid.uuid4().hex[:12]}"


def create_link(
    db: Session,
    *,
    org_id: str,
    project_id: str,
    tracker_kind: str = "linear",
    tracker_team_id: str,
    tracker_team_name: str = "",
    authority: bool = True,
    field_mapping: dict | None = None,
    write_back_comment: bool = True,
) -> TrackerLink:
    if tracker_kind not in TRACKER_KINDS:
        raise HTTPException(422, f"unsupported tracker kind: {tracker_kind}")
    if not tracker_team_id.strip():
        raise HTTPException(422, "tracker_team_id is required")

    existing = db.scalar(
        select(TrackerLink).where(
            TrackerLink.org_id == org_id,
            TrackerLink.tracker_kind == tracker_kind,
            TrackerLink.tracker_team_id == tracker_team_id,
        )
    )
    if existing:
        raise HTTPException(409, "this tracker team is already linked to this org")

    link = TrackerLink(
        id=_gen_id(),
        org_id=org_id,
        project_id=project_id,
        tracker_kind=tracker_kind,
        tracker_team_id=tracker_team_id.strip(),
        tracker_team_name=tracker_team_name.strip(),
        authority=authority,
        field_mapping=field_mapping or {},
        write_back_comment=write_back_comment,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def list_links(db: Session, *, org_id: str) -> list[TrackerLink]:
    return list(
        db.scalars(
            select(TrackerLink)
            .where(TrackerLink.org_id == org_id)
            .order_by(TrackerLink.created_at)
        ).all()
    )


def get_link(db: Session, *, link_id: str) -> TrackerLink:
    link = db.get(TrackerLink, link_id)
    if link is None:
        raise HTTPException(404, "tracker link not found")
    return link


def update_link(
    db: Session,
    *,
    link_id: str,
    authority: bool | None = None,
    field_mapping: dict | None = None,
    write_back_comment: bool | None = None,
    tracker_team_name: str | None = None,
) -> TrackerLink:
    link = get_link(db, link_id=link_id)
    if authority is not None:
        link.authority = authority
    if field_mapping is not None:
        link.field_mapping = field_mapping
    if write_back_comment is not None:
        link.write_back_comment = write_back_comment
    if tracker_team_name is not None:
        link.tracker_team_name = tracker_team_name.strip()
    link.updated_at = utcnow()
    db.commit()
    db.refresh(link)
    return link


def delete_link(db: Session, *, link_id: str) -> dict:
    link = get_link(db, link_id=link_id)
    org_id = link.org_id
    project_id = link.project_id
    db.delete(link)
    db.commit()
    return {"org_id": org_id, "project_id": project_id}
