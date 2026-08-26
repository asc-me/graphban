"""GRPH-504 — one worker, one item at a time.

**Found by cleaning up after the PRD-24 S7 walk.** A confused local model was holding two
items: the one it was working, and a second it had claimed and never opened. The second was
`in_progress`, invisible to the divvy, and stamped with an author who did nothing.

`claim_next` took the head of the queue and stamped `built_by` every time it was called, and
nothing looked at whether the caller was already holding something. PRD-17 D-g is one worker,
one worktree — two items in one worktree is two lots of work on one branch, which is the shape
the whole cluster model exists to prevent.
"""
from __future__ import annotations

import pytest

from app.services import items as items_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


@pytest.fixture()
def key(client, auth):
    return client.post("/api/api-keys", json={"name": "claims"},
                       headers=auth).json()["plaintext"]


def _two_items(db):
    a = items_svc.create_item(db, title="first", project_id="core", status="next")
    b = items_svc.create_item(db, title="second", project_id="core", status="next")
    return a, b


def test_a_second_claim_is_refused_while_the_first_is_live(db):
    _two_items(db)
    first = items_svc.claim_next(db, "agent-1")
    assert first is not None

    with pytest.raises(items_svc.AlreadyHolding) as exc:
        items_svc.claim_next(db, "agent-1")

    assert first.key in str(exc.value), "name what is already held"
    assert "release" in str(exc.value).lower(), "say how to get out of it"


def test_releasing_then_claiming_again_works(db):
    """The control. A gate that refused the second claim FOREVER would strand every worker
    after its first item.

    It comes back with the SAME item, and that is correct rather than a bug: a released item
    is top-scored again, which is exactly why `claim_next` takes a `skip` list. What is under
    test here is that claiming is permitted at all once the hold is gone.
    """
    _two_items(db)
    first = items_svc.claim_next(db, "agent-1")
    items_svc.release_item(db, first.id, "agent-1")

    second = items_svc.claim_next(db, "agent-1")

    assert second is not None


def test_after_releasing_a_declined_item_the_next_one_is_reachable(db):
    """The other half of that: `skip` is how an agent gets past the item it just handed back,
    and the hold gate must not have broken it."""
    first_item, _ = _two_items(db)
    first = items_svc.claim_next(db, "agent-1")
    items_svc.release_item(db, first.id, "agent-1")

    second = items_svc.claim_next(db, "agent-1", skip=[first.id])

    assert second is not None and second.id != first.id


def test_a_different_agent_is_unaffected(db):
    """One agent's hold is not a lock on the queue."""
    _two_items(db)
    items_svc.claim_next(db, "agent-1")

    other = items_svc.claim_next(db, "agent-2")

    assert other is not None


def test_a_lapsed_lease_is_not_a_hold(db):
    """'Live' means what the reclaim path means by it. An agent whose lease expired is holding
    nothing, and must be able to claim again without an operator intervening — otherwise a
    crashed worker locks itself out permanently."""
    _two_items(db)
    items_svc.claim_next(db, "agent-1")

    assert items_svc.live_claim(db, "agent-1", lease_seconds=0) is None
    assert items_svc.claim_next(db, "agent-1", lease_seconds=0) is not None


def test_a_review_hold_is_not_a_worker_hold(db):
    """`review_claimed_by` is a different column on purpose (GRPH-429): a reviewer holding a
    review claim may still be a worker elsewhere."""
    from app.models import utcnow

    a, _b = _two_items(db)
    # A LIVE worker hold by somebody ELSE, plus a review hold by us. Set up this way on
    # purpose: the first version left `claimed_at` unset, so the lease guard filtered the row
    # out no matter which column was consulted, and a mutation that DID consult
    # `review_claimed_by` survived it. The test proved the wrong thing.
    a.claimed_by, a.claimed_at = "agent-2", utcnow()
    a.review_claimed_by, a.review_claimed_at = "agent-1", utcnow()
    db.commit()

    assert items_svc.live_claim(db, "agent-1") is None, "a review hold is not a worker hold"
    assert items_svc.live_claim(db, "agent-2") is not None, "the worker hold is still seen"


def test_the_mcp_surface_refuses_with_a_hint_rather_than_a_crash(client, key, db):
    """A caller that gets a 500 cannot tell what to do; one that gets `conflict` and a hint
    naming `release_item` can act without reading the source."""
    _two_items(db)
    first = _rpc(client, key, "claim_next", {"agent_id": "agent-9"})
    assert first["structuredContent"]["claimed"] is True

    second = _rpc(client, key, "claim_next", {"agent_id": "agent-9"})

    assert second.get("isError") is True, second
    err = second["structuredContent"]["error"]
    assert err["code"] == "conflict"
    assert "already hold" in err["message"]
    assert "release_item" in err["hint"], "AL-47: a refusal carries the next step"
