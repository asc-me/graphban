"""PRD-34 PR 1 — the observed feed. Every call is a row; silence is a word; nothing is guessed.

Each test names the acceptance criterion it pins (§7) and, where it matters, the sabotage
that was run against it. Rows are inspected through a session opened AFTER the request so
the app's own commit is what is being read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from app.mcp_server import TOOLS
from app.models import Agent, AgentCall, Event, Project
from app.services import agent_calls as calls_svc


def _mcp(client, key, name, args=None, session=None):
    headers = {"X-API-Key": key}
    if session:
        headers["Mcp-Session-Id"] = session
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(result) -> dict:
    assert not result.get("isError"), result
    return result["structuredContent"]


@pytest.fixture()
def key(client, auth):
    return client.post("/api/api-keys", json={"name": "feed-key", "project_id": "core"},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _rows(db, tool=None) -> list[AgentCall]:
    db.expire_all()
    stmt = select(AgentCall).order_by(AgentCall.id)
    if tool:
        stmt = stmt.where(AgentCall.tool == tool)
    return db.scalars(stmt).all()


# ---- A1, A2: reads and refusals are rows; the audit ledger is untouched --------------------

def test_a_read_is_a_row_and_not_an_event(client, key, db):
    """A1. Sabotage: gate the feed write on `_READ_ONLY` — this fails."""
    _ok(_mcp(client, key, "search_items", {"query": "reservation lease"}))
    rows = _rows(db, "search_items")
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "observed"
    assert row.target == "reservation lease"
    assert row.ok is True
    assert row.project_id == "core"
    audited = db.scalars(select(Event).where(Event.action == "search_items")).all()
    assert audited == [], "reads must not grow the audit ledger"


def test_a_refused_call_is_a_row_with_its_code(client, key, db):
    """A2 / D13. Sabotage: record only on the success path — this fails."""
    res = _mcp(client, key, "get_item_details", {"id": "CORE-999999"})
    assert res.get("isError")
    rows = _rows(db, "get_item_details")
    assert len(rows) == 1
    assert rows[0].ok is False
    assert rows[0].error_code == "not_found"
    assert rows[0].target == "CORE-999999"


def test_a_mutation_is_both_a_row_and_an_event(client, key, db):
    created = _ok(_mcp(client, key, "create_item", {"title": "feed me", "project_id": "core"}))
    assert _rows(db, "create_item")[0].target == created["id"]
    assert db.scalars(select(Event).where(Event.action == "create_item")).first() is not None


# ---- A3, A4, A23: attribution, never a guess ---------------------------------------------------

def test_attributed_by_agent_id_argument(client, key, db):
    """A3. Sabotage: attribute to the key — this fails."""
    reg = _ok(_mcp(client, key, "register_agent", {"label": "w1", "project_id": "core"}))
    _ok(_mcp(client, key, "search_items", {"query": "by arg", "agent_id": reg["agent_id"]}))
    row = _rows(db, "search_items")[-1]
    assert row.agent_id == reg["agent_id"]


def test_attributed_by_session_when_exactly_one_live_agent(client, key, db):
    """A4 + A23. One live agent on the connection: attributed. Two: NULL, and NEITHER agent's
    feed gains the row. Sabotage: pick the first — this fails."""
    a1 = _ok(_mcp(client, key, "register_agent", {"label": "s1", "project_id": "core"},
                  session="sess-1"))["agent_id"]
    _ok(_mcp(client, key, "search_items", {"query": "one live"}, session="sess-1"))
    assert _rows(db, "search_items")[-1].agent_id == a1

    a2 = _ok(_mcp(client, key, "register_agent", {"label": "s2", "project_id": "core"},
                  session="sess-1"))["agent_id"]
    _ok(_mcp(client, key, "search_items", {"query": "two live"}, session="sess-1"))
    row = _rows(db, "search_items")[-1]
    assert row.agent_id is None
    assert row.api_key_id is not None
    feeds = [calls_svc.feed(db, "core", a) for a in (a1, a2)]
    assert all("two live" not in [r["target"] for r in f["rows"]] for f in feeds)


def test_a_foreign_agent_id_is_not_trusted(client, key, db, auth):
    """An `agent_id` naming an agent on ANOTHER credential does not attribute."""
    other = client.post("/api/api-keys", json={"name": "other", "project_id": "core"},
                        headers=auth).json()["plaintext"]
    theirs = _ok(_mcp(client, other, "register_agent", {"label": "o", "project_id": "core"}))
    _ok(_mcp(client, key, "search_items", {"query": "spoof", "agent_id": theirs["agent_id"]}))
    assert _rows(db, "search_items")[-1].agent_id is None


# ---- A5, A15: unattributed is counted on the board, per credential -----------------------------

def test_unattributed_calls_are_counted_on_the_board_by_key(client, key, auth, db):
    """A5 / D15. Sabotage: filter NULL rows out of `summary` — this fails."""
    _ok(_mcp(client, key, "search_items", {"query": "nobody"}))
    _ok(_mcp(client, key, "search_items", {"query": "nobody again"}))
    board = client.get("/api/live?project_id=core", headers=auth).json()
    # No agent ever registered on this key: the group still exists, with an empty roster.
    groups = {u["label"]: u for u in board["users"]}
    owner = [u for u in board["users"] if u["unattributed_calls"]]
    assert owner, board
    g = owner[0]
    assert g["unattributed_calls"] == 2
    assert g["unattributed_by_key"] == [{"key": "feed-key", "calls": 2}]
    assert g["agents"] == []
    assert "window_seconds" in board and "retention_days" in board
    assert groups  # the census still lists everyone


# ---- A9, A16: silence is a word; the board composes, it does not query ------------------------

def test_never_is_a_word_on_board_and_feed(client, key, auth, db):
    """A9. Sabotage: `rows: []` with `state: "ok"` — this fails."""
    db.add(Agent(id="core-quiet", project_id="core", number=990, label="quiet"))
    db.commit()
    board = client.get("/api/live?project_id=core", headers=auth).json()
    rows = [a for u in board["users"] for a in u["agents"] if a["id"] == "core-quiet"]
    assert rows, board
    a = rows[0]
    assert a["call_state"] == "never"
    assert a["last_call"] is None
    assert a["silence_seconds"] is None
    assert a["calls_in_window"] == 0
    assert a["status"] is None and a["status_state"] == "unreported"
    feed = client.get("/api/live/core-quiet/feed?project_id=core", headers=auth).json()
    assert feed["state"] == "never"
    assert feed["rows"] == []
    assert feed["retention_days"] == calls_svc.retention_days()


def test_active_and_quiet_derive_from_the_latest_row(client, key, auth, db):
    """A? / D21: state is the latest row's age against one heartbeat interval."""
    reg = _ok(_mcp(client, key, "register_agent", {"label": "w", "project_id": "core"}))
    _ok(_mcp(client, key, "search_items", {"query": "now", "agent_id": reg["agent_id"]}))
    board = client.get("/api/live?project_id=core", headers=auth).json()
    a = [x for u in board["users"] for x in u["agents"] if x["id"] == reg["agent_id"]][0]
    assert a["call_state"] == "active"
    assert a["last_call"]["tool"] == "search_items"
    assert a["last_call"]["target"] == "now"
    assert a["last_call"]["ok"] is True
    assert a["calls_in_window"] >= 1
    # Age the row past one interval: quiet, with the silence stated.
    row = _rows(db, "search_items")[-1]
    row.ts = datetime.now(timezone.utc) - timedelta(seconds=10_000)
    db.commit()
    board = client.get("/api/live?project_id=core", headers=auth).json()
    a = [x for u in board["users"] for x in u["agents"] if x["id"] == reg["agent_id"]][0]
    assert a["call_state"] == "quiet"
    assert a["silence_seconds"] >= 10_000


def test_the_board_and_router_do_not_query_the_table():
    """A16 / A12. The service is the one place that reads AgentCall."""
    live_src = open("app/services/live.py", encoding="utf-8").read()
    router_src = open("app/routers/live.py", encoding="utf-8").read()
    assert "calls_svc.summary" in live_src
    assert "AgentCall" not in live_src
    assert "AgentCall" not in router_src and "Agent" not in router_src


def test_the_feed_is_newest_first_and_capped(client, key, auth, db):
    reg = _ok(_mcp(client, key, "register_agent", {"label": "w", "project_id": "core"}))
    for i in range(3):
        _ok(_mcp(client, key, "search_items", {"query": f"q{i}", "agent_id": reg["agent_id"]}))
    feed = client.get(f"/api/live/{reg['agent_id']}/feed?project_id=core&limit=2",
                      headers=auth).json()
    assert feed["state"] == "ok"
    assert [r["target"] for r in feed["rows"]] == ["q2", "q1"]
    assert all(r["source"] == "observed" for r in feed["rows"])


# ---- A11, A12: one target string, never the arguments -----------------------------------------

def test_no_arguments_are_stored(client, key, db):
    """A11. Sabotage: store `args` in `target` — this fails."""
    text = "SECRET-LESSON " + ("x" * 2000)
    res = _mcp(client, key, "add_memory", {"text": text, "project_id": "core"})
    _ok(res)
    for row in _rows(db):
        assert "SECRET-LESSON" not in (row.target or "")
        assert len(row.target or "") <= calls_svc.TARGET_MAX
    src = open("app/mcp_server.py", encoding="utf-8").read()
    site = src[src.index("calls_svc.record("):]
    site = site[:site.index("duration_ms")]
    # The extractor may READ args to pick one string; the row must never be handed them.
    assert "args=" not in site and "meta=" not in site, "record() must never store the arguments"


@pytest.mark.parametrize("name", sorted(t["name"] for t in TOOLS))
def test_every_tool_has_an_extractor_answer(name):
    """A12. Sabotage: an extractor that indexes args["id"] unguarded — this fails."""
    assert isinstance(calls_svc.target_for(name, {}, None), str)
    assert isinstance(calls_svc.target_for(name, {"id": 7, "query": None, "prd_id": []}, {"id": 3}), str)
    assert isinstance(calls_svc.target_for(name, None, "not a dict"), str)


def test_a_claim_names_its_result_and_an_update_names_the_move():
    assert calls_svc.target_for("claim_next", {}, {"id": "CORE-4"}) == "CORE-4"
    assert calls_svc.target_for("claim_cluster", {}, {"cluster": [{"id": "CORE-4"}, {"id": "CORE-5"}]}) == "CORE-4, CORE-5"
    assert calls_svc.target_for("update_item", {"id": "CORE-4", "status": "review"}, None) == "CORE-4 → review"
    assert calls_svc.target_for("search_code", {"query": "x" * 500}, None) == "x" * calls_svc.TARGET_MAX


# ---- A13, A22: the feed never fails the call; a failed sweep is counted -----------------------

def test_a_feed_write_failure_does_not_fail_the_call(client, key, db, monkeypatch):
    """A13. Sabotage: remove the guard in `_record_call` — this fails."""
    def boom(*a, **k):
        raise RuntimeError("feed down")
    monkeypatch.setattr(calls_svc, "record", boom)
    _ok(_mcp(client, key, "search_items", {"query": "still fine"}))
    assert _rows(db, "search_items") == []


def test_a_sweep_failure_is_counted_on_health(client, key, db, monkeypatch):
    """A22 / D18. Sabotage: bare `except: pass` around the sweep — this fails."""
    def boom(*a, **k):
        raise RuntimeError("sweep down")
    monkeypatch.setattr(calls_svc, "sweep", boom)
    monkeypatch.setattr(calls_svc, "SWEEP_EVERY", 1)
    before = calls_svc.SWEEP_FAILED
    _ok(_mcp(client, key, "search_items", {"query": "trigger"}))
    assert calls_svc.SWEEP_FAILED == before + 1
    assert _rows(db, "search_items"), "the row itself still lands"
    assert client.get("/health").json()["agent_calls_sweep_failed"] == before + 1


# ---- A14: retention -----------------------------------------------------------------------------

def test_retention_sweeps_by_age(client, key, db, auth):
    """A14. Sabotage: compare with the wrong sign — this fails (the young row would go)."""
    from app.models import ApiKey
    k = db.scalars(select(ApiKey).where(ApiKey.name == "feed-key")).first()
    now = datetime.now(timezone.utc)
    db.add(AgentCall(project_id="core", api_key_id=k.id, agent_id=None, tool="old",
                     ts=now - timedelta(days=8)))
    db.add(AgentCall(project_id="core", api_key_id=k.id, agent_id=None, tool="young",
                     ts=now - timedelta(days=6)))
    db.commit()
    r = client.post("/api/live/sweep?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1
    tools = {row.tool for row in _rows(db)}
    assert "young" in tools and "old" not in tools


# ---- A15: JWT only, project-bound ---------------------------------------------------------------

def test_feed_is_jwt_only_and_project_bound(client, key, auth, db):
    """A15. Sabotage: `get_user_or_agent_key` on the route — the 401 half fails."""
    reg = _ok(_mcp(client, key, "register_agent", {"label": "w", "project_id": "core"}))
    r = client.get(f"/api/live/{reg['agent_id']}/feed?project_id=core", headers={"X-API-Key": key})
    assert r.status_code == 401
    db.add(Project(id="elsewhere", name="Elsewhere", tag="ELSE"))
    db.add(Agent(id="else-a1", project_id="elsewhere", number=1, label="x"))
    db.commit()
    r = client.get("/api/live/else-a1/feed?project_id=core", headers=auth)
    assert r.status_code == 404


# ---- A17: Activity names the agent -------------------------------------------------------------

def test_activity_names_the_agent_not_only_the_key(client, key, auth, db):
    """A17. Sabotage: drop `meta.agent_id` — this fails."""
    reg = _ok(_mcp(client, key, "register_agent", {"label": "w", "project_id": "core"}))
    item = _ok(_mcp(client, key, "create_item", {"title": "audited", "project_id": "core"}))
    _ok(_mcp(client, key, "update_item", {"id": item["id"], "title": "audited twice",
                                         "agent_id": reg["agent_id"]}))
    ev = db.scalars(select(Event).where(Event.action == "update_item")
                    .order_by(Event.id.desc())).first()
    assert ev is not None and (ev.meta or {}).get("agent_id") == reg["agent_id"]
    listed = client.get("/api/events?project_id=core&action=update_item", headers=auth).json()
    top = listed["results"][0]
    assert top["agent"] == reg["agent_id"]


# ---- A20: two statements per board --------------------------------------------------------------

def test_summary_is_two_statements_whatever_the_agent_count(client, key, db):
    """A20 / D19. Sabotage: loop per agent — this fails."""
    from app.db import engine
    ids = [f"core-a{i}" for i in range(50)]
    seen = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)
    event.listen(engine, "before_cursor_execute", _count)
    try:
        calls_svc.summary(db, "core", ids, window_seconds=600, interval_seconds=60)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert len(seen) == 2, seen


# ---- D6: presence-only heartbeats are not rows --------------------------------------------------

def test_a_successful_heartbeat_is_not_an_observed_row(client, key, db):
    reg = _ok(_mcp(client, key, "register_agent", {"label": "w", "project_id": "core"}))
    _ok(_mcp(client, key, "heartbeat", {"agent_id": reg["agent_id"]}))
    assert _rows(db, "heartbeat") == []
    res = _mcp(client, key, "heartbeat", {"agent_id": reg["agent_id"], "id": "CORE-999999"})
    assert res.get("isError")
    assert [r.ok for r in _rows(db, "heartbeat")] == [False]
