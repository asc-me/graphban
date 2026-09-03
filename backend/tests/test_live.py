"""PRD-33 D5 — one Live aggregation. Absence is a named third state."""
from datetime import datetime, timezone

import pytest

from app.mcp_server import TOOLS
from app.models import Agent, ApiKey, Item, Project, User
from app.services import fleet
from app.services import live as live_svc


def _mcp(client, key, tool, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()
    assert "error" not in r, r
    return r["result"]["structuredContent"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(db):
    db.add(Project(id="livep", name="Live", tag="LV"))
    db.commit()
    return "livep"


@pytest.fixture()
def owner_key(db, proj):
    owner = User(id="u_live", name="Live Owner", handle="liveo", email="liveo@example.com",
                 initials="LO", password_hash="x")
    db.add(owner)
    db.flush()
    row = ApiKey(id="k_live", user_id=owner.id, project_id=proj, name="live-key",
                 prefix="gb_sk_live", hashed_key="x", scopes=["read", "write"], roles=[])
    db.add(row)
    db.commit()
    return row


def _agent_dict(**over):
    base = {
        "id": "a1", "key": "LV-A1", "label": "w", "active_role": "worker",
        "state": "working", "dismissed": False, "last_seen_at": "2026-09-02T12:00:00",
        "worktree": None, "branch": None, "branch_orphaned": False, "holdings": [],
        "credential": "gb_sk_live",
    }
    base.update(over)
    return base


def test_unattributed_is_on_the_default_board(db, proj, owner_key):
    """Sabotage: omit agents with no user from All. The board must not get quieter."""
    named = fleet.register_agent(db, project_id=proj, api_key=owner_key, label="named")
    orphan = fleet.register_agent(db, project_id=proj, api_key=owner_key, label="orphan")
    row = db.get(Agent, orphan.id)
    row.api_key_id = None
    db.commit()

    got = live_svc.board(db, proj)
    ids = {u["user_id"] for u in got["users"]}
    assert None in ids
    assert got["unattributed_count"] == 1
    labels = {u["label"] for u in got["users"]}
    assert "Unattributed" in labels
    assert named.id in {a["id"] for u in got["users"] for a in u["agents"]}
    assert orphan.id in {a["id"] for u in got["users"] for a in u["agents"]}


def test_unreserved_is_not_idle(db, proj):
    """claim_next default-install hole, moved to a list (D3 sabotage)."""
    holdings = [{"id": "LV-1", "stored_id": "it1", "title": "t", "status": "in_progress",
                 "phase": "building", "phase_basis": "x"}]
    roster = [_agent_dict(holdings=holdings)]
    got = live_svc.board(
        db, proj,
        list_agents=lambda *a, **k: roster,
        held_areas=lambda *a, **k: {"held": [], "off_map": []},
    )
    agent = got["users"][0]["agents"][0]
    assert agent["file_state"] == "unreserved"
    assert agent["file_state"] != "idle"
    assert agent["holdings"][0]["pr"]["state"] == "unrecorded"


def test_declared_kind_does_not_win_file_state():
    """Sabotage: put declared in the D3 priority table. Unreserved must still win."""
    files = [{"area": "web/src", "kind": "declared", "reason": None, "node_paths": []}]
    holdings = [{"id": "LV-1"}]
    assert live_svc._file_state("working", holdings, files) == "unreserved"
    assert live_svc._file_state("working", holdings, files) != "leased"


def test_declared_files_on_unreserved_are_not_leased(db, proj):
    """PR 3: ghost touchpoints stay declared. Mixing them into leased is the lie."""
    db.add(Item(id="it1", project_id=proj, number=1, title="t", status="in_progress",
                touchpoints=["web/src/live.ts", "backend/app/services/live.py"]))
    db.commit()
    holdings = [{"id": "LV-1", "stored_id": "it1", "title": "t", "status": "in_progress",
                 "phase": "building", "phase_basis": "x"}]
    got = live_svc.board(
        db, proj,
        list_agents=lambda *a, **k: [_agent_dict(holdings=holdings)],
        held_areas=lambda *a, **k: {"held": [], "off_map": []},
    )
    agent = got["users"][0]["agents"][0]
    assert agent["file_state"] == "unreserved"
    kinds = {f["kind"] for f in agent["files"]}
    assert kinds == {"declared"}
    assert "leased" not in kinds
    areas = {f["area"] for f in agent["files"]}
    assert areas == {"web/src/live.ts", "backend/app/services/live.py"}


def test_declared_does_not_appear_when_leased(db, proj):
    db.add(Item(id="it1", project_id=proj, number=1, title="t", status="in_progress",
                touchpoints=["web/src/live.ts"]))
    db.commit()
    holdings = [{"id": "LV-1", "stored_id": "it1", "title": "t", "status": "in_progress",
                 "phase": "building", "phase_basis": "x"}]
    got = live_svc.board(
        db, proj,
        list_agents=lambda *a, **k: [_agent_dict(id="a1", holdings=holdings)],
        held_areas=lambda *a, **k: {
            "held": [{"agent_id": "a1", "area": "web/src", "predicted": False,
                      "node_paths": []}],
            "off_map": [],
        },
    )
    agent = got["users"][0]["agents"][0]
    assert agent["file_state"] == "leased"
    assert all(f["kind"] != "declared" for f in agent["files"])


def test_by_role_is_the_full_census(db, proj):
    roster = [
        _agent_dict(id="a1", active_role="worker"),
        _agent_dict(id="a2", active_role="reviewer"),
        _agent_dict(id="a3", active_role="all-in-one"),
    ]
    got = live_svc.board(
        db, proj, user_filter="missing",
        list_agents=lambda *a, **k: roster,
        held_areas=lambda *a, **k: {"held": [], "off_map": []},
    )
    assert got["users"] == []
    assert got["by_role"]["worker"] == 1
    assert got["by_role"]["reviewer"] == 1
    assert got["by_role"]["all-in-one"] == 1
    assert got["by_role"]["planner"] == 0


def test_truncation_is_stated(db, proj, owner_key):
    roster = [_agent_dict(id=f"a{i}", key=f"LV-A{i}") for i in range(3)]
    got = live_svc.board(
        db, proj, cap=2,
        list_agents=lambda *a, **k: roster,
        held_areas=lambda *a, **k: {"held": [], "off_map": []},
    )
    assert got["truncated"] is True
    assert got["total_agents"] == 3
    n = sum(len(u["agents"]) for u in got["users"])
    assert n == 2
    assert n < got["total_agents"]
    # census is the full set even when payload is sliced
    assert sum(c["total"] for c in got["user_counts"]) == 3


def test_filter_does_not_shrink_census(db, proj, owner_key):
    fleet.register_agent(db, project_id=proj, api_key=owner_key, label="named")
    orphan = fleet.register_agent(db, project_id=proj, api_key=owner_key, label="orphan")
    db.get(Agent, orphan.id).api_key_id = None
    db.commit()
    got = live_svc.board(db, proj, user_filter="unattributed")
    assert all(u["user_id"] is None for u in got["users"])
    assert got["unattributed_count"] == 1
    assert any(c["user_id"] == "u_live" for c in got["user_counts"])


def test_no_get_live_mcp_tool():
    names = {t["name"] for t in TOOLS}
    assert "get_live" not in names


def test_live_py_does_not_fetch_a_forge():
    src = open("app/services/live.py", encoding="utf-8").read()
    assert "httpx" not in src
    assert "urllib" not in src
    assert "github.com" not in src.lower()


def test_rest_is_jwt_only(client, auth):
    r = client.get("/api/live?project_id=core")
    assert r.status_code in (401, 403)
    # An agent key must not see the board (D5 / A8).
    key = client.post("/api/api-keys", json={"name": "k", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    r = client.get("/api/live?project_id=core", headers={"X-API-Key": key})
    assert r.status_code in (401, 403)


def test_rest_call_returns_the_board(client, auth):
    """THE CALL. Deleting the router would 404 while unit tests on board() still pass."""
    r = client.get("/api/live?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "users" in body
    assert "user_counts" in body
    assert "unattributed_count" in body
    assert "truncated" in body
    assert "heartbeat_interval_seconds" in body
    assert "presence_ttl_seconds" in body
    assert "by_role" in body
    assert "roles" in body


def test_held_areas_failure_is_500(client, auth, monkeypatch):
    """D14 CALL: a partial picture is worse than a slow one."""
    def boom(*a, **k):
        raise RuntimeError("areas down")
    monkeypatch.setattr("app.services.live.fleet_svc.held_areas", boom)
    r = client.get("/api/live?project_id=core", headers=auth)
    assert r.status_code == 500


def test_router_does_not_query_agent():
    src = open("app/routers/live.py", encoding="utf-8").read()
    assert "live_svc.board" in src
    assert "Agent" not in src
