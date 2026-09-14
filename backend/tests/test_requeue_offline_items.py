"""GRPH-850: `fleet_status` must release items held by offline agents.

The promise in `register_agent` is "heartbeat … or you go offline and your items requeue".
Before this fix, `_is_claimable` treated a stale lease as claimable, but the row stayed
`in_progress` / `claimed_by=<dead agent>` until a human intervened. The planner had no
release verb, `choose_resume` skipped `in_progress` unconditionally, and the salvage branch
sat orphaned while a fresh spawn cut from main.

The fix: `fleet_status` calls `requeue_offline_items` before building the roster, so the
release happens on the same tick that detects the offline agent. This test file verifies
that call exists and works.

Sabotage: delete the `requeue_offline_items` call at fleet.py:814. The test
`test_fleet_status_releases_items_held_by_offline_agents` must fail because the item stays
`in_progress` with `claimed_by` set to the dead agent.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Agent, ApiKey, Item, Project, User
from app.services import fleet
from app.services import items as items_svc
from app.services.items import DEFAULT_LEASE_SECONDS


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
    db.add(Project(id="requeue", name="Requeue", tag="RQ"))
    db.commit()
    return "requeue"


@pytest.fixture()
def key(db, proj):
    owner = User(id="u_requeue", name="Requeue Owner", handle="requeue", email="requeue@example.com",
                 initials="RO", password_hash="x")
    db.add(owner)
    db.flush()
    row = ApiKey(id="k_requeue", user_id=owner.id, project_id=proj, name="requeue-key",
                 prefix="gb_sk_ab12", hashed_key="x", scopes=["read", "write"], roles=[])
    db.add(row)
    db.commit()
    return row


def test_fleet_status_releases_items_held_by_offline_agents(db, proj, key):
    """The acceptance criterion: when an agent goes offline (presence expires), items it
    holds return to `next` unclaimed. This is the promise `register_agent` makes.
    
    Sabotage: delete the `requeue_offline_items` call at fleet.py:814. This test must fail
    because the item stays `in_progress` with `claimed_by` set to the dead agent."""
    # Register an agent
    agent = fleet.register_agent(db, project_id=proj, api_key=key, label="worker @ test")
    
    # Create and claim an item
    it = items_svc.create_item(db, title="Test item", project_id=proj, status="next")
    claimed = items_svc.claim_next(db, agent.id, project_id=proj)
    assert claimed is not None, "need a claimed item to test"
    item_id = claimed.id

    # Verify the item is claimed
    db.expire_all()
    item = db.get(Item, item_id)
    assert item.status == "in_progress"
    assert item.claimed_by == agent.id

    # Let the agent's presence expire (simulate offline by backdating last_seen_at)
    agent.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_LEASE_SECONDS + 10)
    db.commit()

    # Call fleet_status — this should trigger requeue_offline_items
    status = fleet.fleet_status(db, project_id=proj)

    # The agent should be offline
    agents = {a["id"]: a for a in status["agents"]}
    assert agents[agent.id]["state"] == "offline"

    # The item should be released back to next
    db.expire_all()
    item = db.get(Item, item_id)
    assert item.status == "next", f"Expected status='next', got '{item.status}'"
    assert item.claimed_by is None, f"Expected claimed_by=None, got '{item.claimed_by}'"


def test_requeue_offline_items_is_idempotent(db, proj, key):
    """Calling fleet_status twice on the same tick should not double-release or error."""
    agent = fleet.register_agent(db, project_id=proj, api_key=key, label="worker @ test")
    it = items_svc.create_item(db, title="Test item", project_id=proj, status="next")
    claimed = items_svc.claim_next(db, agent.id, project_id=proj)
    assert claimed is not None
    item_id = claimed.id

    agent.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_LEASE_SECONDS + 10)
    db.commit()

    # First call releases the item
    fleet.fleet_status(db, project_id=proj)
    db.expire_all()
    item = db.get(Item, item_id)
    assert item.status == "next"
    assert item.claimed_by is None

    # Second call should be a no-op
    fleet.fleet_status(db, project_id=proj)
    db.expire_all()
    item = db.get(Item, item_id)
    assert item.status == "next"
    assert item.claimed_by is None


def test_requeue_offline_items_does_not_release_live_agents(db, proj, key):
    """An agent with a fresh heartbeat keeps its items."""
    agent = fleet.register_agent(db, project_id=proj, api_key=key, label="worker @ test")
    it = items_svc.create_item(db, title="Test item", project_id=proj, status="next")
    claimed = items_svc.claim_next(db, agent.id, project_id=proj)
    assert claimed is not None
    item_id = claimed.id

    # Heartbeat is fresh (just registered)
    status = fleet.fleet_status(db, project_id=proj)

    agents = {a["id"]: a for a in status["agents"]}
    assert agents[agent.id]["state"] != "offline"

    db.expire_all()
    item = db.get(Item, item_id)
    assert item.status == "in_progress"
    assert item.claimed_by == agent.id
