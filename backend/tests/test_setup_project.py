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


# ---- PRD-P67 S1a (GRPH-1360): one checklist, two surfaces, stored human decisions ----------

import os  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.db import engine as _app_engine  # noqa: E402


def _dialog(client, auth, pid):
    r = client.get(f"/api/projects/{pid}/setup", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, auth, pid, body):
    return client.post(f"/api/projects/{pid}/setup/confirm", json=body, headers=auth)


def _by_id(steps):
    return {s["id"]: s for s in steps}


def _assert_surfaces_agree(dialog, mcp):
    """THE RATCHET: every step the two surfaces share has the same id and status on both.

    Driven by `shared` on the dialog side AND by the ids on the MCP side, so a step dropped
    from either surface fails here rather than shrinking the comparison."""
    from app.services.setup import SHARED_STEP_IDS

    d = _by_id(dialog["steps"])
    m = _by_id(mcp["steps"])
    shared_in_dialog = {i for i, s in d.items() if s["shared"]}
    assert shared_in_dialog == set(SHARED_STEP_IDS) == set(m), (shared_in_dialog, set(m))
    for sid in SHARED_STEP_IDS:
        assert (d[sid]["id"], d[sid]["status"]) == (m[sid]["id"], m[sid]["status"]), sid


def test_dialog_checklist_has_steps_zero_to_four_with_three_valued_status(client, auth):
    proj = client.post("/api/projects", json={"name": "Dialog"}, headers=auth).json()
    out = _dialog(client, auth, proj["id"])
    assert [s["id"] for s in out["steps"]] == [
        "where", "project", "model", "connect", "code_graph", "memories", "items"]
    assert [s["step"] for s in out["steps"]] == [0, 1, 2, 3, 4, 4, 4]
    for s in out["steps"]:
        assert {"step", "status", "how"} <= set(s)
        assert s["status"] in ("done", "pending", "unknown")
        if s["status"] == "unknown":
            assert s["reason"]  # never an unexplained unknown
    st = {s["id"]: s["status"] for s in out["steps"]}
    # A fresh project: nothing decided, nothing built, and the stub running by DEFAULT is
    # not a decision — so the model step is pending, not done.
    assert st == {"where": "pending", "project": "pending", "model": "pending",
                  "connect": "unknown", "code_graph": "pending", "memories": "pending",
                  "items": "pending"}


def test_new_user_onboarding_is_unset(client, auth):
    """A user created after the migration has NULL onboarding — the dialog's trigger."""
    from app.db import SessionLocal
    from app.models import User

    s = SessionLocal()
    try:
        u = User(id=f"u_{uuid.uuid4().hex[:6]}", name="New", handle=f"n{uuid.uuid4().hex[:6]}",
                 email=f"{uuid.uuid4().hex[:6]}@x.dev", password_hash="x")
        s.add(u)
        s.commit()
        s.refresh(u)
        assert u.onboarding is None
    finally:
        s.close()
    assert "onboarding" in _dialog(client, auth, "core")


def test_a_check_that_cannot_run_is_unknown_never_done(client, auth, monkeypatch):
    from app.services import platform

    def boom(*a, **k):
        raise RuntimeError("credential store unreachable")

    monkeypatch.setattr(platform, "resolve_chat", boom)
    # Even a recorded "stay offline" must not turn a failed check into done.
    assert _confirm(client, auth, "core", {"decision": "stay_offline"}).status_code == 200
    model = _by_id(_dialog(client, auth, "core")["steps"])["model"]
    assert model["status"] == "unknown"
    assert "credential store unreachable" in model["reason"]


def test_ratchet_shared_steps_agree_across_dialog_and_mcp(client, auth):
    proj = client.post("/api/projects", json={"name": "Ratchet"}, headers=auth).json()
    pid = proj["id"]
    key = _key(client, auth, pid)
    _assert_surfaces_agree(_dialog(client, auth, pid), _sc(client, key, "setup_project"))
    # Move every shared step and check again: agreement in the all-pending state alone would
    # pass a projection that hard-coded "pending".
    assert _confirm(client, auth, pid, {"decision": "project_confirmed"}).status_code == 200
    _sc(client, key, "describe_code", {"nodes": [{"path": "x.py", "summary": "x"}]})
    _sc(client, key, "create_item", {"title": "first"})
    dialog, mcp = _dialog(client, auth, pid), _sc(client, key, "setup_project")
    _assert_surfaces_agree(dialog, mcp)
    st = {s["id"]: s["status"] for s in mcp["steps"]}
    assert st["project"] == "done" and st["code_graph"] == "done" and st["items"] == "done"
    assert st["memories"] == "pending"


def test_ratchet_fails_when_one_shared_step_disagrees(client, auth, monkeypatch):
    """Sabotage, kept as a test: make the MCP projection report one shared step differently
    and the ratchet must fail — otherwise it is a check that cannot fail."""
    from app.services import setup

    real = setup.checklist

    def skewed(db, project_id, *, user_id=None):
        out = real(db, project_id, user_id=user_id)
        for s in out["steps"]:
            if s["id"] == "code_graph":
                s["status"] = "done" if s["status"] != "done" else "pending"
        return out

    dialog = _dialog(client, auth, "core")
    monkeypatch.setattr(setup, "checklist", skewed)
    mcp = _sc(client, _key(client, auth, "core"), "setup_project")
    with pytest.raises(AssertionError):
        _assert_surfaces_agree(dialog, mcp)


def test_setup_project_keeps_its_old_shape_for_old_agents(client, auth):
    r = _sc(client, _key(client, auth, "core"), "setup_project")
    assert [s["step"] for s in r["steps"]] == [1, 2, 3, 4]
    assert {"project_id", "empty", "complete", "steps", "note"} <= set(r)
    for s in r["steps"]:
        assert {"step", "name", "status", "detail", "how"} <= set(s)


def test_project_confirmation_is_per_person(client, auth):
    pid = client.post("/api/projects", json={"name": "PerPerson"}, headers=auth).json()["id"]
    out = _confirm(client, auth, pid, {"decision": "project_confirmed"}).json()
    assert _by_id(out["steps"])["project"]["status"] == "done"
    from app.db import SessionLocal
    from app.services import setup

    s = SessionLocal()
    try:
        other = setup.checklist(s, pid, user_id="someone-else")
    finally:
        s.close()
    assert _by_id(other["steps"])["project"]["status"] == "pending"


def test_confirm_stores_each_decision_and_audits_it(client, auth):
    pid = client.post("/api/projects", json={"name": "Decide"}, headers=auth).json()["id"]
    bodies = [
        {"decision": "project_confirmed"},
        {"decision": "stay_offline"},
        {"decision": "where", "answer": "dedicated_server", "server_address": "http://srv:8080"},
    ]
    for b in bodies:
        r = _confirm(client, auth, pid, b)
        assert r.status_code == 200, r.text
    st = _by_id(r.json()["steps"])
    assert st["project"]["status"] == "done"
    assert st["model"]["status"] == "done" and "offline" in st["model"]["detail"]
    assert st["where"]["status"] == "done" and "http://srv:8080" in st["where"]["detail"]

    from app.db import SessionLocal
    from app.models import Event

    s = SessionLocal()
    try:
        rows = s.query(Event).filter(Event.project_id == pid, Event.action == "setup_confirm") \
            .order_by(Event.id).all()
        assert [e.meta["decision"] for e in rows] == ["project_confirmed", "stay_offline", "where"]
        assert rows[-1].meta["server_address"] == "http://srv:8080"
        assert all(e.actor_type == "user" for e in rows)
    finally:
        s.close()


@pytest.mark.parametrize("body", [
    {"decision": "quickstart_finished"},                           # UI progress is not a decision
    {"decision": "tour_completed_at"},
    {"decision": "project_confirmed", "extra": 1},                 # extra field
    {"decision": "stay_offline", "answer": "this_computer"},       # answer belongs to `where`
    {"decision": "where", "answer": "the_moon"},
    {"decision": "where", "answer": "dedicated_server"},           # address required
    {"decision": "where", "answer": "dedicated_server", "server_address": "  "},
    {"decision": "where", "answer": "this_computer", "server_address": "http://x"},
    {},
])
def test_confirm_refuses_anything_but_the_three_decisions(client, auth, body):
    r = _confirm(client, auth, "core", body)
    assert r.status_code == 422, r.text
    from app.db import SessionLocal
    from app.models import Event, Project

    s = SessionLocal()
    try:
        assert s.get(Project, "core").setup_decisions is None  # nothing stored
        assert s.query(Event).filter(Event.action == "setup_confirm").count() == 0
    finally:
        s.close()


def test_setup_endpoints_are_project_scoped(client, auth):
    assert client.get("/api/projects/nope/setup", headers=auth).status_code in (403, 404)
    assert _confirm(client, auth, "nope", {"decision": "stay_offline"}).status_code in (403, 404)


_IS_PG = _app_engine.url.drivername.startswith("postgresql")


@pytest.mark.skipif(not _IS_PG, reason=(
    "MIGRATION 0186 BACKFILL UNVERIFIED ON THIS ENGINE. Alembic only runs on Postgres; "
    "SQLite builds from create_all. CI runs this against Postgres."))
def test_migration_0186_backfills_existing_users_as_finished():
    from alembic import command
    from alembic.config import Config

    from app.config import settings
    from tests.conftest import _create_database

    base, _, _ = os.environ["DATABASE_URL"].rpartition("/")
    name = f"gb_mig_{uuid.uuid4().hex[:8]}"
    _create_database(f"{base}/postgres", name)
    url = f"{base}/{name}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    previous = settings.database_url
    settings.database_url = url
    eng = create_engine(url)
    try:
        command.upgrade(cfg, "0185")
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (id,name,handle,email,password_hash) "
                           "VALUES ('u_old','Old','old','old@x.dev','x')"))
        command.upgrade(cfg, "0186")
        with eng.begin() as c:
            ob = c.execute(text("SELECT onboarding FROM users WHERE id='u_old'")).scalar_one()
            assert ob["quickstart_finished_at"]  # existing user: finished, so no dialog
            c.execute(text("INSERT INTO users (id,name,handle,email,password_hash) "
                           "VALUES ('u_new','New','new','new@x.dev','x')"))
            assert c.execute(text(
                "SELECT onboarding FROM users WHERE id='u_new'")).scalar_one() is None
            assert c.execute(text(
                "SELECT setup_decisions FROM projects LIMIT 1")).fetchall() == []
    finally:
        settings.database_url = previous
        eng.dispose()
        admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            admin.dispose()
