"""P91 S4 (GRPH-1776): a person adopts, dismisses and batch-adopts proposals."""
import pytest

from tests.test_rule_proposals import _call, _login, _new_rule, env  # noqa: F401


def _base(env):
    return f"/api/projects/{env['pid']}/rules/proposals"


def _loosening(client, env, slug="gen"):
    r = client.post(_base(env), headers=env["h"], json={
        "kind": "new_rule", "primitive": "generated_files", "slug": slug, "title": "G",
        "why": "w", "params": {"paths": ["x"]}, "citations": env["cites"], "import_run": "r1"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _agent_prop(env, client):
    r = _call(client, env["key"], "propose_rule", _new_rule(env))
    assert not r.get("isError"), r
    return r["structuredContent"]["id"]


def _events(pid, action):
    from app.db import SessionLocal
    from app.models import Event
    db = SessionLocal()
    try:
        return [e.meta for e in db.query(Event).filter(Event.project_id == pid,
                                                      Event.action == action)]
    finally:
        db.close()


def test_admin_adopts_agent_proposal_observing_with_history(client, env):
    pid = _agent_prop(env, client)
    r = client.post(_base(env) + "/adopt", headers=env["h"],
                    json={"proposal_ids": [pid], "reason": "migrations keep clashing"})
    assert r.status_code == 200, r.text
    assert r.json()[0]["status"] == "adopted"
    rules = client.get(f"/api/projects/{env['pid']}/rules/custom", headers=env["h"]).json()
    rule = next(x for x in rules if x["rule_id"] == "custom.areas")
    assert rule["state"] == "observing" and rule["source"] == "custom"
    meta = _events(env["pid"], "adopt_rule_proposal")[0]
    assert "proposed by " in meta["history"] and "adopted by" in meta["history"]
    assert meta["reason"] == "migrations keep clashing"


def test_batch_refuses_loosens_then_alone_succeeds(client, env):
    a, g = _agent_prop(env, client), _loosening(client, env)
    r = client.post(_base(env) + "/adopt", headers=env["h"],
                    json={"proposal_ids": [a, g], "reason": "both"})
    assert r.status_code == 422 and str(g) in r.text and "loosens" in r.text
    assert client.get(_base(env) + "?status=open", headers=env["h"]).json().__len__() == 2
    ok = client.post(_base(env) + "/adopt", headers=env["h"],
                     json={"proposal_ids": [g], "reason": "generated dir is noisy"})
    assert ok.status_code == 200, ok.text


def test_batch_adopts_all_with_one_event(client, env):
    a = _agent_prop(env, client)
    b = client.post(_base(env), headers=env["h"], json={
        "kind": "new_rule", "primitive": "exclusive_areas", "slug": "other", "title": "O",
        "why": "w", "params": {"paths": ["zz"]}, "citations": env["cites"]}).json()["id"]
    r = client.post(_base(env) + "/adopt", headers=env["h"],
                    json={"proposal_ids": [a, b], "reason": "batch"})
    assert r.status_code == 200, r.text
    assert len(_events(env["pid"], "adopt_rule_proposals")) == 1


def test_agent_key_cannot_adopt_or_dismiss(client, env):
    pid = _agent_prop(env, client)
    k = {"X-API-Key": env["key"]}
    for url, body in ((_base(env) + "/adopt", {"proposal_ids": [pid], "reason": "r"}),
                      (f"{_base(env)}/{pid}/dismiss", {"reason": "r"})):
        r = client.post(url, headers=k, json=body)
        assert r.status_code == 403 and "rules.person_adopts" in r.text, r.text
    tools = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers=k).json()["result"]["tools"]
    assert not [t for t in tools if "adopt" in t["name"] or "dismiss" in t["name"]]


def test_dismissed_refiling_same_citations_refused_new_citation_accepted(client, env):
    pid = _agent_prop(env, client)
    d = client.post(f"{_base(env)}/{pid}/dismiss", headers=env["h"], json={"reason": "no"})
    assert d.status_code == 200 and d.json()["status"] == "dismissed"
    again = _call(client, env["key"], "propose_rule", _new_rule(env))
    assert again["isError"] is True and "dismissed" in str(again)
    fresh = _call(client, env["key"], "propose_rule", _new_rule(
        env, citations=env["cites"] + [{"kind": "url", "ref": "https://example.com/new"}]))
    assert not fresh.get("isError"), fresh
    assert fresh["structuredContent"]["id"] != pid


def test_dismiss_needs_reason(client, env):
    pid = _agent_prop(env, client)
    r = client.post(f"{_base(env)}/{pid}/dismiss", headers=env["h"], json={"reason": " "})
    assert r.status_code in (403, 422)
