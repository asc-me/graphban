"""D7 — the `wait_seconds` long-poll (GRPH-338 / PRD-17).

**Accept:** a parked worker returns within `wait_seconds` of an item becoming ready, and a
60-second park costs one tool call rather than twelve. `assign_role` against a parked agent
returns the directive in seconds, not at timeout.

Two properties carry it, and neither is about the happy path.

**No transaction is held while parked.** A fleet of agents sitting still must not consume the
connection pool — that would make this feature worse than the spinning it replaces, and it
would only show up under load, which is the worst time to learn it.

**A directive wakes the park early.** A re-tasked agent that stayed parked for its full minute
would keep working the old role for that minute, and the whole promise of D6 is that
reassignment lands on the next poll.
"""
import pytest

from app.models import Agent
from app.services import fleet


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _ok(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "LongPoll"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "poll", "project_id": proj},
                       headers=auth).json()["plaintext"]


# ---- the park itself ------------------------------------------------------------------------

def test_it_returns_the_moment_work_appears(db):
    """The point of parking: answer as soon as there is an answer, not at the deadline."""
    calls = {"n": 0}

    def attempt(_s):
        calls["n"] += 1
        return "an item" if calls["n"] >= 3 else None

    slept = []
    out = fleet.park(db, attempt, wait_seconds=30, sleep=slept.append)

    assert out == "an item"
    assert calls["n"] == 3 and len(slept) == 2, "woke on the answer, not the timeout"


def test_it_gives_up_at_the_deadline(db):
    out = fleet.park(db, lambda _s: None, wait_seconds=2, sleep=lambda _s: None)
    assert out is None


def test_wait_seconds_is_bounded(db):
    """An unbounded block is a connection an operator cannot reason about and a client cannot
    tell from a hang — and past 60s an edge proxy severs it rather than answering."""
    slept = []
    fleet.park(db, lambda _s: None, wait_seconds=9999, sleep=slept.append)

    assert sum(slept) <= fleet.MAX_WAIT_SECONDS


def test_no_wait_is_exactly_the_old_behaviour(db):
    """`wait_seconds` absent must not change a single existing call — every agent written
    before D7 keeps its semantics."""
    calls = {"n": 0}

    def attempt(_s):
        calls["n"] += 1
        return None

    slept = []
    assert fleet.park(db, attempt, sleep=slept.append) is None
    assert calls["n"] == 1 and slept == [], "one attempt, no sleeping"


def test_it_holds_no_transaction_while_parked(db):
    """THE property that decides whether this is safe under load. A parked agent holding a
    connection would exhaust the pool with a fleet doing nothing — and that failure appears
    only when several agents park at once, which is exactly when you cannot debug it."""
    seen = []

    def watching_sleep(_seconds):
        seen.append(db.in_transaction())

    db.execute(__import__("sqlalchemy").text("SELECT 1"))  # force a transaction open
    assert db.in_transaction(), "the fixture only means something if one was open"

    fleet.park(db, lambda _s: None, wait_seconds=3, sleep=watching_sleep)

    assert seen, "it should have slept at least once"
    assert not any(seen), "a transaction was held across a sleep"


# ---- the directive wakes it -------------------------------------------------------------------

def test_a_directive_wakes_the_park_early(client, key, db):
    """`assign_role` against a parked agent returns in seconds, not at timeout. Otherwise a
    re-tasked agent works the old role for a full minute and D6's promise is only true for
    agents that happened not to be parked."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    fleet.assign_role(db, agent_id=me["agent_id"], role="reviewer", reason="queue is deep")

    slept = []
    out = fleet.park(db, lambda _s: None, agent_id=me["agent_id"],
                     wait_seconds=60, sleep=slept.append)

    assert out is None
    assert slept == [], "it returned before sleeping at all"


def test_the_directive_is_still_delivered_exactly_once(client, key, db):
    """The park DETECTS the directive; the response envelope collects it. Acking in both
    places would consume it before the agent ever saw it — the wake-up eating the message it
    woke up for."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    fleet.assign_role(db, agent_id=me["agent_id"], role="reviewer")

    first = _ok(client, key, "claim_review",
                {"agent_id": me["agent_id"], "wait_seconds": 1})

    assert first["directive"]["role"] == "reviewer"
    second = _ok(client, key, "claim_review", {"agent_id": me["agent_id"]})
    assert "directive" not in second


def test_an_agent_with_no_directive_parks_normally(client, key, db):
    """The wake-up is for a real directive only. Waking on `None` would turn every park into
    an immediate return and quietly restore the spinning."""
    me = _ok(client, key, "register_agent", {"label": "w"})

    slept = []
    fleet.park(db, lambda _s: None, agent_id=me["agent_id"], wait_seconds=3,
               sleep=slept.append)

    assert slept, "it parked rather than returning at once"


# ---- wired to the claim tools -----------------------------------------------------------------

def test_claim_next_accepts_a_wait_and_still_answers(client, key):
    _ok(client, key, "create_item", {"title": "A", "status": "next"})
    me = _ok(client, key, "register_agent", {"label": "w"})

    out = _ok(client, key, "claim_next", {"agent_id": me["agent_id"], "wait_seconds": 2})

    assert out["claimed"] is True


def test_claim_cluster_parks_on_a_miss_rather_than_answering_instantly(client, key, db):
    """`claim_cluster` reports a miss as `claimed: False`, not None — a truthy dict. Without
    translating that the park would return "nothing available" immediately and never wait at
    all, which is the bug this test exists to catch."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    slept = []
    real_sleep = fleet.time.sleep
    fleet.time.sleep = slept.append
    try:
        out = _ok(client, key, "claim_cluster",
                  {"agent_id": me["agent_id"], "wait_seconds": 3})
    finally:
        fleet.time.sleep = real_sleep

    assert out["claimed"] is False
    assert out["reason"], "a miss still explains itself"
    assert slept, "an empty backlog should have parked, not answered instantly"


def test_claim_review_accepts_a_wait(client, key):
    me = _ok(client, key, "register_agent", {"label": "r", "role_hint": "reviewer"})

    out = _ok(client, key, "claim_review", {"agent_id": me["agent_id"], "wait_seconds": 1})

    assert out["claimed"] is False and out["reason"]
