"""GRPH-1035 — code-health scans + Stack overlay honesty."""
from __future__ import annotations

import pytest

from app.mcp_server import LIVE_TOOL_COUNT, TOOLS
from app.services import scans as scans_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_record_scan_tools_are_in_the_manifest():
    names = {t["name"] for t in TOOLS}
    assert "record_scan" in names
    assert "scan_status" in names
    assert "scans_due" in names
    assert LIVE_TOOL_COUNT == len(TOOLS)


def test_empty_findings_is_clean_at_sha_not_never_scanned(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="pip-audit", scanner_version="2.7.3",
        path="backend/", scanned_sha="abc1234", findings=[], reported_by="GRPH-A1",
    )
    st = scans_svc.scan_status(db, "core", path="backend/", head_sha="abc1234")
    assert st["scans"], "a recorded clean scan must appear"
    row = st["scans"][0]
    assert row["state"] == "clean_at_sha"
    assert row["findings"] == []
    assert row["scanned_sha"] == "abc1234"


def test_absence_is_never_scanned_not_clean(db):
    health = scans_svc.component_health(db, "core", head_sha="abc1234")
    web = next(c for c in health["components"] if c["id"] == "web")
    # Security has no producer yet → never_scanned. Must not roll up to CLEAN
    # unless every category is clean (coverage/work may be clean_at_sha derived).
    assert web["categories"]["security"]["state"] == "never_scanned"
    assert web["rollup"]["state"] != "clean_at_sha"
    assert web["rollup"]["label"] in ("NOT SCANNED", "STALE", "FINDINGS")


def test_findings_and_stale_are_distinct(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit", path="backend/",
        scanned_sha="oldsha01",
        findings=[{"severity": "high", "summary": "assert used", "path": "backend/app/x.py"}],
    )
    # Against a moved head → still has_findings (findings remain visible).
    st = scans_svc.scan_status(db, "core", path="backend/", head_sha="newsha02")
    assert st["scans"][0]["state"] == "has_findings"
    assert len(st["scans"][0]["findings"]) == 1

    # Clean at old sha, head moved → stale.
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit", path="backend/",
        scanned_sha="oldsha01", findings=[],
    )
    st2 = scans_svc.scan_status(db, "core", path="backend/", head_sha="newsha02")
    assert st2["scans"][0]["state"] == "stale"


def test_record_scan_requires_scanned_sha(db):
    with pytest.raises(ValueError, match="scanned_sha"):
        scans_svc.record_scan(
            db, project_id="core", scanner_id="pip-audit",
            path="backend/", scanned_sha="", findings=[],
        )


def test_stack_health_endpoint(client, auth):
    r = client.get("/api/platform/stack/health?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scored"] is False
    ids = {c["id"] for c in body["components"]}
    assert ids == {"web", "api", "fleet", "cli"}
    for c in body["components"]:
        assert "rollup" in c and "categories" in c
        assert set(c["categories"]) >= {"security", "coverage", "work"}


def _mcp(client, headers, name, arguments, call_id=1):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers=headers,
    )


def test_mcp_record_scan_round_trip(client, auth):
    key = client.post("/api/api-keys", json={"name": "scan-e2e"}, headers=auth).json()["plaintext"]
    headers = {"X-API-Key": key}
    r = _mcp(client, headers, "record_scan", {
        "scanner_id": "pip-audit",
        "scanner_version": "2.7.3",
        "path": "web/",
        "scanned_sha": "deadbeef",
        "findings": [
            {"severity": "med", "summary": "lodash advisory", "path": "web/package.json"},
        ],
        "project_id": "core",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    sc = body["result"]["structuredContent"]
    assert sc["findings_recorded"] == 1
    assert sc["clean"] is False

    st = _mcp(client, headers, "scan_status", {
        "path": "web/", "head_sha": "deadbeef", "project_id": "core",
    }, call_id=2)
    assert st.status_code == 200, st.text
    path_payload = st.json()["result"]["structuredContent"]
    assert path_payload["project_id"] == "core"
    assert path_payload["scans"] and path_payload["scans"][0]["state"] == "has_findings"

    # Overlay shape (no path) — exercises components/scored/generated_at for schema probe.
    overlay = _mcp(client, headers, "scan_status", {
        "head_sha": "deadbeef", "project_id": "core",
    }, call_id=3)
    assert overlay.status_code == 200, overlay.text
    ov = overlay.json()["result"]["structuredContent"]
    assert ov["scored"] is False
    assert "generated_at" in ov
    assert {c["id"] for c in ov["components"]} == {"web", "api", "fleet", "cli"}

    due = _mcp(client, headers, "scans_due", {
        "head_sha": "deadbeef", "project_id": "core",
    }, call_id=4)
    assert due.status_code == 200, due.text
    due_body = due.json()["result"]["structuredContent"]
    assert "due" in due_body and "due_count" in due_body


def test_sabotage_caller_dropping_scanned_sha_is_refused(db):
    """Sabotage the CALL: omit scanned_sha — suite must fail if we stop requiring it."""
    with pytest.raises(ValueError):
        scans_svc.record_scan(
            db, project_id="core", scanner_id="pip-audit",
            path="backend/", scanned_sha="   ", findings=[],
        )
