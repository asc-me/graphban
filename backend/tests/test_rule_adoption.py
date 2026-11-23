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


def test_batch_refuses_loosens_then_alone_succeeds(client, env, monkeypatch):
    # GRPH-1873: this test is about proposal mechanics, not evaluation of the primitive.
    monkeypatch.setattr("app.services.custom_rules.EVALUATED", tuple(__import__("app.services.rule_pack", fromlist=["x"]).PRIMITIVES))
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


def test_library_turn_on_adoption_starts_observing(client, env, monkeypatch):
    """D5 (S5, GRPH-1777): adopting a library `turn_on` switches the rule on `observing`, so it
    measures and does not enforce."""
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
    assert a.status_code == 200, a.text
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        state = T.resolve(db, rule.id, env["pid"])
        assert state["on"]["value"] is True and state["state"] == "observing"
    finally:
        db.close()
    assert _events(env["pid"], "adopt_rule_proposal")[0]["state"] == "observing"


def test_library_tighten_adoption_keeps_an_observing_rule_observing(client, env, monkeypatch):
    """D5 (GRPH-1777): promotion is a separate person act. Adopting a `tighten` on an observing
    library rule changes its params and leaves it observing, from when it began."""
    from app.db import SessionLocal
    from app.services import rule_toggles as T
    from app.services.rule_toggles import LibraryRule
    rule = LibraryRule(id=T.EXCLUSIVE_AREAS_RULE, stage="build", title="Areas", summary="s",
                       enforcement="advisory", enforcer=None, default_on=False,
                       default_params={"paths": []})
    monkeypatch.setattr(T, "LIBRARY", {rule.id: rule})

    def adopt(kind, paths):
        r = client.post(_base(env), headers=env["h"], json={
            "kind": kind, "rule_id": rule.id, "params": {"paths": paths},
            "citations": env["cites"]})
        assert r.status_code == 201, r.text
        a = client.post(_base(env) + "/adopt", headers=env["h"],
                        json={"proposal_ids": [r.json()["id"]], "reason": kind})
        assert a.status_code == 200, a.text

    def state():
        db = SessionLocal()
        try:
            return T.resolve(db, rule.id, env["pid"]), T._row(db, rule.id, project_id=env["pid"])
        finally:
            db.close()

    adopt("turn_on", ["db"])
    _, row = state()
    since = row.observing_since
    assert since is not None
    adopt("tighten", ["db", "x"])
    resolved, row = state()
    assert resolved["state"] == "observing"
    assert resolved["params"]["value"]["paths"] == ["db", "x"]
    assert row.observing_since == since
    assert [m["state"] for m in _events(env["pid"], "adopt_rule_proposal")] == [
        "observing", "observing"]


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


def _rule_exists(pid, rule_id):
    from app.db import SessionLocal
    from app.models import CustomRule
    db = SessionLocal()
    try:
        return db.query(CustomRule).filter(CustomRule.project_id == pid,
                                           CustomRule.rule_id == rule_id).first() is not None
    finally:
        db.close()


def test_mixed_batch_refused_at_second_member_applies_nothing(client, env, monkeypatch):
    """All-or-nothing: a batch refused at its second member leaves the first proposal open and
    no rule created — recording an event commits, so nothing may record before the last check."""
    from app.services import rule_toggles as T
    from app.services.rule_toggles import LibraryRule
    rule = LibraryRule(id=T.EXCLUSIVE_AREAS_RULE, stage="build", title="Areas", summary="s",
                       enforcement="advisory", enforcer=None, default_on=False,
                       default_params={"paths": []})
    first = _agent_prop(env, client)
    monkeypatch.setattr(T, "LIBRARY", {rule.id: rule})
    second = client.post(_base(env), headers=env["h"], json={
        "kind": "turn_on", "rule_id": rule.id, "params": {"paths": ["db"]},
        "citations": env["cites"]})
    assert second.status_code == 201, second.text
    d = client.post(_base(env) + f"/{second.json()['id']}/dismiss", headers=env["h"],
                    json={"reason": "no"})
    assert d.status_code == 200, d.text  # a dismissed proposal is no longer open: refused at 2nd
    r = client.post(_base(env) + "/adopt", headers=env["h"],
                    json={"proposal_ids": [first, second.json()["id"]], "reason": "both"})
    assert r.status_code == 422, r.text
    assert not _rule_exists(env["pid"], "custom.areas")
    open_ids = {p["id"] for p in client.get(_base(env) + "?status=open", headers=env["h"]).json()}
    assert first in open_ids
    assert not _events(env["pid"], "adopt_rule_proposal")


def test_batch_with_duplicate_slug_refused_applies_nothing(client, env):
    a = _agent_prop(env, client)
    b = client.post(_base(env), headers=env["h"], json={
        "kind": "new_rule", "primitive": "exclusive_areas", "slug": "areas", "title": "A2",
        "why": "w", "params": {"paths": ["other"]}, "citations": env["cites"]})
    assert b.status_code == 201, b.text
    r = client.post(_base(env) + "/adopt", headers=env["h"],
                    json={"proposal_ids": [a, b.json()["id"]], "reason": "both"})
    assert r.status_code == 422 and "custom.areas" in r.text, r.text
    assert not _rule_exists(env["pid"], "custom.areas")
    assert len(client.get(_base(env) + "?status=open", headers=env["h"]).json()) == 2
    assert not _events(env["pid"], "adopt_rule_proposal")
