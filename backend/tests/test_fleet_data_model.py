"""Agents as first-class rows, and the clock that governs them (GRPH-331 / PRD-17).

Before this, `agent_id` was a self-declared string defaulting to the API key's name — so
three terminals sharing a key were ONE agent to the server. Nothing could assign roles
between them, nothing could stop one signing off its own work, and "who is out there" had no
answer. This is the data model that makes those questions expressible; D1–D3 build on it.

Two properties carry most of the design and both are tested by breaking them:

- **Two registrations on one key are two agents.** De-duplicating by label would merge two
  real terminals into one, which is precisely the bug the table exists to fix.
- **Presence is derived, never stored.** An agent that dies does not announce it, so a stored
  status reads healthy for a process killed an hour ago.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import tagging
from app.models import Agent, ApiKey, AreaReservation, Item, Project
from app.services import fleet
from app.services import keys as keys_svc
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
    db.add(Project(id="fleet", name="Fleet", tag="FL"))
    db.commit()
    return "fleet"


def _agent(db, project_id, label="claude @ macbook", **over):
    stored_id, number = keys_svc.mint(db, project_id, "agent")
    row = Agent(id=stored_id, number=number, project_id=project_id, label=label, **over)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---- identity ---------------------------------------------------------------------------

def test_two_registrations_on_one_key_are_two_agents(db, proj):
    """THE bug this table fixes. Two identical Claude Code windows on one machine is a
    legitimate fleet shape; merging them by label would recreate exactly the condition where
    the server cannot tell them apart."""
    a = _agent(db, proj, label="claude @ macbook")
    b = _agent(db, proj, label="claude @ macbook")

    assert a.id != b.id and a.number != b.number
    assert db.query(Agent).count() == 2


def test_an_agent_renders_a_project_scoped_key(db, proj):
    """`GRPH-A3`, not `AGT-3`. PRD-17's data model writes the latter, but that is the
    pre-tag, product-wide prefix PRD-13 exists to replace — so the convention the same
    sentence cites wins over its literal example."""
    a = _agent(db, proj)

    assert a.key == f"FL-A{a.number}"
    assert tagging.parse(a.key) == ("FL", "agent", a.number)


def test_the_key_follows_a_retag(db, proj):
    """The PRD-13 guarantee, asserted for the new kind: retagging is one UPDATE on one row
    and the agent's stored id never moves."""
    a = _agent(db, proj)
    stored = a.id

    db.get(Project, proj).tag = "FLT"
    db.commit()
    db.refresh(a)

    assert a.id == stored, "the stored id is frozen"
    assert a.key == f"FLT-A{a.number}"


def test_numbers_are_per_project(db, proj):
    """Two projects can both hold an agent 1 — the property that replaced the global
    counter."""
    db.add(Project(id="other", name="Other", tag="OT"))
    db.commit()

    a = _agent(db, proj)
    b = _agent(db, "other")

    assert a.number == b.number == 1
    assert a.id != b.id


# ---- presence is derived --------------------------------------------------------------

def test_presence_lapses_without_a_stored_transition(db, proj):
    """An agent that dies does not say so. A stored `offline` would have to be written by
    something, and nothing is running when the process is gone."""
    a = _agent(db, proj, state="working")
    stale = datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_LEASE_SECONDS)
    a.last_seen_at = stale
    db.commit()

    assert fleet.presence_state(a) == "offline"
    assert a.state == "working", "derived on read — the stored state is untouched"


def test_a_fresh_agent_keeps_its_stored_state(db, proj):
    a = _agent(db, proj, state="reviewing")

    assert fleet.presence_state(a) == "reviewing"


def test_the_presence_ttl_follows_the_lease_clock(db):
    """One clock governs leases, reservations, the bounce pin and presence. A project that
    raises `lease_seconds` for long builds must not start declaring healthy workers dead
    mid-edit — which is exactly what an independent constant would do."""
    assert fleet.presence_ttl_seconds(600) == 150
    assert fleet.presence_ttl_seconds(2400) == 600, "a longer lease widens presence too"
    # Three consecutive misses before death, so one slow round trip never releases an agent.
    assert fleet.heartbeat_interval_seconds(600) * 3 == fleet.presence_ttl_seconds(600)


def test_an_agent_with_a_longer_lease_is_still_alive_at_the_default_ttl(db, proj):
    """The concrete failure the derivation prevents, rather than the arithmetic."""
    a = _agent(db, proj, state="working")
    a.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=200)
    db.commit()

    assert fleet.presence_state(a, lease_seconds=600) == "offline"
    assert fleet.presence_state(a, lease_seconds=2400) == "working"


# ---- the role ceiling ------------------------------------------------------------------

def test_a_key_with_no_roles_means_unspecified_not_none(db):
    """The migration position for every key minted before PRD-17. Reading an empty list as
    "no roles" would silently make every agent on that credential unable to work — an
    absence behaving like a decision."""
    assert fleet.eligible_roles(ApiKey(roles=[])) == fleet.ROLES
    assert fleet.eligible_roles(ApiKey(roles=None)) == fleet.ROLES


def test_a_key_restricts_to_the_roles_it_names(db):
    assert fleet.eligible_roles(ApiKey(roles=["worker"])) == ("worker",)


def test_an_unknown_role_on_a_key_is_ignored(db):
    """A typo'd scope minted a permanently dead key once already (GRPH-219 D4). Here the
    same mistake degrades to the full ceiling rather than to nothing."""
    assert fleet.eligible_roles(ApiKey(roles=["Worker", "admin"])) == fleet.ROLES


# ---- the review handoff ------------------------------------------------------------------

def test_an_item_carries_the_review_handoff_fields(db, proj, client):
    """`reviewed_by` is a column so PRD-17's load-bearing invariant — an agent cannot pass
    its own work — is a comparison against `claimed_by`, not an inference from an event
    log."""
    from app.services import items as items_svc

    it = items_svc.create_item(db, title="A task", project_id=proj)
    it.claimed_by = "FL-A1"
    it.branch = "wt-2/feature"
    it.reviewed_by = "FL-A2"
    it.bounce_pinned_to = "FL-A1"
    it.bounce_pinned_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    db.commit()
    db.refresh(it)

    assert it.reviewed_by != it.claimed_by
    assert it.branch == "wt-2/feature"
    assert it.bounce_pinned_to == "FL-A1"


def test_a_reservation_expires_rather_than_being_released(db, proj, client):
    """The holder may die. A reservation nothing can expire is a file nobody may touch
    again."""
    from app.services import items as items_svc

    it = items_svc.create_item(db, title="A task", project_id=proj)
    a = _agent(db, proj)
    db.add(AreaReservation(agent_id=a.id, item_id=it.id, area="backend/app/services/",
                           expires_at=datetime.now(timezone.utc) + timedelta(seconds=60)))
    db.commit()

    row = db.query(AreaReservation).one()
    assert row.expires_at is not None and row.agent_id == a.id


def test_existing_keys_keep_working(client, auth, db):
    """The migration position, end to end: a key minted through the normal path is eligible
    for every role, so nothing in flight breaks when the ceiling arrives."""
    made = client.post("/api/api-keys", json={"name": "fleet-compat"}, headers=auth).json()
    key = db.get(ApiKey, made["id"])

    assert fleet.eligible_roles(key) == fleet.ROLES
