"""`baseline_suggestions` names the `gbfleet bench run` command (GRPH-1863)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AttemptTelemetry
from app.services import harness as hsvc


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Sug"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "sug", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _register(client, key, label, caps):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "register_agent", "arguments": {
                                          "branch": "gb/t", "label": label,
                                          "capabilities": caps}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text


def test_a_new_binary_version_suggests_a_baseline_run_naming_it(client, key, db, proj):
    db.add(AttemptTelemetry(
        id="at-sug-1", project_id=proj, vendor="anthropic", model="sonnet",
        binary_version="1.0.0", capabilities=["A1"], outcome="signed_off",
        derived_at=datetime.now(timezone.utc) - timedelta(days=1)))
    db.commit()
    _register(client, key, "child", {"vendor": "anthropic", "model": "sonnet",
                                     "binary_version": "2.0.0"})
    hits = hsvc.baseline_suggestions(db, proj)
    assert len(hits) == 1
    assert hits[0]["trigger"] == "version_change"
    assert hits[0]["binary_version"] == "2.0.0"
    assert hits[0]["command"] == "gbfleet bench run --suite template --adapter anthropic:sonnet"


def test_a_new_vendor_model_with_no_cell_suggests_a_baseline_run(client, key, db, proj):
    _register(client, key, "fresh", {"vendor": "acme", "model": "nova",
                                     "binary_version": "1.0.0"})
    hit = hsvc.baseline_suggestions(db, proj)[0]
    assert hit["trigger"] == "new_row"
    assert hit["command"].endswith("--adapter acme:nova")
