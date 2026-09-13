"""Tracker linking and authority REST (PRD-10 / GRPH-186).

Org-scoped: every route sits behind an org-admin gate. The link record, authority
flag, and per-link field mapping live here; the sync engine and adapter are
separate items.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.schemas import TrackerLinkCreate, TrackerLinkOut, TrackerLinkUpdate
from app.security import authz
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import tracker_links as tracker_links_svc

router = APIRouter(prefix="/orgs/{org_id}/tracker-links", tags=["tracker-links"])


@router.get("", response_model=list[TrackerLinkOut])
def list_links(
    org_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_org_member(db, user.id, org_id)
    return tracker_links_svc.list_links(db, org_id=org_id)


@router.post("", response_model=TrackerLinkOut, status_code=201)
def create_link(
    org_id: str,
    body: TrackerLinkCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_org_admin(db, user.id, org_id)
    link = tracker_links_svc.create_link(
        db,
        org_id=org_id,
        project_id=body.project_id,
        tracker_kind=body.tracker_kind,
        tracker_team_id=body.tracker_team_id,
        tracker_team_name=body.tracker_team_name,
        authority=body.authority,
        field_mapping=body.field_mapping,
        write_back_comment=body.write_back_comment,
    )
    events_svc.record_user(
        db, user, action="create_tracker_link", target_type="tracker_link",
        target_id=link.id,
        meta={"org_id": org_id, "project_id": body.project_id,
              "tracker_kind": body.tracker_kind, "tracker_team_id": body.tracker_team_id},
    )
    return link


@router.get("/{link_id}", response_model=TrackerLinkOut)
def get_link(
    org_id: str,
    link_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_org_member(db, user.id, org_id)
    link = tracker_links_svc.get_link(db, link_id=link_id)
    if link.org_id != org_id:
        raise HTTPException(404, "tracker link not found")
    return link


@router.patch("/{link_id}", response_model=TrackerLinkOut)
def update_link(
    org_id: str,
    link_id: str,
    body: TrackerLinkUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_org_admin(db, user.id, org_id)
    link = tracker_links_svc.get_link(db, link_id=link_id)
    if link.org_id != org_id:
        raise HTTPException(404, "tracker link not found")
    link = tracker_links_svc.update_link(
        db,
        link_id=link_id,
        authority=body.authority,
        field_mapping=body.field_mapping,
        write_back_comment=body.write_back_comment,
        tracker_team_name=body.tracker_team_name,
    )
    events_svc.record_user(
        db, user, action="update_tracker_link", target_type="tracker_link",
        target_id=link_id,
        meta={"org_id": org_id, "changed": body.model_dump(exclude_none=True)},
    )
    return link


@router.delete("/{link_id}", status_code=204)
def delete_link(
    org_id: str,
    link_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_org_admin(db, user.id, org_id)
    link = tracker_links_svc.get_link(db, link_id=link_id)
    if link.org_id != org_id:
        raise HTTPException(404, "tracker link not found")
    tracker_links_svc.delete_link(db, link_id=link_id)
    events_svc.record_user(
        db, user, action="delete_tracker_link", target_type="tracker_link",
        target_id=link_id, meta={"org_id": org_id},
    )
