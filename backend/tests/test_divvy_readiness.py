"""The divvy hands out only what is startable, and says what it withheld (GRPH-783).

**Measured on the live instance, 2026-09-07.** With the review queue empty, `claim_cluster`
returned two clusters back to back to one worker seat, every member `ready: false`: one waited
on a dependency that was not done, two had an explicit routing note saying their next step was
a grill — which a worker seat is forbidden to run. Releasing cluster one handed two of its three
items straight back in cluster two. Each claim reserved thirty-four areas, so the spin also
blocked every other worker in the wave. One of those items had by then been released three
times, and nothing read that.

Two separable faults, both asserted here THROUGH THE TOOL rather than against the service:

1. `clusters_for_project` filtered the pool on `claimable` alone, while `claim_next` also
   required dependency readiness. Two definitions of "takeable", and the divvy had the laxer
   one. Now one pool (`items.claim_pool`) feeds both, and what it declines is returned as
   `withheld` — a partition with nothing in it and three items withheld is not an empty
   backlog.
2. Nothing counted hand-backs. `release_item` now does, and at `RELEASE_HOLD` the item is
   parked out of the pool until a planner delegates it. Role-agnostic on purpose: `until` is
   a planner and delegates from this pool automatically, so exempting planners would have
   re-created the livelock one level up.
"""
import pytest

from app.services import items as items_svc
from tests import attest


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
    """Its own project: the seeded dataset supplies ready work that would let a 'nothing to
    claim' assertion pass by handing out something unrelated."""
    return client.post("/api/projects", json={"name": "DivvyReady"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "divvy", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


def _item(client, key, title, areas, status="next"):
    return _ok(client, key, "create_item",
               {"title": title, "status": status, "touchpoints": areas})["id"]


def _agent(client, key, label, role="worker"):
    return _ok(client, key, "register_agent",
               {"label": label, "role_hint": role, "capabilities": {"instance": label}})["agent_id"]


def _depends(client, key, a, b):
    """`a` depends on `b`."""
    return _ok(client, key, "link_items", {"a": a, "b": b, "type": "dependency"})


def _ids(got):
    return [i["id"] for i in got["items"]]


# Distinct directories throughout: `areas_collide` groups by parent directory (GRPH-810), so two
# paths under one `src/` would cluster together and an assertion about which item was handed
# out would be about the partition rather than the gate.


def test_the_divvy_never_hands_out_an_item_whose_dependency_is_not_done(client, key, db):
    dep = _item(client, key, "ship first", ["alpha/a.py"])
    waiting = _item(client, key, "needs the first", ["beta/b.py"])
    _depends(client, key, waiting, dep)
    w1 = _agent(client, key, "w1")

    got = _ok(client, key, "claim_cluster", {"agent_id": w1})
    assert got["claimed"] and _ids(got) == [dep], got
    assert got["withheld"] == [{"id": waiting, "why": "dependency", "blocked_by": [dep]}]

    # The second seat is REFUSED, and the refusal names the item and what it waits on —
    # "nothing ready to claim" alone is what a spinning worker reads as "done".
    w2 = _agent(client, key, "w2")
    miss = _ok(client, key, "claim_cluster", {"agent_id": w2})
    assert miss["claimed"] is False, miss
    assert waiting in miss["reason"] and dep in miss["reason"], miss["reason"]
    assert miss["withheld"] == [{"id": waiting, "why": "dependency", "blocked_by": [dep]}]

    # Finish the dependency: the same call now hands the item out.
    attest.complete(db, dep)
    got = _ok(client, key, "claim_cluster", {"agent_id": w2})
    assert _ids(got) == [waiting], got
    assert got["withheld"] == []


def test_collision_clusters_reports_what_it_withheld(client, key):
    """`until` picks its seed from this reply, so a blocked item in a cluster would be
    delegated — and an empty partition with no `withheld` would read as a finished wave."""
    dep = _item(client, key, "ship first", ["alpha/a.py"])
    waiting = _item(client, key, "needs the first", ["beta/b.py"])
    _depends(client, key, waiting, dep)

    res = _ok(client, key, "collision_clusters", {})
    offered = {i for c in res["clusters"] for i in c["items"]}
    assert dep in offered and waiting not in offered, res
    assert res["withheld"] == [{"id": waiting, "why": "dependency", "blocked_by": [dep]}]

    # An explicit status is a board view, not a claim pool: it is not gated.
    board = _ok(client, key, "collision_clusters", {"status": "next"})
    assert waiting in {i for c in board["clusters"] for i in c["items"]}
    assert board["withheld"] == []


def test_two_hand_backs_park_an_item_until_a_planner_delegates_it(client, key):
    it = _item(client, key, "grill first, then decompose", ["gamma/g.py"])
    w = _agent(client, key, "w")

    for n in (1, 2):
        got = _ok(client, key, "claim_cluster", {"agent_id": w})
        assert _ids(got) == [it], (n, got)
        rel = _ok(client, key, "release_item",
                  {"id": it, "agent_id": w, "reason": "needs a PRD; a worker seat cannot grill"})
        assert rel["releases"] == n, rel
    # The reason is ON THE ITEM, where the next reader finds it before claiming.
    notes = [e["detail"] for e in rel["evidence"] if e.get("kind") == "note"]
    assert any(d.startswith(f"released by {w}: needs a PRD") for d in notes), notes

    # Third ask: withheld, and the refusal says so and what to do about it.
    miss = _ok(client, key, "claim_cluster", {"agent_id": w})
    assert miss["claimed"] is False, miss
    assert it in miss["reason"] and "2 times" in miss["reason"] and "delegate" in miss["reason"]
    assert miss["withheld"] == [{"id": it, "why": "released", "releases": 2}]
    # `claim_next` reads the SAME pool, so it agrees.
    assert _ok(client, key, "claim_next", {"agent_id": w})["item"] is None

    # A planner delegating it is the touch that resets the count; the divvy offers it again.
    planner = _agent(client, key, "p", role="planner")
    _ok(client, key, "delegate", {"id": it, "lane": "backend", "tier": "cheap",
                                  "agent_id": planner})
    assert "releases" not in _ok(client, key, "get_item_details", {"id": it})
    got = _ok(client, key, "claim_cluster", {"agent_id": w})
    assert _ids(got) == [it], got


def test_one_hand_back_is_not_a_pattern(client, key):
    """An agent that crashed or ran out of budget released once. Holding on one would park
    every item a dying seat touched."""
    it = _item(client, key, "ordinary work", ["delta/d.py"])
    w = _agent(client, key, "w")
    assert _ids(_ok(client, key, "claim_cluster", {"agent_id": w})) == [it]
    _ok(client, key, "release_item", {"id": it, "agent_id": w})
    got = _ok(client, key, "claim_cluster", {"agent_id": w})
    assert _ids(got) == [it] and got["withheld"] == [], got


def test_handing_back_a_review_hold_is_not_a_worker_hand_back(client, key, db):
    """A reviewer returning an item it may not review (its own, say) is not the signal."""
    it = _item(client, key, "built by w", ["epsilon/e.py"])
    w = _agent(client, key, "w")
    assert _ids(_ok(client, key, "claim_cluster", {"agent_id": w})) == [it]
    _ok(client, key, "update_item", {"id": it, "status": "review", "agent_id": w})
    r = _agent(client, key, "r")
    took = _ok(client, key, "claim_review", {"agent_id": r})
    assert took["claimed"] and took["item"]["id"] == it, took
    back = _ok(client, key, "release_item", {"id": it, "agent_id": r})
    assert "releases" not in back, back


def test_the_pool_is_one_function_for_both_claim_paths():
    """`claim_next` and the divvy used to disagree because each had its own predicate. The
    private candidate list is now a projection of the public pool — asserted on the source
    so a later 'optimisation' that re-inlines a second rule is a diff against this line."""
    import inspect

    src = inspect.getsource(items_svc._ready_candidates)
    assert "claim_pool(" in src, src
