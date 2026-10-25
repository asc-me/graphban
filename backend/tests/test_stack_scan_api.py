"""GRPH-1047 — REST producer path for Stack code-health scans.

`record_scan` shipped as an MCP tool (GRPH-1035) reachable only on a fleet-worker
connection, so an operator or CI could not populate the Stack overlay. These cover the
key-authed REST ingest that fixes that: it records exactly like the MCP tool, refuses a
read-only caller, and never lets absence read as clean.
"""
from __future__ import annotations


def _key(client, auth, name="scan-rest"):
    return client.post("/api/api-keys", json={"name": name}, headers=auth).json()["plaintext"]


def test_post_records_a_scan_and_it_shows_in_health(client, auth):
    key = _key(client, auth)
    r = client.post("/api/platform/stack/scans", json={
        "project_id": "core", "scanner_id": "bandit", "scanner_version": "1.9.4",
        "path": "backend/", "scanned_sha": "cafe1234",
        "findings": [{"severity": "med", "summary": "B608 SQL string",
                      "path": "backend/app/x.py:1", "link_kind": "code",
                      "link_ref": "backend/app/x.py"}],
    }, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["findings_recorded"] == 1
    assert body["clean"] is False
    # Provenance is the posting credential, never a caller-supplied agent id (GRPH-1044).
    assert body["reported_by"].startswith("key:")

    # It shows in the overlay payload the UI reads (GET /stack/health).
    h = client.get("/api/platform/stack/health?project_id=core", headers=auth).json()
    api = next(c for c in h["components"] if c["id"] == "api")
    assert api["categories"]["security"]["state"] == "has_findings"


def test_empty_findings_is_a_clean_result_not_never_scanned(client, auth):
    key = _key(client, auth)
    r = client.post("/api/platform/stack/scans", json={
        "project_id": "core", "scanner_id": "vulture", "path": "cli/",
        "scanned_sha": "cafe1234", "findings": [], "category": "refactor",
    }, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    assert r.json()["findings_recorded"] == 0
    assert r.json()["clean"] is True

    h = client.get("/api/platform/stack/health?project_id=core", headers=auth).json()
    cli = next(c for c in h["components"] if c["id"] == "cli")
    # A recorded clean result (clean_at_sha or stale), NOT never_scanned.
    assert cli["categories"]["refactor"]["state"] != "never_scanned"


def test_a_summary_less_finding_is_422_and_nothing_is_stored(client, auth):
    key = _key(client, auth)
    r = client.post("/api/platform/stack/scans", json={
        "project_id": "core", "scanner_id": "bandit", "path": "web/",
        "scanned_sha": "cafe1234",
        "findings": [{"severity": "high"}],  # no summary — absence must not read as clean
    }, headers={"X-API-Key": key})
    assert r.status_code == 422, r.text

    # Nothing stored: web security stays never_scanned.
    h = client.get("/api/platform/stack/health?project_id=core", headers=auth).json()
    web = next(c for c in h["components"] if c["id"] == "web")
    assert web["categories"]["security"]["state"] == "never_scanned"


def test_an_unknown_scanner_without_category_is_422_and_nothing_is_stored(client, auth):
    # GRPH-1108: no fallback category — a default would let one empty row paint it clean.
    key = _key(client, auth)
    r = client.post("/api/platform/stack/scans", json={
        "project_id": "core", "scanner_id": "coderabbit", "path": "web/",
        "scanned_sha": "cafe1234", "findings": [],
    }, headers={"X-API-Key": key})
    assert r.status_code == 422, r.text
    assert "pip-audit" in r.text and "refactor" in r.text

    h = client.get(
        "/api/platform/stack/health?project_id=core&head_sha=cafe1234", headers=auth,
    ).json()
    web = next(c for c in h["components"] if c["id"] == "web")
    assert web["categories"]["security"]["state"] == "never_scanned"


def test_no_key_is_401(client):
    r = client.post("/api/platform/stack/scans", json={
        "project_id": "core", "scanner_id": "bandit", "path": "web/",
        "scanned_sha": "cafe1234", "findings": [],
    })
    assert r.status_code == 401, r.text


def test_a_non_member_key_cannot_write_403(client, auth):
    # A second user, not a member of `core`, holds a valid key but no write there.
    reg = client.post("/api/auth/register", json={
        "name": "Sam Rivers", "email": "sam@example.com", "handle": "sam",
        "password": "pw123456",
    })
    assert reg.status_code == 201, reg.text
    other = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    other_key = client.post(
        "/api/api-keys", json={"name": "outsider"}, headers=other).json()["plaintext"]

    r = client.post("/api/platform/stack/scans", json={
        "project_id": "core", "scanner_id": "bandit", "path": "web/",
        "scanned_sha": "cafe1234", "findings": [],
    }, headers={"X-API-Key": other_key})
    assert r.status_code == 403, r.text
