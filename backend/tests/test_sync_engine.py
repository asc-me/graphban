"""PRD-P10 §Sync engine (GRPH-188).

Covers:
- Fingerprint store (write, check_echo, consume, prune)
- Mirror (upsert, bulk, get, list)
- Reconcile (echo suppression, external edit, first-time mirror, conflict policy)
- Field mapping (default, custom, lossy)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import TrackerLink, TrackerMirror, SyncFingerprint, utcnow
from app.services import sync_engine
from app.services.linear import LinearIssue


def _make_issue(**overrides) -> LinearIssue:
    defaults = {
        "id": "lin_123",
        "identifier": "ENG-42",
        "title": "Fix bug",
        "description": "Details",
        "assignee_id": "u1",
        "assignee_name": "Alice",
        "state_id": "s1",
        "state_name": "In Progress",
        "state_type": "started",
        "team_id": "t1",
        "team_name": "Eng",
        "labels": ["bug"],
        "updated_at": "2026-09-12T00:00:00Z",
        "url": "https://linear.app/issue/ENG-42",
    }
    defaults.update(overrides)
    return LinearIssue(**defaults)


def _make_link(db, **overrides) -> TrackerLink:
    from app.models import Organization
    # Ensure an org exists for the FK.
    org_id = overrides.pop("org_id", "org_test1")
    if db.get(Organization, org_id) is None:
        db.add(Organization(id=org_id, name="Test Org"))
        db.commit()
    defaults = {
        "id": "trl_test123",
        "org_id": org_id,
        "project_id": "core",
        "tracker_kind": "linear",
        "tracker_team_id": "team_1",
        "tracker_team_name": "Eng",
        "authority": True,
        "field_mapping": {},
        "write_back_comment": True,
    }
    defaults.update(overrides)
    link = TrackerLink(**defaults)
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


# ---- Fingerprint store ----

class TestFingerprintWrite:
    def test_creates_fingerprint(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            fp = sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_123",
                field_name="canonical_status", expected_version="v1",
            )
            assert fp.link_id == link.id
            assert fp.issue_id == "lin_123"
            assert fp.field == "canonical_status"
            assert fp.expected_version == "v1"
            assert fp.write_token
            assert fp.consumed is False
        finally:
            db.close()

    def test_deterministic_write_token(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            fp1 = sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_1",
                field_name="canonical_status", expected_version="v1",
            )
            fp2 = sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_1",
                field_name="canonical_status", expected_version="v1",
            )
            assert fp1.write_token == fp2.write_token
            assert fp1.id != fp2.id
        finally:
            db.close()


class TestCheckEcho:
    def test_matches_fingerprint(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_1",
                field_name="canonical_status", expected_version="v1",
            )
            match = sync_engine.check_echo(
                db, link_id=link.id, issue_id="lin_1",
                field_name="canonical_status", incoming_version="v1",
            )
            assert match is not None
            assert match.field == "canonical_status"
        finally:
            db.close()

    def test_no_match_when_version_differs(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_1",
                field_name="canonical_status", expected_version="v1",
            )
            match = sync_engine.check_echo(
                db, link_id=link.id, issue_id="lin_1",
                field_name="canonical_status", incoming_version="v2",
            )
            assert match is None
        finally:
            db.close()

    def test_no_match_when_consumed(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            fp = sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_1",
                field_name="canonical_status", expected_version="v1",
            )
            sync_engine.consume_fingerprint(db, fingerprint_id=fp.id)
            match = sync_engine.check_echo(
                db, link_id=link.id, issue_id="lin_1",
                field_name="canonical_status", incoming_version="v1",
            )
            assert match is None
        finally:
            db.close()

    def test_no_match_when_no_fingerprint(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            match = sync_engine.check_echo(
                db, link_id=link.id, issue_id="lin_1",
                field_name="canonical_status", incoming_version="v1",
            )
            assert match is None
        finally:
            db.close()


class TestConsumeFingerprint:
    def test_consumes(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            fp = sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_1",
                field_name="title", expected_version="v1",
            )
            assert sync_engine.consume_fingerprint(db, fingerprint_id=fp.id) is True
            db.refresh(fp)
            assert fp.consumed is True
        finally:
            db.close()

    def test_idempotent(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            fp = sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_1",
                field_name="title", expected_version="v1",
            )
            sync_engine.consume_fingerprint(db, fingerprint_id=fp.id)
            assert sync_engine.consume_fingerprint(db, fingerprint_id=fp.id) is False
        finally:
            db.close()

    def test_returns_false_for_unknown(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            assert sync_engine.consume_fingerprint(db, fingerprint_id="nope") is False
        finally:
            db.close()


class TestPruneFingerprints:
    def test_prunes_old_consumed(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            fp = sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_1",
                field_name="title", expected_version="v1",
            )
            sync_engine.consume_fingerprint(db, fingerprint_id=fp.id)
            # Backdate it.
            fp.created_at = utcnow() - timedelta(hours=2)
            db.commit()
            cutoff = utcnow() - timedelta(hours=1)
            count = sync_engine.prune_fingerprints(db, link_id=link.id, older_than=cutoff)
            assert count == 1
            assert db.get(SyncFingerprint, fp.id) is None
        finally:
            db.close()

    def test_does_not_prune_unconsumed(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            fp = sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_1",
                field_name="title", expected_version="v1",
            )
            fp.created_at = utcnow() - timedelta(hours=2)
            db.commit()
            cutoff = utcnow() - timedelta(hours=1)
            count = sync_engine.prune_fingerprints(db, link_id=link.id, older_than=cutoff)
            assert count == 0
        finally:
            db.close()


# ---- Mirror ----

class TestMirrorIssue:
    def test_creates_new(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            issue = _make_issue()
            mirror = sync_engine.mirror_issue(db, link_id=link.id, issue=issue)
            assert mirror.issue_id == "lin_123"
            assert mirror.title == "Fix bug"
            assert mirror.canonical_status == "in_progress"
            assert mirror.labels == ["bug"]
        finally:
            db.close()

    def test_upserts_existing(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            issue = _make_issue()
            sync_engine.mirror_issue(db, link_id=link.id, issue=issue)
            # Update the issue.
            updated = _make_issue(title="Updated title", state_type="completed")
            mirror = sync_engine.mirror_issue(db, link_id=link.id, issue=updated)
            assert mirror.title == "Updated title"
            assert mirror.canonical_status == "done"
        finally:
            db.close()


class TestMirrorBulk:
    def test_mirrors_batch(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            issues = [
                _make_issue(id="lin_1", identifier="ENG-1"),
                _make_issue(id="lin_2", identifier="ENG-2"),
                _make_issue(id="lin_3", identifier="ENG-3"),
            ]
            count = sync_engine.mirror_issues_bulk(db, link_id=link.id, issues=issues)
            assert count == 3
            assert len(sync_engine.list_mirror_by_link(db, link_id=link.id)) == 3
        finally:
            db.close()


class TestGetMirror:
    def test_returns_none_for_unknown(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            assert sync_engine.get_mirror(db, issue_id="nope") is None
        finally:
            db.close()

    def test_returns_existing(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            sync_engine.mirror_issue(db, link_id=link.id, issue=_make_issue())
            mirror = sync_engine.get_mirror(db, issue_id="lin_123")
            assert mirror is not None
            assert mirror.title == "Fix bug"
        finally:
            db.close()


# ---- Reconcile ----

class TestReconcileIssue:
    def test_first_time_mirrors_all(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            issue = _make_issue()
            diff = sync_engine.reconcile_issue(db, link=link, incoming=issue)
            assert diff.has_changes
            assert len(diff.changed_fields) > 0
            assert diff.external_edit is False
            assert sync_engine.get_mirror(db, issue_id="lin_123") is not None
        finally:
            db.close()

    def test_no_change_when_same(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            issue = _make_issue()
            sync_engine.mirror_issue(db, link_id=link.id, issue=issue)
            diff = sync_engine.reconcile_issue(db, link=link, incoming=issue)
            assert not diff.has_changes
            assert not diff.external_edit
        finally:
            db.close()

    def test_external_edit_detected(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            issue = _make_issue()
            sync_engine.mirror_issue(db, link_id=link.id, issue=issue)
            # PM changes the title externally.
            changed = _make_issue(title="PM changed this")
            diff = sync_engine.reconcile_issue(db, link=link, incoming=changed)
            assert diff.has_changes
            assert "title" in diff.changed_fields
            assert diff.external_edit is True
            assert diff.applied["title"] == "PM changed this"
            # Mirror is updated.
            mirror = sync_engine.get_mirror(db, issue_id="lin_123")
            assert mirror.title == "PM changed this"
        finally:
            db.close()

    def test_echo_suppressed(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            issue = _make_issue()
            sync_engine.mirror_issue(db, link_id=link.id, issue=issue)
            # We write a status change.
            sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_123",
                field_name="canonical_status", expected_version="2026-09-12T00:00:00Z",
            )
            # The webhook echoes it back with the same version.
            echo = _make_issue(state_type="completed")
            diff = sync_engine.reconcile_issue(db, link=link, incoming=echo)
            assert diff.is_echo is True
            assert not diff.external_edit
            assert not diff.has_changes
        finally:
            db.close()

    def test_external_edit_when_version_mismatches_fingerprint(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            issue = _make_issue()
            sync_engine.mirror_issue(db, link_id=link.id, issue=issue)
            # We fingerprint a status change at v1.
            sync_engine.fingerprint_write(
                db, link_id=link.id, issue_id="lin_123",
                field_name="canonical_status", expected_version="v1",
            )
            # But the webhook comes back with a different version (PM changed it after us).
            changed = _make_issue(state_type="completed", updated_at="v2")
            diff = sync_engine.reconcile_issue(db, link=link, incoming=changed)
            assert diff.external_edit is True
            assert "canonical_status" in diff.changed_fields
        finally:
            db.close()

    def test_description_never_written_back(self, client):
        """Description is tracker-owned (reads flow down) but never written to tracker."""
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db)
            issue = _make_issue(description="Original")
            sync_engine.mirror_issue(db, link_id=link.id, issue=issue)
            # PM edits description.
            changed = _make_issue(description="PM edited")
            diff = sync_engine.reconcile_issue(db, link=link, incoming=changed)
            assert "description" in diff.changed_fields
            mirror = sync_engine.get_mirror(db, issue_id="lin_123")
            assert mirror.description == "PM edited"
        finally:
            db.close()


# ---- Field mapping ----

class TestFieldMapping:
    def test_default_mapping(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, field_mapping={})
            incoming = {
                "title": "Test",
                "state_type": "started",
                "assignee_id": "u1",
                "unknown_field": "dropped",
            }
            mapped = sync_engine.apply_field_mapping(link, incoming)
            assert mapped["title"] == "Test"
            assert mapped["canonical_status"] == "started"
            assert mapped["assignee_id"] == "u1"
            assert "unknown_field" not in mapped
            assert "dropped" not in mapped.values()
        finally:
            db.close()

    def test_custom_mapping(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, field_mapping={"title": "title", "labels": "labels"})
            incoming = {"title": "Test", "labels": ["a"], "assignee_id": "u1"}
            mapped = sync_engine.apply_field_mapping(link, incoming)
            assert mapped == {"title": "Test", "labels": ["a"]}
        finally:
            db.close()

    def test_empty_mapping_uses_default(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            link = _make_link(db, field_mapping={})
            incoming = {"title": "T", "url": "http://x"}
            mapped = sync_engine.apply_field_mapping(link, incoming)
            assert "title" in mapped
            assert "url" in mapped
        finally:
            db.close()


# ---- Values equality ----

class TestValuesEqual:
    def test_none_none(self):
        assert sync_engine._values_equal(None, None) is True

    def test_none_value(self):
        assert sync_engine._values_equal(None, "x") is False
        assert sync_engine._values_equal("x", None) is False

    def test_list_order_independent(self):
        assert sync_engine._values_equal(["a", "b"], ["b", "a"]) is True

    def test_scalar(self):
        assert sync_engine._values_equal("x", "x") is True
        assert sync_engine._values_equal("x", "y") is False
