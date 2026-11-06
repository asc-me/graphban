"""AL-133: setup_project first-run bootstrap checklist + empty-project detection."""


def _key(client, auth, project_id, scopes=None):
    body = {"name": "agent", "project_id": project_id}
    if scopes:
        body["scopes"] = scopes
    return client.post("/api/api-keys", json=body, headers=auth).json()["plaintext"]


def _sc(client, key, tool, args=None):
    return client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key}).json()["result"]["structuredContent"]


def test_setup_project_lists_the_four_v1_steps(client, auth):
    r = _sc(client, _key(client, auth, "core"), "setup_project")
    assert [s["name"] for s in r["steps"]] == [
        "Confirm the project", "Build the code graph", "Load memories", "Propose work items"]
    assert r["project_id"] == "core"
    # sources are surfaced + extensible per D2
    assert "AGENTS.md" in r["steps"][2]["sources"]


def test_empty_project_flags_pending_and_get_context_empty(client, auth):
    proj = client.post("/api/projects", json={"name": "Fresh"}, headers=auth).json()
    key = _key(client, auth, proj["id"])
    setup = _sc(client, key, "setup_project")
    assert setup["empty"] is True and setup["complete"] is False
    assert all(s["status"] == "pending" for s in setup["steps"][1:])  # steps 2–4 pending
    assert _sc(client, key, "get_context")["empty"] is True  # the first-run signal


def test_checklist_reflects_progress_resumably(client, auth):
    proj = client.post("/api/projects", json={"name": "Grows"}, headers=auth).json()
    key = _key(client, auth, proj["id"])
    _sc(client, key, "describe_code", {"nodes": [{"path": "x.py", "summary": "x"}]})
    setup = _sc(client, key, "setup_project")
    assert setup["empty"] is False
    graph = next(s for s in setup["steps"] if s["name"] == "Build the code graph")
    assert graph["status"] == "done"  # re-run reflects the new state


def test_setup_project_is_read_only_and_in_a_read_key_manifest(client, auth):
    key = _key(client, auth, "core", scopes=["read"])
    tools = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"X-API-Key": key}).json()["result"]["tools"]
    assert "setup_project" in {t["name"] for t in tools}  # visible to a read-only key


import pytest


@pytest.fixture
def db(client):  # `client` first: it creates the schema and seeds the demo project
    from app.db import SessionLocal
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ---- GRPH-1360: the checklist service, its endpoints, and the onboarding column ----

def _api_steps(client, auth, pid):
    return client.get(f"/api/projects/{pid}/setup", headers=auth).json()


def test_checklist_has_steps_zero_to_four_with_server_state_statuses(client, auth):
    proj = client.post("/api/projects", json={"name": "Fresh2"}, headers=auth).json()
    body = _api_steps(client, auth, proj["id"])
    assert [s["step"] for s in body["steps"]] == [0, 1, 2, 3, 4]
    assert {s["status"] for s in body["steps"]} == {"pending"}


def test_a_check_that_cannot_run_is_unknown_with_a_reason_never_done(db, monkeypatch):
    from app.services import setup as svc

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(svc, "_n", boom)
    steps = svc.checklist(db, "core")["steps"]
    for s in steps[2:]:
        assert s["status"] == "unknown" and "db down" in s["reason"]


def test_ratchet_shared_steps_agree_on_dialog_and_setup_project(client, auth):
    proj = client.post("/api/projects", json={"name": "Ratchet"}, headers=auth).json()
    pid = proj["id"]
    key = _key(client, auth, pid)
    _sc(client, key, "describe_code", {"nodes": [{"path": "x.py", "summary": "x"}]})
    client.post(f"/api/projects/{pid}/setup/confirm", json={"decision": "workspace"}, headers=auth)
    dialog = {s["step"]: s["status"] for s in _api_steps(client, auth, pid)["steps"]}
    agent = {s["step"]: s["status"] for s in _sc(client, key, "setup_project")["steps"]}
    assert agent and set(agent) == {1, 2, 3, 4}
    assert all(dialog[k] == v for k, v in agent.items())
    assert dialog[1] == "done" and dialog[2] == "done"


def test_ratchet_fails_when_a_shared_step_disagrees(db, monkeypatch):
    """Sabotage: the agent projection reports a different status for one shared step."""
    from app.services import setup as svc
    real = svc.agent_view

    def lying(db_, pid):
        out = real(db_, pid)
        out["steps"][0] = {**out["steps"][0], "status": "done"}
        return out
    monkeypatch.setattr(svc, "agent_view", lying)
    dialog = {s["step"]: s["status"] for s in svc.checklist(db, "core")["steps"]}
    agent = {s["step"]: s["status"] for s in svc.agent_view(db, "core")["steps"]}
    assert any(dialog[k] != v for k, v in agent.items())


def test_confirm_stores_only_the_three_decisions_and_audits_each(client, auth):
    proj = client.post("/api/projects", json={"name": "Confirm"}, headers=auth).json()
    pid = proj["id"]
    url = f"/api/projects/{pid}/setup/confirm"
    assert client.post(url, json={"decision": "workspace"}, headers=auth).status_code == 200
    assert client.post(url, json={"decision": "offline"}, headers=auth).status_code == 200
    r = client.post(url, json={"decision": "server", "where": "dedicated_server",
                               "address": "gb.example.com"}, headers=auth)
    assert r.status_code == 200
    steps = {s["step"]: s["status"] for s in r.json()["steps"]}
    assert steps[0] == "done" and steps[1] == "done" and r.json()["offline"] is True
    for bad in ({"decision": "tour_done"}, {"decision": "workspace", "extra": 1},
                {"decision": "server", "where": "moon"},
                {"decision": "server", "where": "dedicated_server"}):
        assert client.post(url, json=bad, headers=auth).status_code == 422


def test_confirm_writes_an_events_row_per_decision(client, auth, db):
    from sqlalchemy import select

    from app.models import Event
    proj = client.post("/api/projects", json={"name": "Audit"}, headers=auth).json()
    pid = proj["id"]
    for body in ({"decision": "workspace"}, {"decision": "offline"},
                 {"decision": "server", "where": "this_computer"}):
        client.post(f"/api/projects/{pid}/setup/confirm", json=body, headers=auth)
    rows = db.scalars(select(Event).where(Event.project_id == pid, Event.action == "setup_confirm")).all()
    assert sorted(r.meta["decision"] for r in rows) == ["offline", "server", "workspace"]


def test_new_user_onboarding_is_unset(db):
    from app.models import User
    u = User(id="u-new", name="N", handle="newbie", email="n@example.com", password_hash="x")
    db.add(u)
    db.commit()
    assert db.get(User, "u-new").onboarding is None
