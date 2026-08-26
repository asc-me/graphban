"""D2 — role eligibility and the call gate (GRPH-333 / PRD-17).

**Accept:** a worker-role agent calling `update_item(status="done")` gets an `unauthorized`
tool error naming the role required; the item stays `review`. The refusal appears in the
ledger with the agent id and the human principal behind the key — and the principal is
stamped server-side from the key, never from anything the client sends, so a compromised
client still produces a correctly attributed trail.

Enforced at CALL time rather than by trimming the manifest, and that is not a shortcut:
`tools/list` is fetched once at client connect, *before* `register_agent` has run, and this
endpoint returns single JSON with no SSE — so there is no channel to push a changed manifest
when a role is assigned later. A manifest can only fail to mention a tool; the gate refuses
it.
"""
import pytest

from app.models import Agent, Event, Item
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


def _refused(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert res.get("isError") is True, res
    return res["structuredContent"]["error"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def agent_key(client, auth):
    return client.post("/api/api-keys", json={"name": "fleet"},
                       headers=auth).json()["plaintext"]


def _register(client, key, role="worker"):
    return _ok(client, key, "register_agent", {"label": f"{role} term", "role_hint": role})


# ---- the acceptance criterion ------------------------------------------------------------

def test_a_worker_cannot_mark_an_item_done(client, agent_key, db):
    """THE criterion. Without this the self-review ban is decorative: a worker that can write
    `done` never has to hand its work to anybody."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]
    _ok(client, agent_key, "update_item",
        {"id": item_key, "status": "review", "agent_id": me["agent_id"]})

    err = _refused(client, agent_key, "update_item",
                   {"id": item_key, "status": "done", "agent_id": me["agent_id"]})

    assert err["code"] == "unauthorized"
    assert "reviewer" in err["message"] and "worker" in err["message"]
    assert err["hint"], "a refusal must carry the machine-readable next step (AL-47)"
    after = _ok(client, agent_key, "get_item_details", {"id": item_key})
    assert after["status"] == "review", "the item did not move"


def test_a_worker_may_move_work_as_far_as_review(client, agent_key):
    """The ceiling is a ceiling, not a ban on the tool. A gate that refused `update_item`
    outright would stop a worker recording progress at all."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})

    moved = _ok(client, agent_key, "update_item",
                {"id": claimed["item"]["id"], "status": "review", "agent_id": me["agent_id"]})

    assert moved["status"] == "review"


def test_the_refusal_reaches_the_ledger_with_the_agent_and_the_principal(client, agent_key, db):
    """A refusal nobody records is a policy nobody can audit. Both attributions are stamped
    server-side — the agent from the registry, the principal from the key's owner."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})

    _refused(client, agent_key, "update_item",
             {"id": claimed["item"]["id"], "status": "done", "agent_id": me["agent_id"]})

    ev = db.query(Event).filter(Event.action == "role_refused").order_by(Event.id.desc()).first()
    assert ev is not None, "the refusal was not audited"
    assert ev.meta["agent_id"] == me["agent_id"]
    assert ev.meta["tool"] == "update_item"
    assert ev.meta["principal"]["id"], "the human behind the key is named"


def test_the_principal_cannot_be_spoofed_by_the_client(client, agent_key, db):
    """Stamped from the credential, never from the payload. A compromised client still
    produces a correctly attributed trail — which is the only reason the trail is worth
    keeping."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})

    _refused(client, agent_key, "update_item",
             {"id": claimed["item"]["id"], "status": "done", "agent_id": me["agent_id"],
              "principal": {"id": "u_attacker", "label": "someone else"}})

    ev = db.query(Event).filter(Event.action == "role_refused").order_by(Event.id.desc()).first()
    assert ev.meta["principal"]["id"] != "u_attacker"


# ---- the role table ------------------------------------------------------------------------

def test_a_planner_does_not_quietly_do_the_work(client, agent_key):
    """The orchestrator plans; it does not claim. A planner that can `claim_next` becomes
    another worker and the fleet loses the role that was supposed to coordinate it."""
    me = _register(client, agent_key, "planner")

    err = _refused(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})

    assert err["code"] == "unauthorized" and "worker" in err["message"]


def test_a_reviewer_does_not_claim_fresh_work(client, agent_key):
    me = _register(client, agent_key, "reviewer")

    err = _refused(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})

    assert err["code"] == "unauthorized"


# ---- who the gate binds --------------------------------------------------------------------

def test_an_unregistered_caller_on_a_full_key_is_unaffected(client, agent_key):
    """Every setup that predates PRD-17 keeps working. A key eligible for all three roles
    carries no restriction, which is exactly the old behaviour."""
    claimed = _ok(client, agent_key, "claim_next", {})
    assert claimed["claimed"]

    moved = _ok(client, agent_key, "update_item",
                {"id": claimed["item"]["id"], "status": "done"})

    assert moved["status"] == "done"


def test_a_restricted_key_is_bound_even_without_registering(client, auth, db):
    """The bypass that would otherwise exist: skip `register_agent` and the gate has no role
    to check. A key pinned to one role carries that role whether or not anyone registered."""
    made = client.post("/api/api-keys", json={"name": "worker-only"}, headers=auth).json()
    from app.models import ApiKey

    db.get(ApiKey, made["id"]).roles = ["worker"]
    db.commit()
    claimed = _ok(client, made["plaintext"], "claim_next", {})

    err = _refused(client, made["plaintext"], "update_item",
                   {"id": claimed["item"]["id"], "status": "done"})

    assert err["code"] == "unauthorized"


# ---- quarantine ------------------------------------------------------------------------------

def test_three_refusals_quarantine_a_drifting_agent(client, agent_key, db):
    """An agent that keeps calling its old role's tools after a directive is drifting, and one
    holding a cluster while producing nothing is strictly worse than no agent — it blocks the
    divvy for everyone else."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]

    for _ in range(fleet.QUARANTINE_AFTER_REFUSALS):
        _refused(client, agent_key, "update_item",
                 {"id": item_key, "status": "done", "agent_id": me["agent_id"]})

    agent = db.get(Agent, me["agent_id"])
    db.refresh(agent)
    assert agent.state == "quarantined"
    held = db.query(Item).filter(Item.claimed_by == me["agent_id"]).all()
    assert held == [], "its work went back to the queue"


def test_quarantine_does_not_backdate_the_last_contact(client, agent_key, db):
    """`offline` is derived from `last_seen_at`, so "mark it offline" would mean writing a
    time we know to be false — the roster claiming we had not seen an agent that had just
    called us. Quarantine is its own state for exactly that reason."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    before = db.get(Agent, me["agent_id"]).last_seen_at

    for _ in range(fleet.QUARANTINE_AFTER_REFUSALS):
        _refused(client, agent_key, "update_item",
                 {"id": claimed["item"]["id"], "status": "done", "agent_id": me["agent_id"]})

    agent = db.get(Agent, me["agent_id"])
    db.refresh(agent)
    assert agent.last_seen_at >= before, "the one field the server knows is true stays true"
    assert fleet.presence_state(agent) == "quarantined"


def test_a_successful_call_clears_the_refusal_count(client, agent_key, db):
    """CONSECUTIVE is the property. Three refusals spread across a productive hour is one
    stale code path in a client, not an agent that has stopped listening."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]

    for _ in range(fleet.QUARANTINE_AFTER_REFUSALS - 1):
        _refused(client, agent_key, "update_item",
                 {"id": item_key, "status": "done", "agent_id": me["agent_id"]})
    _ok(client, agent_key, "update_item",
        {"id": item_key, "status": "review", "agent_id": me["agent_id"]})
    _refused(client, agent_key, "update_item",
             {"id": item_key, "status": "done", "agent_id": me["agent_id"]})

    agent = db.get(Agent, me["agent_id"])
    db.refresh(agent)
    assert agent.state != "quarantined"


def test_a_quarantined_agent_cannot_heartbeat_its_way_back(client, agent_key, db):
    """The recovery path is to register again — a new row — so the verdict stays attached to
    the process that earned it rather than being withdrawn by the process it was about."""
    me = _register(client, agent_key, "worker")
    fleet.quarantine(db, me["agent_id"])

    fleet.touch(db, me["agent_id"], state="working")

    agent = db.get(Agent, me["agent_id"])
    db.refresh(agent)
    assert agent.state == "quarantined"


def test_quarantine_flags_an_orphaned_branch(client, agent_key, db):
    """The fleet can release the ITEM. It cannot merge or discard somebody's edits, so a
    branch left behind is state only a human can resolve and must be visible as such."""
    me = _register(client, agent_key, "worker")
    agent = db.get(Agent, me["agent_id"])
    agent.branch = "wt-2/feature"
    db.commit()
    _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})

    out = fleet.quarantine(db, me["agent_id"])

    # Asserted on the FACT, not on a column: `branch_orphaned` is derived now (GRPH-396),
    # because as a column it was written only here — so it fired for the drifting agent and
    # never for the dead one, which is the case it exists for.
    assert out["branch_orphaned"] is True
    rows = {a["id"]: a for a in fleet.list_agents(db)}
    assert rows[me["agent_id"]]["branch_orphaned"] is True


# ---- the release_item ceiling ----------------------------------------------------------------

def test_a_worker_cannot_release_to_done_through_release_item(client, agent_key, db):
    """The second door: `release_item` was a sibling of `update_item` with its own key, and
    workers could set `to_status="done"` on it — bypassing update_item's ceiling entirely.
    When `claimed_by` and `built_by` both point to the worker, the item goes `done` with
    `reviewed_by=None`, and every post-review invariant is empty."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]
    _ok(client, agent_key, "update_item",
        {"id": item_key, "status": "review", "agent_id": me["agent_id"]})

    err = _refused(client, agent_key, "release_item",
                   {"id": item_key, "to_status": "done", "agent_id": me["agent_id"]})

    assert err["code"] == "unauthorized"
    assert "reviewer" in err["message"] and "worker" in err["message"]
    assert err["hint"], "a refusal must carry the machine-readable next step (AL-47)"
    after = _ok(client, agent_key, "get_item_details", {"id": item_key})
    assert after["status"] == "review", "the item did not move"


def test_a_worker_can_still_release_to_next(client, agent_key):
    """A gate that refused `release_item` outright would strand every worker holding an item.
    Releasing to `next` (handing work back) is the intended use."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]

    released = _ok(client, agent_key, "release_item",
                   {"id": item_key, "to_status": "next", "agent_id": me["agent_id"]})

    assert released["status"] == "next"


def test_a_worker_can_still_release_to_backlog(client, agent_key):
    """Releasing to `backlog` is also a valid hand-back — same path, different status."""
    me = _register(client, agent_key, "worker")
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]

    released = _ok(client, agent_key, "release_item",
                   {"id": item_key, "to_status": "backlog", "agent_id": me["agent_id"]})

    assert released["status"] == "backlog"
