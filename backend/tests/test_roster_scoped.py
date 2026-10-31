"""GRPH-1253 — `/api/fleet` and `/api/live` load only the agents their payload shows.

Both routes used to build every agent (≈6,400 on the live instance) and then drop almost all of
them. The roster is now split: the shown agents are loaded in full, the rest are counted in SQL.

`tests/snapshots/grph1253.json` was written by THE PRE-CHANGE CODE (5ca60d7a) on the fixture
below, so the snapshot test compares the scoped paths against the whole-roster ones they
replace. Regenerate only on purpose: `GRPH1253_WRITE_SNAPSHOT=1 pytest tests/test_roster_scoped.py`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.models import Agent, ApiKey, Item
from app.services import fleet as fleet_svc

SNAPSHOT = Path(__file__).parent / "snapshots" / "grph1253.json"
OLD = 2000
SHOWN = 11   # 3 online + 2 holding + 1 dismissed + 5 recent-offline


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def roster(client, auth, db):
    proj = client.post("/api/projects", json={"name": "Scoped"}, headers=auth).json()["id"]
    client.post("/api/api-keys", json={"name": "k", "project_id": proj,
                                       "scopes": ["read", "write"]}, headers=auth)
    key_id = db.scalar(select(ApiKey.id).order_by(ApiKey.created_at.desc()))
    items = [client.post("/api/items", json={"title": f"item {i}", "project_id": proj},
                         headers=auth).json()["id"] for i in range(2)]
    now = datetime.now(timezone.utc)
    n = 0

    def add(label, *, seen, role="worker", branch="", state="idle", dismissed=False):
        nonlocal n
        n += 1
        a = Agent(id=f"agent-{n:05d}", project_id=proj, number=n, api_key_id=key_id,
                  label=label, active_role=role, branch=branch, state=state,
                  last_seen_at=seen, registered_at=seen,
                  dismissed_at=now if dismissed else None)
        db.add(a)
        return a.id

    online = [add(f"on{i}", seen=now - timedelta(seconds=5), role=r)
              for i, r in enumerate(("worker", "planner", "all-in-one"))]
    holders = [add(f"hold{i}", seen=now - timedelta(seconds=10)) for i in range(2)]
    dismissed = add("dismissed", seen=now - timedelta(days=2), dismissed=True)
    recent = [add(f"recent{i}", seen=now - timedelta(minutes=10 + i), branch="")
              for i in range(5)]
    for i in range(OLD):
        add(f"old{i}", seen=now - timedelta(hours=5, seconds=i),
            role=("worker", "reviewer", "planner")[i % 3], branch=f"gb/old-{i}" if i % 4 == 0 else "")
    db.flush()
    for it_id, holder, col in ((items[0], holders[0], "claimed_by"),
                               (items[1], holders[1], "review_claimed_by")):
        item = db.get(Item, it_id)
        setattr(item, col, holder)
        item.status = "in_progress" if col == "claimed_by" else "review"
    db.commit()
    shown = set(online + holders + [dismissed] + recent)
    return proj, shown


def _loaded_agent_ids(db, call):
    db.expunge_all()
    seen: set[str] = set()

    def _load(target, context):
        seen.add(target.id)

    event.listen(Agent, "load", _load)
    try:
        out = call()
    finally:
        event.remove(Agent, "load", _load)
    return seen, out


def test_scoped_roster_loads_only_the_shown_agents(db, roster):
    proj, shown = roster
    ids, status = _loaded_agent_ids(
        db, lambda: fleet_svc.fleet_status(db, proj, collapsed=True))
    assert ids == shown and len(ids) == SHOWN
    assert {a["id"] for a in status["agents"]} == shown
    assert status["total"] == SHOWN + OLD
    # One old agent in four left a branch behind, so it is orphaned rather than quiet.
    assert status["collapsed"]["by_kind"] == {"quiet": OLD - OLD // 4, "orphaned": OLD // 4}
    assert status["collapsed"]["count"] == OLD


def test_scoped_counts_equal_the_whole_roster_ones(db, roster):
    proj, _ = roster
    want = fleet_svc.collapse_roster(fleet_svc.fleet_status(db, proj))
    got = fleet_svc.fleet_status(db, proj, collapsed=True)
    for k in ("total", "online", "by_role", "posture", "collapsed"):
        assert got[k] == want[k], k
    assert got["agents"] == want["agents"]


def test_collapsed_page_is_exact_and_loads_only_the_page(db, roster):
    proj, _ = roster
    for kind in (None, "quiet", "orphaned", "nonsense"):
        db.expunge_all()
        want_all = [a for a in fleet_svc.list_agents(db, proj)
                    if fleet_svc._collapse_kind(
                        a, datetime.now(timezone.utc) - timedelta(
                            seconds=fleet_svc.ROSTER_RECENT_SECONDS))
                    and (kind is None or fleet_svc._collapse_kind(
                        a, datetime.now(timezone.utc) - timedelta(
                            seconds=fleet_svc.ROSTER_RECENT_SECONDS)) == kind)]
        ids, page = _loaded_agent_ids(
            db, lambda: fleet_svc.collapsed_agents(db, proj, offset=10, limit=25, kind=kind))
        assert page["total"] == len(want_all)
        assert page["agents"] == want_all[10:35]
        assert len(ids) == len(page["agents"]) <= 25


def test_live_loads_online_plus_the_cap_not_every_agent(db, roster):
    from app.services import live as live_svc

    proj, _ = roster
    ids, board = _loaded_agent_ids(db, lambda: live_svc.board(db, proj, cap=7))
    assert len(ids) <= 3 + 2 + 7, len(ids)
    assert board["total_agents"] == SHOWN - 1 + OLD   # dismissed is not on the board
    assert board["online_agents"] == 5


def test_mcp_fleet_status_still_returns_every_agent(db, roster):
    proj, _ = roster
    status = fleet_svc.fleet_status(db, proj)
    assert len(status["agents"]) == status["total"] == SHOWN + OLD
    assert "collapsed" not in status


def test_sabotage_loading_everything_fails_the_rows_loaded_test(db, roster, monkeypatch):
    """Load all agents in the scoped path again: the rows-loaded assertion must notice."""
    proj, shown = roster
    real = fleet_svc.list_agents
    monkeypatch.setattr(fleet_svc, "list_agents",
                        lambda *a, **k: real(*a, **{**k, "where": None}))
    ids, _ = _loaded_agent_ids(db, lambda: fleet_svc.fleet_status(db, proj, collapsed=True))
    assert ids != shown and len(ids) > SHOWN


# ---- the snapshot: scoped output is main's output --------------------------------------------

_VOLATILE = {"last_seen_at", "registered_at", "served_at", "expires_at", "created_at",
             "claimed_at", "silence_seconds", "last_call", "at", "status_at", "seen_at",
             "credential"}


def _norm(obj, names):
    if isinstance(obj, dict):
        return {k: _norm(v, names) for k, v in obj.items() if k not in _VOLATILE}
    if isinstance(obj, list):
        return [_norm(v, names) for v in obj]
    return names.get(obj, obj) if isinstance(obj, str) else obj


def _payloads(client, auth, db, proj):
    names = {proj: "PROJECT"}
    for i in db.scalars(select(Item).where(Item.project_id == proj)).all():
        names[i.id] = f"item:{i.key}"
    for k in db.scalars(select(ApiKey)).all():
        names[k.id] = f"key:{k.name}"
    get = lambda path, **p: client.get(path, params={"project_id": proj, **p}, headers=auth).json()  # noqa: E731
    fleet = get("/api/fleet")
    drop = ("matrix", "mix", "measured", "probe_suggestions", "tier_map", "credentials",
            "release_ref", "waves", "profile", "policy")
    fleet = {k: v for k, v in fleet.items() if k not in drop}
    return _norm({
        "fleet": fleet,
        "live": get("/api/live"),
        "collapsed_p1": get("/api/fleet/agents/collapsed", offset=0, limit=100),
        "collapsed_orphaned_p2": get("/api/fleet/agents/collapsed", offset=100, limit=100,
                                     kind="orphaned"),
    }, names)


def test_outputs_match_the_pre_change_snapshot(client, auth, db, roster):
    proj, _ = roster
    got = json.loads(json.dumps(_payloads(client, auth, db, proj), default=str))
    if os.environ.get("GRPH1253_WRITE_SNAPSHOT"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        pytest.skip("snapshot written")
    want = json.loads(SNAPSHOT.read_text())
    assert want["fleet"]["collapsed"]["count"] == OLD and want["live"]["users"]
    for part in want:
        assert got[part] == want[part], part


# ---- live with several users: the cap is filled group by group (bounce of 4b77e5db) ----------

MULTI_SNAPSHOT = Path(__file__).parent / "snapshots" / "grph1253_multi.json"


@pytest.fixture()
def two_users(client, auth, db):
    """u_a and u_b, 30 offline agents each; u_a's were seen 10h ago, u_b's 2h ago.

    A project-wide "cap most recently seen" pick takes only u_b's, yet the unfiltered board
    puts u_a first and `user_filter=u_a` shows only u_a. Plus one online agent per user.
    """
    from app.models import User

    proj = client.post("/api/projects", json={"name": "Two users"}, headers=auth).json()["id"]
    now = datetime.now(timezone.utc)
    keys = {}
    for uid, name in (("u_a", "Alice"), ("u_b", "Bob")):
        db.add(User(id=uid, name=name, handle=uid, email=f"{uid}@example.test",
                    password_hash="x"))
        db.add(ApiKey(id=f"key-{uid}", user_id=uid, project_id=proj, name=f"k-{uid}",
                      prefix=f"gb_sk_{uid}", hashed_key=f"h-{uid}", scopes=["read", "write"]))
        keys[uid] = f"key-{uid}"
    db.flush()
    n = 0
    for uid, ago in (("u_a", timedelta(hours=10)), ("u_b", timedelta(hours=2))):
        for i in range(30):
            n += 1
            seen = now - ago - timedelta(seconds=i)
            db.add(Agent(id=f"agent-{uid}-{i:02d}", project_id=proj, number=n,
                         api_key_id=keys[uid], label=f"{uid}-{i}", active_role="worker",
                         branch="", state="idle", last_seen_at=seen, registered_at=seen))
        n += 1
        db.add(Agent(id=f"agent-{uid}-on", project_id=proj, number=n, api_key_id=keys[uid],
                     label=f"{uid}-on", active_role="worker", branch="", state="idle",
                     last_seen_at=now, registered_at=now))
    db.commit()
    return proj


_BOARDS = {"all": {}, "viewer_u_b": {"viewer_id": "u_b"}, "filter_u_a": {"user_filter": "u_a"},
           "filter_u_b": {"user_filter": "u_b"}}


def _boards(db, proj, **extra):
    from app.services import live as live_svc

    return {name: _norm(live_svc.board(db, proj, cap=10, **kw, **extra), {proj: "PROJECT"})
            for name, kw in _BOARDS.items()}


def test_live_filter_shows_the_filtered_users_agents(db, two_users):
    from app.services import live as live_svc

    board = live_svc.board(db, two_users, cap=10, user_filter="u_a")
    [u] = board["users"]
    assert u["user_id"] == "u_a" and u["total"] == 31 and u["online"] == 1
    assert [a["label"] for a in u["agents"]] == ["u_a-on"] + [f"u_a-{i}" for i in range(9)]
    board = live_svc.board(db, two_users, cap=10)
    assert [u["user_id"] for u in board["users"]] == ["u_a"]
    assert len(board["users"][0]["agents"]) == 10


def test_live_scoped_matches_the_whole_roster_board_for_every_group(db, two_users):
    """The whole-roster board (injected list_agents: main's path) is the oracle."""
    whole = _boards(db, two_users, list_agents=fleet_svc.list_agents)
    for name, got in _boards(db, two_users).items():
        assert got == whole[name], name


def test_live_scoped_loads_online_plus_the_cap_per_user(db, two_users):
    from app.services import live as live_svc

    ids, _ = _loaded_agent_ids(db, lambda: live_svc.board(db, two_users, cap=10))
    assert len(ids) == 2 + 2 * 10, len(ids)


def test_live_multi_user_matches_the_pre_change_snapshot(db, two_users):
    got = json.loads(json.dumps(_boards(db, two_users), default=str))
    if os.environ.get("GRPH1253_WRITE_SNAPSHOT"):
        MULTI_SNAPSHOT.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        pytest.skip("snapshot written")
    want = json.loads(MULTI_SNAPSHOT.read_text())
    assert want["filter_u_a"]["users"][0]["agents"]
    for name in want:
        assert got[name] == want[name], name
