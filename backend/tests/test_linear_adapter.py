"""PRD-P10 §Linear integration adapter.

Covers:
- Status mapping (Linear state types → AgentLedger canonical statuses)
- Webhook signature verification (HMAC-SHA256)
- Integration lifecycle (link / status / unlink)
- OAuth URL construction
- Rate-limit state tracking
- GraphQL client error handling
- Router endpoints (status, unlink, issues, write-back)
- Encrypted token storage
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest

from app.services import linear


# ---- Status mapping ----

class TestStatusMapping:
    def test_backlog_states_map_to_backlog(self):
        assert linear.LINEAR_STATE_TYPE_TO_STATUS["backlog"] == "backlog"
        assert linear.LINEAR_STATE_TYPE_TO_STATUS["unstarted"] == "backlog"
        assert linear.LINEAR_STATE_TYPE_TO_STATUS["triage"] == "backlog"

    def test_started_maps_to_in_progress(self):
        assert linear.LINEAR_STATE_TYPE_TO_STATUS["started"] == "in_progress"

    def test_completed_and_canceled_map_to_done(self):
        assert linear.LINEAR_STATE_TYPE_TO_STATUS["completed"] == "done"
        assert linear.LINEAR_STATE_TYPE_TO_STATUS["canceled"] == "done"

    def test_reverse_map_covers_all_valid_statuses(self):
        for status in linear.VALID_STATUSES:
            assert status in linear.STATUS_TO_LINEAR_STATE_TYPE

    def test_review_maps_to_started(self):
        assert linear.STATUS_TO_LINEAR_STATE_TYPE["review"] == "started"

    def test_canonical_status_property(self):
        issue = linear.LinearIssue(
            id="abc", identifier="ENG-1", title="Test",
            description=None, assignee_id=None, assignee_name=None,
            state_id="s1", state_name="In Progress", state_type="started",
            team_id="t1", team_name="Eng", labels=[], updated_at="2026-01-01",
            url="https://linear.app/issue/ENG-1",
        )
        assert issue.canonical_status == "in_progress"

    def test_unknown_state_type_defaults_to_backlog(self):
        issue = linear.LinearIssue(
            id="abc", identifier="ENG-1", title="Test",
            description=None, assignee_id=None, assignee_name=None,
            state_id="s1", state_name="Unknown", state_type="mystery",
            team_id="t1", team_name="Eng", labels=[], updated_at="2026-01-01",
            url="",
        )
        assert issue.canonical_status == "backlog"


# ---- Webhook verification ----

class TestWebhookVerification:
    def test_valid_signature(self):
        secret = "whsec_test123"
        payload = b'{"action":"create","data":{"id":"123"}}'
        sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert linear.verify_webhook(payload, sig, secret) is True

    def test_invalid_signature(self):
        assert linear.verify_webhook(b"payload", "badsig", "secret") is False

    def test_empty_secret_returns_false(self):
        assert linear.verify_webhook(b"payload", "sig", "") is False

    def test_empty_signature_returns_false(self):
        assert linear.verify_webhook(b"payload", "", "secret") is False

    def test_tampered_payload_fails(self):
        secret = "whsec_test123"
        payload = b'{"action":"create"}'
        sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert linear.verify_webhook(b'{"action":"delete"}', sig, secret) is False


# ---- OAuth URL ----

class TestOAuthUrl:
    def test_builds_url_with_client_id(self, monkeypatch):
        monkeypatch.setattr(linear.settings, "linear_client_id", "test_client_id")
        monkeypatch.setattr(linear.settings, "linear_redirect_uri", "")
        monkeypatch.setattr(linear.settings, "app_base_url", "http://localhost:8080")
        url, state = linear.oauth_url()
        assert "linear.app/oauth/approve" in url
        assert "client_id=test_client_id" in url
        assert "response_type=code" in url
        assert f"state={state}" in url
        assert len(state) > 16

    def test_raises_without_client_id(self, monkeypatch):
        monkeypatch.setattr(linear.settings, "linear_client_id", "")
        with pytest.raises(linear.LinearError, match="not configured"):
            linear.oauth_url()

    def test_custom_redirect_uri(self, monkeypatch):
        monkeypatch.setattr(linear.settings, "linear_client_id", "cid")
        monkeypatch.setattr(linear.settings, "linear_redirect_uri", "https://custom.example.com/cb")
        url, _ = linear.oauth_url()
        assert "redirect_uri=https://custom.example.com/cb" in url


# ---- Rate limit tracking ----

class TestRateLimit:
    def test_initial_state_not_low(self):
        rl = linear.RateLimitState()
        assert rl.remaining == 1500
        assert not rl.is_low

    def test_low_when_below_threshold(self):
        rl = linear.RateLimitState(remaining=10)
        assert rl.is_low

    def test_update_from_headers(self):
        rl = linear.RateLimitState()
        headers = {
            "x-ratelimit-requests-remaining": "42",
            "x-ratelimit-requests-limit": "1500",
        }
        mock_headers = MagicMock()
        mock_headers.__contains__ = lambda self, k: k in headers
        mock_headers.__getitem__ = lambda self, k: headers[k]
        rl.update_from_headers(mock_headers)
        assert rl.remaining == 42
        assert rl.limit == 1500


# ---- GraphQL client ----

class TestLinearClient:
    def test_graphql_raises_on_http_error(self):
        client = linear.LinearClient(access_token="test_token")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.headers = {}
        client._http = MagicMock()
        client._http.post.return_value = mock_resp
        with pytest.raises(linear.LinearError, match="HTTP 500"):
            client.graphql("query { viewer { id } }")
        client.close()

    def test_graphql_raises_on_graphql_errors(self):
        client = linear.LinearClient(access_token="test_token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {"errors": [{"message": "Not found"}]}
        client._http = MagicMock()
        client._http.post.return_value = mock_resp
        with pytest.raises(linear.LinearError, match="Not found"):
            client.graphql("query { viewer { id } }")
        client.close()

    def test_graphql_returns_data(self):
        client = linear.LinearClient(access_token="test_token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {"data": {"viewer": {"id": "v1"}}}
        client._http = MagicMock()
        client._http.post.return_value = mock_resp
        result = client.graphql("query { viewer { id } }")
        assert result == {"viewer": {"id": "v1"}}
        client.close()


# ---- Integration lifecycle (DB) ----

class TestIntegrationLifecycle:
    def test_link_and_status(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            assert linear.get_integration(db) is None
            integ = linear.link_integration(
                db, access_token="tok_123",
                workspace_id="ws_1", workspace_name="Acme",
            )
            assert integ.token_set
            status = linear.integration_status(db)
            assert status["linked"] is True
            assert status["workspace_id"] == "ws_1"
            assert status["workspace_name"] == "Acme"
            assert status["token_set"] is True
        finally:
            db.close()

    def test_unlink(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="tok", workspace_id="ws", workspace_name="W",
            )
            assert linear.unlink_integration(db) is True
            assert linear.get_integration(db) is None
            assert linear.integration_status(db)["linked"] is False
        finally:
            db.close()

    def test_unlink_when_not_linked(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            assert linear.unlink_integration(db) is False
        finally:
            db.close()

    def test_token_encrypted_at_rest(self, client):
        from app.db import SessionLocal
        from app.security import secrets as sec
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="secret_token",
                workspace_id="ws", workspace_name="W",
            )
            integ = linear.get_integration(db)
            stored = integ.access_token_enc
            if sec.encryption_enabled():
                assert stored.startswith("enc::")
                assert "secret_token" not in stored
            decrypted = sec.decrypt(stored)
            assert decrypted == "secret_token"
        finally:
            db.close()

    def test_touch_webhook_updates_timestamp(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="tok", workspace_id="ws", workspace_name="W",
            )
            assert linear.integration_status(db)["last_webhook_at"] is None
            linear.touch_webhook(db)
            assert linear.integration_status(db)["last_webhook_at"] is not None
        finally:
            db.close()

    def test_touch_sync_updates_timestamp(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="tok", workspace_id="ws", workspace_name="W",
            )
            assert linear.integration_status(db)["last_sync_at"] is None
            linear.touch_sync(db)
            assert linear.integration_status(db)["last_sync_at"] is not None
        finally:
            db.close()


# ---- Parse issue ----

class TestParseIssue:
    def test_parses_full_node(self):
        node = {
            "id": "i1",
            "identifier": "ENG-42",
            "title": "Fix bug",
            "description": "Details here",
            "assignee": {"id": "u1", "name": "Alice"},
            "state": {"id": "s1", "name": "In Progress", "type": "started"},
            "team": {"id": "t1", "name": "Engineering"},
            "labels": {"nodes": [{"id": "l1", "name": "bug"}, {"id": "l2", "name": "urgent"}]},
            "updatedAt": "2026-09-12T00:00:00Z",
            "url": "https://linear.app/issue/ENG-42",
        }
        issue = linear._parse_issue(node)
        assert issue.id == "i1"
        assert issue.identifier == "ENG-42"
        assert issue.assignee_id == "u1"
        assert issue.state_type == "started"
        assert issue.canonical_status == "in_progress"
        assert issue.labels == ["bug", "urgent"]

    def test_handles_null_assignee(self):
        node = {
            "id": "i1", "identifier": "ENG-1", "title": "T",
            "description": None, "assignee": None,
            "state": {"id": "s1", "name": "Backlog", "type": "backlog"},
            "team": {"id": "t1", "name": "Eng"},
            "labels": {"nodes": []},
            "updatedAt": "2026-01-01", "url": "",
        }
        issue = linear._parse_issue(node)
        assert issue.assignee_id is None
        assert issue.assignee_name is None

    def test_handles_missing_labels(self):
        node = {
            "id": "i1", "identifier": "ENG-1", "title": "T",
            "description": None, "assignee": None,
            "state": {"id": "s1", "name": "Backlog", "type": "backlog"},
            "team": {"id": "t1", "name": "Eng"},
            "updatedAt": "2026-01-01", "url": "",
        }
        issue = linear._parse_issue(node)
        assert issue.labels == []


# ---- find_state_for_type ----

class TestFindStateForType:
    def test_finds_matching_state(self):
        states = [
            {"id": "s1", "name": "Backlog", "type": "backlog"},
            {"id": "s2", "name": "In Progress", "type": "started"},
            {"id": "s3", "name": "Done", "type": "completed"},
        ]
        assert linear.find_state_for_type(states, "started") == "s2"

    def test_returns_none_when_no_match(self):
        states = [{"id": "s1", "name": "Backlog", "type": "backlog"}]
        assert linear.find_state_for_type(states, "completed") is None


# ---- Router endpoints ----

class TestRouterStatus:
    def test_status_unauthenticated(self, client):
        r = client.get("/api/linear/status")
        assert r.status_code == 401

    def test_status_when_not_linked(self, client, auth):
        r = client.get("/api/linear/status", headers=auth)
        assert r.status_code == 200
        data = r.json()
        assert data["linked"] is False

    def test_status_when_linked(self, client, auth):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="tok", workspace_id="ws_1", workspace_name="Acme",
            )
        finally:
            db.close()
        r = client.get("/api/linear/status", headers=auth)
        assert r.status_code == 200
        data = r.json()
        assert data["linked"] is True
        assert data["workspace_id"] == "ws_1"

    def test_unlink(self, client, auth):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="tok", workspace_id="ws", workspace_name="W",
            )
        finally:
            db.close()
        r = client.delete("/api/linear/link", headers=auth)
        assert r.status_code == 200
        assert r.json()["removed"] is True
        r2 = client.get("/api/linear/status", headers=auth)
        assert r2.json()["linked"] is False


class TestRouterWebhook:
    def test_webhook_without_integration(self, client):
        r = client.post("/api/linear/webhook", content=b"{}",
                        headers={"content-type": "application/json"})
        assert r.status_code == 400

    def test_webhook_with_valid_payload(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="tok", workspace_id="ws", workspace_name="W",
            )
        finally:
            db.close()
        payload = json.dumps({"action": "create", "data": {"id": "i1"}}).encode()
        r = client.post("/api/linear/webhook", content=payload,
                        headers={"content-type": "application/json", "linear-signature": ""})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_webhook_updates_freshness(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="tok", workspace_id="ws", workspace_name="W",
            )
        finally:
            db.close()
        payload = json.dumps({"action": "update", "data": {"id": "i1"}}).encode()
        client.post("/api/linear/webhook", content=payload,
                    headers={"content-type": "application/json", "linear-signature": ""})
        status = client.get("/api/linear/status",
                            headers={"Authorization": f"Bearer {client.post('/api/auth/login', json={'email': 'alex@ascme-labs.com', 'password': 'graphban'}).json()['access_token']}"})
        assert status.json()["last_webhook_at"] is not None

    def test_webhook_rejects_bad_signature(self, client):
        from app.db import SessionLocal
        from app.security import secrets as sec
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="tok", workspace_id="ws", workspace_name="W",
                webhook_secret="mysecret",
            )
        finally:
            db.close()
        payload = b'{"action":"create"}'
        r = client.post("/api/linear/webhook", content=payload,
                        headers={"content-type": "application/json", "linear-signature": "wrongsig"})
        assert r.status_code == 401


class TestRouterIssues:
    def test_issues_requires_team_id(self, client, auth):
        r = client.get("/api/linear/issues", headers=auth)
        assert r.status_code == 422

    def test_issues_requires_integration(self, client, auth):
        r = client.get("/api/linear/issues?team_id=t1", headers=auth)
        assert r.status_code == 400


class TestRouterWriteBack:
    def test_write_status_requires_team_id(self, client, auth):
        r = client.post("/api/linear/issues/i1/status",
                        json={"status": "done"}, headers=auth)
        assert r.status_code == 422

    def test_write_status_requires_integration(self, client, auth):
        r = client.post("/api/linear/issues/i1/status?team_id=t1",
                        json={"status": "done"}, headers=auth)
        assert r.status_code == 400

    def test_write_comment_requires_integration(self, client, auth):
        r = client.post("/api/linear/issues/i1/comment",
                        json={"body": "hello"}, headers=auth)
        assert r.status_code == 400

    def test_write_assignee_requires_integration(self, client, auth):
        r = client.post("/api/linear/issues/i1/assignee",
                        json={"assignee_id": "u1"}, headers=auth)
        assert r.status_code == 400


# ---- get_client ----

class TestGetClient:
    def test_raises_when_no_integration(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            with pytest.raises(linear.LinearError, match="No Linear integration"):
                linear.get_client(db)
        finally:
            db.close()

    def test_returns_client_when_linked(self, client):
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            linear.link_integration(
                db, access_token="my_token", workspace_id="ws", workspace_name="W",
            )
            lc = linear.get_client(db)
            assert lc.access_token == "my_token"
            lc.close()
        finally:
            db.close()
