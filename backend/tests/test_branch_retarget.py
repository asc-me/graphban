"""GRPH-1395. The branch-pointer guard protects a REVIEWER, so it fires only on one.

Twice (GRPH-1360, GRPH-1362) a planner sent `update_item(status=review, branch=…)` for an
item that was `in_progress`/`next` with nobody holding it, and was told the item was "in
review" and to bounce it first. It was not in review — the guard read the status this same
call had just written — and with no holder it could not be bounced.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services import fleet as fleet_svc
from app.services import items as items_svc


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


def _key(client, auth, name):
    return client.post("/api/api-keys", json={"name": name},
                       headers=auth).json()["plaintext"]


def _register(client, key, role, branch="gb/test"):
    return _ok(client, key, "register_agent",
               {"branch": branch, "label": f"{role} term", "role_hint": role})


def _item_on(client, key, planner_id, db, *, branch, status):
    item = _ok(client, key, "create_item", {"title": "finished elsewhere", "status": "next"})
    _ok(client, key, "update_item",
        {"id": item["id"], "branch": branch, "agent_id": planner_id})
    stored = items_svc.get_item(db, item["id"])
    stored.status = status
    stored.claimed_by = None
    db.commit()
    return item["id"]


@pytest.mark.parametrize("status", ["in_progress", "next"])
def test_a_planner_routes_unheld_work_to_review_on_its_real_branch(client, auth, db, status):
    """THE BUG. Sabotage: guard on `item.status` (post-write) again — this call is
    refused as "in review on gb/stale-pointer" for an item that is not in review."""
    pkey = _key(client, auth, "planner")
    planner = _register(client, pkey, "planner")
    item_id = _item_on(client, pkey, planner["agent_id"], db,
                       branch="gb/stale-pointer", status=status)

    out = _ok(client, pkey, "update_item",
              {"id": item_id, "status": "review", "branch": "gb/finished-pr",
               "agent_id": planner["agent_id"]})

    assert out["status"] == "review"
    assert out["branch"] == "gb/finished-pr"

    # And the reviewer that claims it afterwards reads the NEW branch.
    rkey = _key(client, auth, "reviewer")
    reviewer = _register(client, rkey, "reviewer", branch="gb/reviewer")
    claimed = _ok(client, rkey, "claim_review",
                  {"agent_id": reviewer["agent_id"], "wait_seconds": 0})
    assert claimed.get("claimed") is True, claimed
    assert claimed["item"]["id"] == item_id
    assert claimed["branch"] == "gb/finished-pr"


def test_in_review_with_no_live_reviewer_the_pointer_can_move(client, auth, db):
    """No hold, nobody reading: nothing to protect and nothing to bounce."""
    pkey = _key(client, auth, "planner")
    planner = _register(client, pkey, "planner")
    item_id = _item_on(client, pkey, planner["agent_id"], db,
                       branch="gb/stale-pointer", status="review")

    out = _ok(client, pkey, "update_item",
              {"id": item_id, "branch": "gb/finished-pr", "agent_id": planner["agent_id"]})
    assert out["branch"] == "gb/finished-pr"


def test_a_leftover_hold_on_unreviewed_work_does_not_read_as_review(client, auth, db):
    """The status the item HAD, not the one this call writes. Sabotage: guard on
    `item.status` — an `in_progress` item carrying a stale hold is refused as `review`."""
    pkey = _key(client, auth, "planner")
    planner = _register(client, pkey, "planner")
    item_id = _item_on(client, pkey, planner["agent_id"], db,
                       branch="gb/stale-pointer", status="in_progress")
    rkey = _key(client, auth, "reviewer")
    reviewer = _register(client, rkey, "reviewer", branch="gb/reviewer")
    stored = items_svc.get_item(db, item_id)
    stored.review_claimed_by = reviewer["agent_id"]
    stored.review_claimed_at = datetime.now(timezone.utc)
    db.commit()

    out = _ok(client, pkey, "update_item",
              {"id": item_id, "status": "review", "branch": "gb/finished-pr",
               "agent_id": planner["agent_id"]})
    assert out["status"] == "review"
    assert out["branch"] == "gb/finished-pr"


def test_a_held_review_still_refuses_and_says_so(client, auth, db):
    """THE REFUSAL STANDS. Sabotage: drop the `if reviewer:` raise — the reviewer's
    pointer is rewritten under them and this goes red."""
    pkey = _key(client, auth, "planner")
    planner = _register(client, pkey, "planner")
    item_id = _item_on(client, pkey, planner["agent_id"], db,
                       branch="gb/under-review", status="review")
    rkey = _key(client, auth, "reviewer")
    reviewer = _register(client, rkey, "reviewer", branch="gb/reviewer")
    stored = items_svc.get_item(db, item_id)
    stored.review_claimed_by = reviewer["agent_id"]
    stored.review_claimed_at = datetime.now(timezone.utc)
    db.commit()
    assert fleet_svc.review_claim_holder(stored) == reviewer["agent_id"]

    err = _refused(client, pkey, "update_item",
                   {"id": item_id, "branch": "gb/rewritten",
                    "agent_id": planner["agent_id"]})

    assert "`review`" in err["message"]
    assert reviewer["agent_id"] in err["message"]
    assert "gb/under-review" in err["message"]
    db.refresh(stored)
    assert stored.branch == "gb/under-review"


def test_the_message_never_claims_review_for_an_item_that_is_not():
    """The guard reads the PRE-call status (GRPH-1395)."""
    import inspect

    src = inspect.getsource(items_svc.update_item)
    assert 'prev_status == "review"' in src
    assert "review_claim_holder" in src
