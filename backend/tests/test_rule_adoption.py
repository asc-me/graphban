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


def test_library_turn_on_adoption_refused_not_enforcing(client, env, monkeypatch):
    """D5: a library rule cannot start observing before S5, so adopting its `turn_on` is
    refused rather than switching it on enforcing."""
    from app.services import rule_toggles as T
    from app.services.rule_toggles import LibraryRule
    rule = LibraryRule(id=T.EXCLUSIVE_AREAS_RULE, stage="build", title="Areas", summary="s",
                       enforcement="advisory", enforcer=None, default_on=False,
                       default_params={"paths": []})
    monkeypatch.setattr(T, "LIBRARY", {rule.id: rule})
    r = client.post(_base(env), headers=env["h"], json={
        "kind": "turn_on", "rule_id": rule.id, "params": {"paths": ["db"]},
        "citations": env["cites"]})
    assert r.status_code == 201, r.text
    prop = r.json()["id"]
    a = client.post(_base(env) + "/adopt", headers=env["h"],
                    json={"proposal_ids": [prop], "reason": "turn it on"})
    assert a.status_code == 422 and "GRPH-1777" in a.text and rule.id in a.text, a.text
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        assert T.resolve(db, rule.id, env["pid"])["on"]["value"] is False
    finally:
        db.close()
    assert client.get(_base(env) + "?status=open", headers=env["h"]).json()[0]["id"] == prop
    assert not _events(env["pid"], "adopt_rule_proposal")


def test_tighten_adoption_records_real_state(client, env):
    h = env["h"]
    ok = client.post(f"/api/projects/{env['pid']}/rules/custom", headers=h, json={
        "slug": "live", "primitive": "exclusive_areas", "title": "L", "why": "w",
        "params": {"paths": ["a"]}, "reason": "r"})
    assert ok.status_code == 201, ok.text
    t = _call(client, env["key"], "propose_rule", {
        "kind": "tighten", "draft": {"rule_id": "custom.live", "params": {"paths": ["a", "b"]}},
        "citations": env["cites"]})
    assert not t.get("isError"), t
    r = client.post(_base(env) + "/adopt", headers=h,
                    json={"proposal_ids": [t["structuredContent"]["id"]], "reason": "more"})
    assert r.status_code == 200, r.text
    rules = client.get(f"/api/projects/{env['pid']}/rules/custom", headers=h).json()
    live = next(x for x in rules if x["rule_id"] == "custom.live")
    meta = _events(env["pid"], "adopt_rule_proposal")[0]
    assert meta["state"] == live["state"] == "active"
