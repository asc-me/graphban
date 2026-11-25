"""GRPH-1871: MCP get_rule_toggles must not 500 once a rule is observing (datetime `since`)."""
from datetime import datetime

import pytest

from app.db import SessionLocal
from app.models import RuleToggle, utcnow
from app.services import code_graph
from app.services import rule_toggles as T
from app.services import rules as R

RULE = T.EXCLUSIVE_AREAS_RULE


@pytest.fixture()
def setup(client, monkeypatch):
    monkeypatch.setattr(code_graph, "search_code", lambda db, q, pid, top_k=5: [])
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="plan", title="Exclusive file areas", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=True, default_params={"paths": ["db/migrations"]}))
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=h).json()["id"]
    key = client.post("/api/api-keys", json={"name": "k", "project_id": pid,
                                             "tool_tiers": ["misc"]}, headers=h).json()["plaintext"]
    return pid, key


def _call(client, key, pid):
    return client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_rule_toggles", "arguments": {"project_id": pid}}},
        headers={"X-API-Key": key})


def test_observing_rule_serialises_since_as_iso(client, setup):
    pid, key = setup
    s = SessionLocal()
    try:
        s.add(RuleToggle(rule_id=RULE, project_id=pid, on=True, observing_since=utcnow()))
        s.commit()
    finally:
        s.close()
    resp = _call(client, key, pid)
    assert resp.status_code == 200, resp.text
    res = resp.json()["result"]
    assert not res.get("isError"), res
    t = next(x for x in res["structuredContent"]["toggles"] if x["rule_id"] == RULE)
    assert t["state"] == "observing"
    since = t["observation"]["since"]
    assert isinstance(since, str)
    datetime.fromisoformat(since)


def test_no_observing_rule_unchanged(client, setup):
    pid, key = setup
    res = _call(client, key, pid).json()["result"]
    assert not res.get("isError"), res
    t = next(x for x in res["structuredContent"]["toggles"] if x["rule_id"] == RULE)
    assert t["state"] != "observing" and "observation" not in t
