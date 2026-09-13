"""PRD-P10 §Governance and data boundary (GRPH-194).

Covers:
- Storage tier (bodies_in_hub vs metadata_only)
- Mirror data filtering based on tier
- Clustering availability under metadata_only
- Inference boundary reporting
"""
from __future__ import annotations

import pytest

from app.models import TrackerLink, Organization
from app.services import governance


def _make_link(db, storage_tier="bodies_in_hub", **overrides) -> TrackerLink:
    org_id = overrides.pop("org_id", "org_gov1")
    if db.get(Organization, org_id) is None:
        db.add(Organization(id=org_id, name="Gov Test Org"))
        db.commit()
    defaults = {
        "id": "trl_gov123",
        "org_id": org_id,
        "project_id": "core",
        "tracker_kind": "linear",
        "tracker_team_id": "team_gov",
        "tracker_team_name": "Gov Eng",
        "authority": True,
        "field_mapping": {},
        "write_back_comment": True,
        "storage_tier": storage_tier,
    }
    defaults.update(overrides)
    link = TrackerLink(**defaults)
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


# ---- Storage tier ----

class TestGetStorageTier:
    def test_default_is_bodies_in_hub(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            assert governance.get_storage_tier(link) == "bodies_in_hub"
        finally:
            db.close()

    def test_metadata_only(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, storage_tier="metadata_only")
            assert governance.get_storage_tier(link) == "metadata_only"
        finally:
            db.close()

    def test_unknown_defaults_to_bodies_in_hub(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, storage_tier="bogus")
            assert governance.get_storage_tier(link) == "bodies_in_hub"
        finally:
            db.close()


class TestIsMetadataOnly:
    def test_false_for_bodies_in_hub(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            assert governance.is_metadata_only(link) is False
        finally:
            db.close()

    def test_true_for_metadata_only(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, storage_tier="metadata_only")
            assert governance.is_metadata_only(link) is True
        finally:
            db.close()


# ---- Mirror data filtering ----

class TestFilterMirrorData:
    def test_bodies_in_hub_keeps_all(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            data = {
                "issue_id": "lin_1",
                "title": "Fix bug",
                "description": "Details here",
                "canonical_status": "in_progress",
                "assignee_id": "u1",
                "labels": ["bug"],
            }
            filtered = governance.filter_mirror_data(link, data)
            assert filtered == data
        finally:
            db.close()

    def test_metadata_only_drops_body_fields(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, storage_tier="metadata_only")
            data = {
                "issue_id": "lin_1",
                "title": "Fix bug",
                "description": "Details here",
                "canonical_status": "in_progress",
                "assignee_id": "u1",
                "labels": ["bug"],
            }
            filtered = governance.filter_mirror_data(link, data)
            assert "title" not in filtered
            assert "description" not in filtered
            assert filtered["issue_id"] == "lin_1"
            assert filtered["canonical_status"] == "in_progress"
            assert filtered["labels"] == ["bug"]
        finally:
            db.close()

    def test_does_not_mutate_original(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            data = {"issue_id": "lin_1", "title": "T"}
            original = dict(data)
            governance.filter_mirror_data(link, data)
            assert data == original
        finally:
            db.close()


# ---- Clustering availability ----

class TestClusteringAvailable:
    def test_true_for_bodies_in_hub(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            assert governance.clustering_available(link) is True
        finally:
            db.close()

    def test_false_for_metadata_only(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, storage_tier="metadata_only")
            assert governance.clustering_available(link) is False
        finally:
            db.close()


# ---- Inference boundary ----

class TestInferenceBoundary:
    def test_reports_providers(self, client):
        result = governance.inference_boundary()
        assert "embed_provider" in result
        assert "chat_provider" in result
        assert result["no_shared_model"] is True
        assert result["local_only_posture_available"] is True

    def test_stub_is_local_only(self, client, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "embed_provider", "stub")
        monkeypatch.setattr(settings, "chat_provider", "stub")
        result = governance.inference_boundary()
        assert result["is_local_only"] is True

    def test_cloud_is_not_local_only(self, client, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "embed_provider", "openai")
        monkeypatch.setattr(settings, "chat_provider", "anthropic")
        result = governance.inference_boundary()
        assert result["is_local_only"] is False


# ---- Constants ----

class TestConstants:
    def test_valid_tiers(self):
        assert "bodies_in_hub" in governance.VALID_STORAGE_TIERS
        assert "metadata_only" in governance.VALID_STORAGE_TIERS

    def test_metadata_fields_do_not_include_body(self):
        assert "title" not in governance.METADATA_FIELDS
        assert "description" not in governance.METADATA_FIELDS

    def test_body_fields(self):
        assert "title" in governance.BODY_FIELDS
        assert "description" in governance.BODY_FIELDS
