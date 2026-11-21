"""AL-133: setup_project first-run bootstrap checklist + empty-project detection."""
import pytest


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
    # One node pinned to one revision is the whole graph: most nodes carry it, so the step is done.
    _sc(client, key, "describe_code", {
        "nodes": [{"path": "x.py", "summary": "x"}], "revision": "abc123"})
    setup = _sc(client, key, "setup_project")
    assert setup["empty"] is False
    graph = next(s for s in setup["steps"] if s["name"] == "Build the code graph")
    assert graph["status"] == "done"  # re-run reflects the new state
    assert graph["detail"] == "1 node, 1 at revision abc123."


def test_setup_project_is_read_only_and_in_a_read_key_manifest(client, auth):
    key = _key(client, auth, "core", scopes=["read"])
    tools = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"X-API-Key": key}).json()["result"]["tools"]
    assert "setup_project" in {t["name"] for t in tools}  # visible to a read-only key


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
    assert [s["step"] for s in body["steps"]] == [0, 1, 2, 3, 4, 5]
    assert {s["status"] for s in body["steps"]} == {"pending"}


def test_a_check_that_cannot_run_is_unknown_with_a_reason_never_done(db, monkeypatch):
    from app.services import setup as svc

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(svc, "_n", boom)
    steps = svc.checklist(db, "core")["steps"]
    for s in steps[2:5]:
        assert s["status"] == "unknown" and "db down" in s["reason"]


def _assert_shared_steps_agree(client, auth, key, pid):
    """The ratchet: every step on BOTH the dialog's checklist and `setup_project` carries the
    same id, name and status. Steps only one surface shows (⓪) are not compared."""
    dialog = {s["step"]: (s["name"], s["status"]) for s in _api_steps(client, auth, pid)["steps"]}
    agent = {s["step"]: (s["name"], s["status"]) for s in _sc(client, key, "setup_project")["steps"]}
    shared = set(dialog) & set(agent)
    assert shared == {1, 2, 3, 4}, f"shared steps changed: {sorted(shared)}"
    diff = {k: (dialog[k], agent[k]) for k in shared if dialog[k] != agent[k]}
    assert not diff, f"dialog and setup_project disagree: {diff}"
    return dialog


def _ratchet_project(client, auth, name):
    pid = client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]
    key = _key(client, auth, pid)
    _sc(client, key, "describe_code", {
        "nodes": [{"path": "x.py", "summary": "x"}], "revision": "abc123"})
    client.post(f"/api/projects/{pid}/setup/confirm", json={"decision": "workspace"}, headers=auth)
    return pid, key


def test_ratchet_shared_steps_agree_on_dialog_and_setup_project(client, auth):
    pid, key = _ratchet_project(client, auth, "Ratchet")
    dialog = _assert_shared_steps_agree(client, auth, key, pid)
    # a mix of done and pending, so agreement is not the trivial all-pending case
    assert dialog[1][1] == "done" and dialog[2][1] == "done" and dialog[3][1] == "pending"


def test_ratchet_fails_when_a_shared_step_disagrees(client, auth, monkeypatch):
    """Sabotage: `setup_project` reports a different status for one shared step; the ratchet
    above must catch it through the real MCP surface."""
    from app.services import setup as svc
    pid, key = _ratchet_project(client, auth, "Sabotage")
    real = svc.agent_view

    def lying(db_, project_id):
        out = real(db_, project_id)
        out["steps"][2] = {**out["steps"][2], "status": "done"}  # step ③ claims done
        return out
    monkeypatch.setattr(svc, "agent_view", lying)
    with pytest.raises(AssertionError, match="disagree"):
        _assert_shared_steps_agree(client, auth, key, pid)


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


def test_reconfirming_a_confirmed_project_is_a_noop(client, auth, db):
    from sqlalchemy import select

    from app.models import Event
    pid = client.post("/api/projects", json={"name": "Again"}, headers=auth).json()["id"]
    url = f"/api/projects/{pid}/setup/confirm"
    first = client.post(url, json={"decision": "workspace"}, headers=auth)
    for _ in range(3):
        assert client.post(url, json={"decision": "workspace"}, headers=auth).status_code == 200
    rows = db.scalars(select(Event).where(Event.project_id == pid, Event.action == "setup_confirm")).all()
    assert len(rows) == 1
    assert first.json()["steps"][1]["status"] == "done"


def test_new_user_onboarding_is_unset(db):
    from app.models import User
    u = User(id="u-new", name="N", handle="newbie", email="n@example.com", password_hash="x")
    db.add(u)
    db.commit()
    assert db.get(User, "u-new").onboarding is None


# ---- migration 0186 ----------------------------------------------------------------

def test_migration_backfills_existing_users_as_finished():
    import importlib.util
    import pathlib

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect, text

    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0186_setup_onboarding.py"
    spec = importlib.util.spec_from_file_location("migration_0186", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id VARCHAR PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE projects (id VARCHAR PRIMARY KEY)"))
        conn.execute(text("INSERT INTO users VALUES ('u1'), ('u2')"))

    def run(fn):
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                fn()

    run(mod.upgrade)
    import json
    with engine.begin() as conn:
        rows = dict(conn.execute(text("SELECT id, onboarding FROM users")).all())
        assert set(rows) == {"u1", "u2"}
        for v in rows.values():
            assert json.loads(v)["quickstart_finished_at"]
        conn.execute(text("INSERT INTO users (id) VALUES ('u3')"))  # created after: unset
        assert conn.execute(text("SELECT onboarding FROM users WHERE id='u3'")).scalar() is None

    run(mod.downgrade)
    assert "onboarding" not in {c["name"] for c in inspect(engine).get_columns("users")}


def test_build_the_code_graph_step_names_gban_code_map(client, auth):
    """GRPH-1706: step ② names the CLI that builds and refreshes the graph, dry-run first,
    and keeps the LOCAL-FIRST / quota guidance."""
    step = next(s for s in _sc(client, _key(client, auth, "core"), "setup_project")["steps"]
                if s["name"] == "Build the code graph")
    assert "gban code map" in step["how"]
    assert "gban code map --dry-run" in step["how"]
    assert "LOCAL-FIRST" in step["how"] and "AL-134" in step["how"]


def _graph_step(client, auth, pid):
    key = _key(client, auth, pid)
    agent = next(s for s in _sc(client, key, "setup_project")["steps"]
                 if s["name"] == "Build the code graph")
    dialog = next(s for s in _api_steps(client, auth, pid)["steps"] if s["step"] == 2)
    return agent, dialog


def _seed_nodes(db, pid, n, *, revision="", updated_at=None, prefix="n"):
    from app.models import CodeNode
    for i in range(n):
        db.add(CodeNode(
            id=f"cn_{prefix}_{i:04d}", project_id=pid, path=f"{prefix}/{i}.py", kind="file",
            revision=revision, **({"updated_at": updated_at} if updated_at is not None else {}),
        ))
    db.commit()


def test_unrevisioned_nodes_do_not_read_as_a_built_graph(client, auth, db):
    """GRPH-1707: 211 described nodes and no pinned revision is a start, not a finished graph.

    The detail names that coverage. Restoring the count>0 check marks the step done and
    this test fails — on setup_project and on the dialog checklist, which share the step.
    """
    pid = client.post("/api/projects", json={"name": "Unrevisioned"}, headers=auth).json()["id"]
    _seed_nodes(db, pid, 211, prefix="unrev")
    agent, dialog = _graph_step(client, auth, pid)
    assert agent["status"] == "pending" and dialog["status"] == "pending"
    assert agent["detail"] == "211 nodes, 0 at a revision."
    assert dialog["detail"] == agent["detail"]
    assert "gban code map" in agent["how"]


def test_fewer_than_most_nodes_at_the_latest_revision_is_not_done(client, auth, db):
    """A newer revision on a minority of nodes is a partial refresh, not coverage."""
    from datetime import datetime, timedelta, timezone

    from app.services.setup import CODE_GRAPH_MOST

    pid = client.post("/api/projects", json={"name": "Minority"}, headers=auth).json()["id"]
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = old + timedelta(days=1)
    _seed_nodes(db, pid, 4, revision="aaa111", updated_at=old, prefix="old")
    _seed_nodes(db, pid, 1, revision="bbb222", updated_at=new, prefix="new")
    assert 1 / 5 <= CODE_GRAPH_MOST
    agent, dialog = _graph_step(client, auth, pid)
    assert agent["status"] == "pending" and dialog["status"] == "pending"
    assert agent["detail"] == "5 nodes, 1 at revision bbb222."


def test_exactly_half_the_nodes_at_the_latest_revision_is_not_most(client, auth, db):
    from datetime import datetime, timedelta, timezone

    from app.services.setup import CODE_GRAPH_MOST

    pid = client.post("/api/projects", json={"name": "Half"}, headers=auth).json()["id"]
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = old + timedelta(days=1)
    _seed_nodes(db, pid, 1, revision="aaa111", updated_at=old, prefix="old")
    _seed_nodes(db, pid, 1, revision="bbb222", updated_at=new, prefix="new")
    assert 1 / 2 <= CODE_GRAPH_MOST  # "most" is strictly more than the named threshold
    agent, _dialog = _graph_step(client, auth, pid)
    assert agent["status"] == "pending"
    assert agent["detail"] == "2 nodes, 1 at revision bbb222."


def test_most_nodes_at_the_latest_revision_is_done(client, auth, db):
    from datetime import datetime, timedelta, timezone

    from app.services.setup import CODE_GRAPH_MOST

    pid = client.post("/api/projects", json={"name": "Majority"}, headers=auth).json()["id"]
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = old + timedelta(days=1)
    _seed_nodes(db, pid, 1, revision="aaa111", updated_at=old, prefix="old")
    _seed_nodes(db, pid, 3, revision="bbb222", updated_at=new, prefix="new")
    assert 3 / 4 > CODE_GRAPH_MOST
    agent, dialog = _graph_step(client, auth, pid)
    assert agent["status"] == "done" and dialog["status"] == "done"
    assert agent["detail"] == "4 nodes, 3 at revision bbb222."
