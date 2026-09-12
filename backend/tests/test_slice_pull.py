"""PRD-P10 §Per-dev slice pull (GRPH-190).

Covers:
- Service: get_slice, get_slice_with_governance, count_slice, get_link_for_project
- Router: /slice/my, /slice/count
- Governance filtering under metadata_only
"""
from __future__ import annotations

import pytest

from app.models import TrackerLink, TrackerMirror, Organization, User
from app.services import slice_pull
from app.services.linear import LinearIssue


SEED_PW = "graphban"


def _login(client, email, password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_link(db, **overrides) -> TrackerLink:
    org_id = overrides.pop("org_id", "org_slice1")
    if db.get(Organization, org_id) is None:
        db.add(Organization(id=org_id, name="Slice Test Org"))
        db.commit()
    defaults = {
        "id": "trl_slice123",
        "org_id": org_id,
        "project_id": "core",
        "tracker_kind": "linear",
        "tracker_team_id": "team_slice",
        "tracker_team_name": "Slice Eng",
        "authority": True,
        "field_mapping": {},
        "write_back_comment": True,
        "storage_tier": "bodies_in_hub",
    }
    defaults.update(overrides)
    link = TrackerLink(**defaults)
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def _make_mirror(db, link_id, issue_id, assignee_id, **overrides):
    defaults = {
        "issue_id": issue_id,
        "link_id": link_id,
        "tracker_kind": "linear",
        "identifier": f"ENG-{issue_id[-1]}",
        "title": f"Issue {issue_id}",
        "description": f"Details for {issue_id}",
        "canonical_status": "in_progress",
        "assignee_id": assignee_id,
        "assignee_name": "Dev",
        "labels": ["bug"],
        "tracker_updated_at": "2026-09-12T00:00:00Z",
        "version": "v1",
        "url": f"https://linear.app/issue/{issue_id}",
    }
    defaults.update(overrides)
    mirror = TrackerMirror(**defaults)
    db.add(mirror)
    db.commit()
    return mirror


# ---- Service ----

class TestGetSlice:
    def test_returns_assigned_issues(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            _make_mirror(db, link.id, "lin_1", "user_1")
            _make_mirror(db, link.id, "lin_2", "user_1")
            _make_mirror(db, link.id, "lin_3", "user_2")
            issues = slice_pull.get_slice(db, link_id=link.id, assignee_id="user_1")
            assert len(issues) == 2
            assert {i.issue_id for i in issues} == {"lin_1", "lin_2"}
        finally:
            db.close()

    def test_empty_for_unknown_assignee(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            _make_mirror(db, link.id, "lin_1", "user_1")
            issues = slice_pull.get_slice(db, link_id=link.id, assignee_id="nobody")
            assert len(issues) == 0
        finally:
            db.close()


class TestGetSliceWithGovernance:
    def test_bodies_in_hub_keeps_all(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, storage_tier="bodies_in_hub")
            _make_mirror(db, link.id, "lin_1", "user_1")
            issues = slice_pull.get_slice_with_governance(db, link=link, assignee_id="user_1")
            assert len(issues) == 1
            assert issues[0]["title"] == "Issue lin_1"
            assert issues[0]["description"] == "Details for lin_1"
        finally:
            db.close()

    def test_metadata_only_drops_body(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, storage_tier="metadata_only")
            _make_mirror(db, link.id, "lin_1", "user_1")
            issues = slice_pull.get_slice_with_governance(db, link=link, assignee_id="user_1")
            assert len(issues) == 1
            assert "title" not in issues[0]
            assert "description" not in issues[0]
            assert issues[0]["issue_id"] == "lin_1"
        finally:
            db.close()


class TestCountSlice:
    def test_counts_assigned(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            _make_mirror(db, link.id, "lin_1", "user_1")
            _make_mirror(db, link.id, "lin_2", "user_1")
            count = slice_pull.count_slice(db, link_id=link.id, assignee_id="user_1")
            assert count == 2
        finally:
            db.close()


class TestGetLinkForProject:
    def test_returns_link(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            _make_link(db)
            link = slice_pull.get_link_for_project(db, project_id="core")
            assert link is not None
            assert link.id == "trl_slice123"
        finally:
            db.close()

    def test_returns_none_when_not_linked(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = slice_pull.get_link_for_project(db, project_id="nonexistent")
            assert link is None
        finally:
            db.close()


# ---- Router ----

class TestRouterMySlice:
    def test_requires_auth(self, client):
        r = client.get("/api/slice/my?project_id=core")
        assert r.status_code == 401

    def test_requires_project_id(self, client, auth):
        r = client.get("/api/slice/my", headers=auth)
        assert r.status_code == 422

    def test_returns_404_when_not_linked(self, client, auth):
        r = client.get("/api/slice/my?project_id=nonexistent", headers=auth)
        assert r.status_code == 404

    def test_returns_slice(self, client, auth):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            # Get the seed user's id.
            user = db.query(User).filter_by(handle="ascme").first()
            _make_mirror(db, link.id, "lin_1", user.id)
        finally:
            db.close()
        r = client.get("/api/slice/my?project_id=core", headers=auth)
        assert r.status_code == 200
        data = r.json()
        assert data["project_id"] == "core"
        assert data["count"] >= 1


class TestRouterSliceCount:
    def test_requires_auth(self, client):
        r = client.get("/api/slice/count?project_id=core")
        assert r.status_code == 401

    def test_returns_count(self, client, auth):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            user = db.query(User).filter_by(handle="ascme").first()
            _make_mirror(db, link.id, "lin_1", user.id)
        finally:
            db.close()
        r = client.get("/api/slice/count?project_id=core", headers=auth)
        assert r.status_code == 200
        assert r.json()["count"] >= 1
