"""D6 — allocation and the directive downlink (GRPH-337 / PRD-17).

**Accept:** 4 agents / ready work in 3 non-colliding clusters proposes 3 workers + 1 reviewer
with a cluster each. Drop a worker → the next `propose_allocation` reflects 3. Adding a fifth
agent with no free cluster proposes it as a second REVIEWER, not a fourth worker. Re-tasking a
live worker to reviewer takes effect **on that agent's next poll**, with no reconnect and no
re-prime.

Two ideas carry the slice.

**The server proposes; the planner commits.** Nothing in `propose_allocation` writes a role.
A proposal that assigned itself would make the Fleet view's diff a formality and take the
decision from the only actor positioned to weigh it.

**The downlink is the poll response.** MCP is client→server — the server cannot wake an idle
terminal, and no mainstream client starts working because a notification arrived. So intent
rides back on whatever the agent polls next, and `role_assigned_at > role_acked_at` is the
entire mechanism. No queue table, because the comparison IS the outbox.
"""
import pytest

from app.models import Agent, ApiKey
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
    """Its own project — the seeded backlog would supply extra ready clusters and make every
    'how many workers' assertion count somebody else's work."""
    return client.post("/api/projects", json={"name": "FleetAlloc"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "alloc", "project_id": proj},
                       headers=auth).json()["plaintext"]


# Distinct TOP-LEVEL directories. `area/0` and `area/1` are NOT disjoint: the partition
# relates two paths that share a parent directory, so those land in one cluster — which is
# what the first version of this fixture got wrong, proposing 1 worker instead of 3.
_AREAS = ["alpha/one.py", "beta/two.py", "gamma/three.py", "delta/four.py", "epsilon/five.py"]


def _clusters(client, key, n):
    """`n` items with genuinely disjoint touch-areas — so `n` non-colliding clusters."""
    for i in range(n):
        _ok(client, key, "create_item",
            {"title": f"work {i}", "status": "next", "touchpoints": [_AREAS[i]]})


def _agents(client, key, n):
    return [_ok(client, key, "register_agent", {"label": f"a{i}"}) for i in range(n)]


# ---- the proposal ---------------------------------------------------------------------------

def test_four_agents_and_three_clusters_propose_three_workers_and_a_reviewer(client, key):
    """THE acceptance criterion. The fourth agent has no free cluster, so it reviews rather
    than queueing for work that collides."""
    _clusters(client, key, 3)
    _agents(client, key, 4)

    out = _ok(client, key, "propose_allocation", {})

    assert out["workers"] == 3 and out["reviewers"] == 1
    workers = [m for m in out["mapping"] if m["role"] == "worker"]
    assert all(m["cluster"] for m in workers), "each worker gets a cluster"
    assert len({tuple(m["cluster"]) for m in workers}) == 3, "and never the same one twice"


def test_a_fifth_agent_becomes_a_second_reviewer_not_a_fourth_worker(client, key):
    """A worker with no non-colliding cluster is an agent the divvy refuses every time it
    asks. Reviewing is where the fleet is actually short — the review queue is what backs up
    when workers outnumber the work."""
    _clusters(client, key, 3)
    _agents(client, key, 5)

    out = _ok(client, key, "propose_allocation", {})

    assert out["workers"] == 3 and out["reviewers"] == 2


def test_dropping_an_agent_reflects_in_the_next_proposal(client, key, db):
    """Re-proposal when an agent joins or drops — the roster is derived, so the proposal moves
    with it without anything being told."""
    _clusters(client, key, 3)
    agents = _agents(client, key, 4)
    assert _ok(client, key, "propose_allocation", {})["workers"] == 3

    from datetime import datetime, timedelta, timezone

    dead = db.get(Agent, agents[0]["agent_id"])
    dead.last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=fleet.presence_ttl_seconds() + 5)
    db.commit()

    out = _ok(client, key, "propose_allocation", {})
    assert out["workers"] + out["reviewers"] == 3, "the dead agent is not allocated"


def test_a_lone_agent_is_a_worker_and_the_rationale_says_why(client, key):
    """With one agent there is nobody to review for, so proposing a reviewer would idle the
    only pair of hands in the room."""
    _clusters(client, key, 1)
    _agents(client, key, 1)

    out = _ok(client, key, "propose_allocation", {})

    assert out["workers"] == 1 and out["reviewers"] == 0
    assert "nobody to review" in out["rationale"]


def test_an_empty_fleet_proposes_nothing_and_says_so(client, key):
    """`0` with a reason, rather than a plan for agents that do not exist."""
    out = _ok(client, key, "propose_allocation", {})
    assert out["workers"] == 0 and "no agents online" in out["rationale"]


def test_the_proposal_writes_nothing(client, key, db):
    """The server proposes; the planner commits. A proposal that assigned itself would make
    the Fleet view's diff a formality."""
    _clusters(client, key, 2)
    agents = _agents(client, key, 3)
    before = {a["agent_id"]: db.get(Agent, a["agent_id"]).active_role for a in agents}

    _ok(client, key, "propose_allocation", {})

    for a in agents:
        db.refresh(db.get(Agent, a["agent_id"]))
        assert db.get(Agent, a["agent_id"]).active_role == before[a["agent_id"]]


# ---- the downlink -----------------------------------------------------------------------------

def test_a_role_change_reaches_the_agent_on_its_next_poll(client, key, db):
    """No reconnect, no re-prime, no new transport. The agent finds out by calling whatever it
    was going to call anyway."""
    _clusters(client, key, 1)
    me = _ok(client, key, "register_agent", {"label": "w"})
    fleet.assign_role(db, agent_id=me["agent_id"], role="reviewer",
                      reason="review queue is 4 deep")

    polled = _ok(client, key, "fleet_status", {"agent_id": me["agent_id"]})

    assert polled["directive"]["type"] == "role_change"
    assert polled["directive"]["role"] == "reviewer"
    assert polled["directive"]["reason"] == "review queue is 4 deep"
    assert "claim_review" in polled["directive"]["next"], "machine-readable next step"


def test_a_directive_is_delivered_once(client, key, db):
    """Acked on delivery. A directive redelivered forever is worse than one delivered once —
    the agent would keep re-adopting a role it already holds."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    fleet.assign_role(db, agent_id=me["agent_id"], role="reviewer")

    first = _ok(client, key, "fleet_status", {"agent_id": me["agent_id"]})
    second = _ok(client, key, "fleet_status", {"agent_id": me["agent_id"]})

    assert "directive" in first
    assert "directive" not in second


def test_a_second_assignment_replaces_an_uncollected_one(client, key, db):
    """At most one outstanding directive per agent, ever — it is not a queue. Delivering a
    superseded instruction first would have the agent adopt a role the planner has already
    changed its mind about."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    fleet.assign_role(db, agent_id=me["agent_id"], role="reviewer")
    fleet.assign_role(db, agent_id=me["agent_id"], role="planner")

    polled = _ok(client, key, "fleet_status", {"agent_id": me["agent_id"]})

    assert polled["directive"]["role"] == "planner"
    assert "directive" not in _ok(client, key, "fleet_status", {"agent_id": me["agent_id"]})


def test_an_agent_with_no_directive_gets_a_clean_response(client, key):
    """The envelope is optional. Adding an empty `directive` to every response would make an
    agent branch on a key that means nothing."""
    me = _ok(client, key, "register_agent", {"label": "w"})

    assert "directive" not in _ok(client, key, "fleet_status", {"agent_id": me["agent_id"]})


def test_the_role_change_actually_binds_the_gate(client, key, db):
    """The refusal path is the backstop: an agent that ignores its directive and calls a
    worker tool anyway is told the role it has NOW."""
    _clusters(client, key, 1)
    me = _ok(client, key, "register_agent", {"label": "w"})
    fleet.assign_role(db, agent_id=me["agent_id"], role="reviewer")

    res = _rpc(client, key, "claim_next", {"agent_id": me["agent_id"]})

    assert res["structuredContent"]["error"]["code"] == "unauthorized"
    assert "reviewer" in res["structuredContent"]["error"]["message"]


def test_a_directive_cannot_climb_past_the_credential(client, auth, proj, db):
    """The key is the ceiling and a planner reshuffles within it. Otherwise the planner could
    issue a role the agent is refused for on every call — a fleet arguing with itself while
    both halves believe they are right."""
    raw = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "worker", "wave": "w1"},
                      headers=auth).json()["plaintext"]
    me = _ok(client, raw, "register_agent", {"label": "w"})

    from app.security import authz

    with pytest.raises(authz.Forbidden):
        fleet.assign_role(db, agent_id=me["agent_id"], role="reviewer")


def test_assign_role_is_the_planners(client, key, db):
    """A worker that could re-task itself is a worker that promotes itself out of review."""
    me = _ok(client, key, "register_agent", {"label": "w", "role_hint": "worker"})

    res = _rpc(client, key, "assign_role",
               {"agent_id": me["agent_id"], "target_agent_id": me["agent_id"],
                "role": "reviewer"})

    assert res["structuredContent"]["error"]["code"] == "unauthorized"


def test_a_planner_commits_the_proposal(client, key, db):
    """The other half of "the server proposes, the planner commits" — and the same row an
    orchestrator agent writes is the one the Fleet view's Apply writes."""
    boss = _ok(client, key, "register_agent", {"label": "p", "role_hint": "planner"})
    hand = _ok(client, key, "register_agent", {"label": "w"})

    out = _ok(client, key, "assign_role",
              {"agent_id": boss["agent_id"],            # the caller: a planner
               "target_agent_id": hand["agent_id"],     # who is being re-tasked
               "role": "reviewer", "reason": "queue is deep"})

    assert out["active_role"] == "reviewer"
    # Issued but not collected. The Fleet view renders that distinction so a human can see a
    # reassignment has been made and not yet picked up.
    assert out["pending"] is True
