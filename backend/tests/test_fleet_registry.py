"""D1 — the agent registry and presence (GRPH-332 / PRD-17).

The acceptance criterion is the whole test file: *two terminals on the same API key register
as two distinct agents with distinct ids; killing one flips it to `offline` within the TTL and
returns its items to the queue.*

Both halves were impossible before. `agent_id` was a self-declared string defaulting to the
key's name, so two terminals were one agent — and nothing tracked presence at all, so nothing
could notice a death.

**The two clocks are the subtle part, and they are deliberately different.** Presence lapses
at `lease_seconds / 4`; an item becomes reclaimable at the full `lease_seconds`. So an agent
reads `offline` on the roster *before* its work returns to the queue. That gap is the design:
the roster should show a dead agent immediately, while its half-finished item stays reserved a
while longer in case the process is merely wedged. Tests here assert both, at their own clocks,
rather than conflating them into one number that would be wrong for one of the two.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Agent, ApiKey, Item, Project
from app.services import fleet
from app.services import items as items_svc
from app.services.items import DEFAULT_LEASE_SECONDS


def _mcp(client, key, tool, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()
    assert "error" not in r, r
    return r["result"]["structuredContent"]


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


@pytest.fixture()
def key(db, proj):
    """A real credential pinned to the project, so registration has a ceiling to read.

    The owning user is created rather than invented. An earlier version passed
    `user_id="u_1"`, which SQLite accepted happily — it does not enforce foreign keys unless
    asked — and Postgres rejected outright. The green SQLite run was describing a row the
    real database would never hold.
    """
    from app.models import User

    owner = User(id="u_fleet", name="Fleet Owner", handle="fleet", email="fleet@example.com",
                 initials="FO", password_hash="x")
    db.add(owner)
    db.flush()
    row = ApiKey(id="k_fleet", user_id=owner.id, project_id=proj, name="fleet-key",
                 prefix="gb_sk_ab12", hashed_key="x", scopes=["read", "write"], roles=[])
    db.add(row)
    db.commit()
    return row


# ---- registration ------------------------------------------------------------------------

def test_two_registrations_on_one_key_are_two_agents(db, proj, key):
    """THE acceptance criterion, first half. One key, two terminals, two agents — the thing
    the old self-declared `agent_id` could not express."""
    a = fleet.register_agent(db, project_id=proj, api_key=key, label="claude @ macbook")
    b = fleet.register_agent(db, project_id=proj, api_key=key, label="claude @ macbook")

    assert a.id != b.id
    assert {x["id"] for x in fleet.list_agents(db, proj)} == {a.id, b.id}


def test_a_role_hint_is_honoured_when_the_credential_permits_it(db, proj, key):
    a = fleet.register_agent(db, project_id=proj, api_key=key, role_hint="reviewer")

    assert a.active_role == "reviewer"


def test_a_role_hint_beyond_the_ceiling_is_clamped_not_refused(db, proj, key):
    """The hint comes from a client config file. Refusing the registration over a preference
    would strand an agent that could have worked; the authoritative answer is returned in
    `active_role` either way."""
    key.roles = ["worker"]
    db.commit()

    a = fleet.register_agent(db, project_id=proj, api_key=key, role_hint="reviewer")

    assert a.active_role == "worker"


def test_registration_records_the_capabilities_that_drive_review_diversity(db, proj, key):
    """`vendor` is load-bearing later: a Claude reviewer approving Claude work is a different
    agent but not a different error distribution."""
    a = fleet.register_agent(db, project_id=proj, api_key=key,
                             capabilities={"vendor": "anthropic", "model": "opus-5"})

    assert a.capabilities["vendor"] == "anthropic"


# ---- presence ------------------------------------------------------------------------------

def test_an_agent_goes_offline_without_anything_reporting_it(db, proj, key):
    """Second half of acceptance. Nothing runs when a process is killed, so presence has to be
    derived on read — a swept status would need something alive to do the sweeping."""
    a = fleet.register_agent(db, project_id=proj, api_key=key)
    a.state = "working"
    a.last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=fleet.presence_ttl_seconds() + 5)
    db.commit()

    roster = {x["id"]: x for x in fleet.list_agents(db, proj)}

    assert roster[a.id]["state"] == "offline"


def test_a_heartbeat_keeps_an_agent_alive(db, proj, key):
    a = fleet.register_agent(db, project_id=proj, api_key=key)
    a.last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=fleet.presence_ttl_seconds() + 5)
    db.commit()

    fleet.touch(db, a.id, state="working")

    assert fleet.presence_state(a) == "working"


def test_touch_never_creates_an_agent(db, proj, key):
    """Creating on miss would resurrect an id the roster had already aged out, giving one
    process two identities."""
    assert fleet.touch(db, "FL-A99") is None
    assert db.query(Agent).count() == 0


def test_an_agent_cannot_declare_itself_offline(db, proj, key):
    """A contradiction — it is calling us. Storing it would survive the next heartbeat and
    show a working agent as dead."""
    a = fleet.register_agent(db, project_id=proj, api_key=key)

    fleet.touch(db, a.id, state="offline")

    assert a.state != "offline"
    assert fleet.presence_state(a) != "offline"


def test_offline_agents_stay_on_the_roster(db, proj, key):
    """An agent that died holding a branch is exactly what a human needs to see. Hiding it
    would answer 'who is out there' with a tidier lie."""
    a = fleet.register_agent(db, project_id=proj, api_key=key, branch="wt-2/feature")
    a.last_seen_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    roster = fleet.list_agents(db, proj)

    assert [x["id"] for x in roster] == [a.id]
    assert roster[0]["branch"] == "wt-2/feature"


# ---- the two clocks --------------------------------------------------------------------

def test_a_dead_agents_item_returns_to_the_queue(db, proj, key, client):
    """The rest of the acceptance criterion — and it needs NO new mechanism. Past the lease,
    the existing stale-reclaim path in `items._is_claimable` hands the work to whoever asks
    next. Agent death is the lease timeout that already shipped."""
    it = items_svc.create_item(db, title="Some work", project_id=proj, status="next")
    a = fleet.register_agent(db, project_id=proj, api_key=key)
    claimed = items_svc.claim_next(db, a.id, project_id=proj)
    assert claimed is not None and claimed.claimed_by == a.id

    # The process dies: no heartbeat, so both clocks start running.
    a.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_LEASE_SECONDS + 60)
    claimed.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=DEFAULT_LEASE_SECONDS + 60)
    db.commit()

    assert fleet.presence_state(a) == "offline"
    taken = items_svc.claim_next(db, "FL-A2", project_id=proj)
    assert taken is not None and taken.id == it.id, "the work came back"


def test_the_roster_goes_offline_before_the_item_is_reclaimable(db, proj, key, client):
    """The two clocks, asserted as a difference rather than assumed to be one number.

    Presence lapses at lease/4 so a dead agent shows immediately; the item stays reserved
    until the full lease, in case the process is wedged rather than gone. Collapsing them
    would either hide a death for ten minutes or hand a half-finished change to a stranger
    after two.
    """
    it = items_svc.create_item(db, title="Some work", project_id=proj, status="next")
    a = fleet.register_agent(db, project_id=proj, api_key=key)
    items_svc.claim_next(db, a.id, project_id=proj)

    # Past the presence TTL, but well inside the item lease.
    gap = fleet.presence_ttl_seconds() + 10
    assert gap < DEFAULT_LEASE_SECONDS, "the fixture only means something while this holds"
    a.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=gap)
    db.query(Item).filter(Item.id == it.id).update(
        {"claimed_at": datetime.now(timezone.utc) - timedelta(seconds=gap)})
    db.commit()

    assert fleet.presence_state(a) == "offline", "the roster shows the death at once"
    assert items_svc.claim_next(db, "FL-A2", project_id=proj) is None, "the work is still held"


# ---- over MCP ------------------------------------------------------------------------------

def test_register_and_status_over_mcp(client, auth):
    """The transport agents actually use. Asserting on the service alone would pass just as
    well if the tool were never wired."""
    raw = client.post("/api/api-keys", json={"name": "fleet"}, headers=auth).json()["plaintext"]

    first = _mcp(client, raw, "register_agent", {"label": "claude @ macbook"})
    second = _mcp(client, raw, "register_agent", {"label": "claude @ macbook"})
    status = _mcp(client, raw, "fleet_status")

    assert first["agent_id"] != second["agent_id"], "one key, two terminals, two agents"
    assert first["active_role"] in fleet.ROLES
    # The cadence travels with the identity — an agent that must read a constant out of the
    # docs to stay alive is one that eventually does not.
    assert first["heartbeat_interval_seconds"] * 3 == first["presence_ttl_seconds"]
    assert status["total"] == 2 and status["online"] == 2
    assert {a["id"] for a in status["agents"]} == {first["agent_id"], second["agent_id"]}


def test_heartbeat_extends_presence_as_well_as_the_lease(client, auth, db):
    """One call keeps both alive. An agent heartbeating its item but not its presence would be
    declared dead while visibly working, and the roster would report the opposite of what is
    happening."""
    raw = client.post("/api/api-keys", json={"name": "fleet2"}, headers=auth).json()["plaintext"]
    me = _mcp(client, raw, "register_agent", {"label": "worker"})
    claimed = _mcp(client, raw, "claim_next", {"agent_id": me["agent_id"]})
    assert claimed["claimed"], "need a claimed item to heartbeat"

    agent = db.get(Agent, me["agent_id"])
    agent.last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=fleet.presence_ttl_seconds() + 5)
    db.commit()
    assert fleet.presence_state(agent) == "offline"

    _mcp(client, raw, "heartbeat", {"id": claimed["item"]["id"], "agent_id": me["agent_id"]})

    db.refresh(agent)
    assert fleet.presence_state(agent) != "offline"


def test_the_roster_shows_what_an_agent_is_holding(client, auth):
    """The roster's second question after 'who is out there' is 'what is stuck with them'."""
    raw = client.post("/api/api-keys", json={"name": "fleet3"}, headers=auth).json()["plaintext"]
    me = _mcp(client, raw, "register_agent", {"label": "worker"})
    claimed = _mcp(client, raw, "claim_next", {"agent_id": me["agent_id"]})
    assert claimed["claimed"]

    status = _mcp(client, raw, "fleet_status")

    mine = next(a for a in status["agents"] if a["id"] == me["agent_id"])
    assert [h["id"] for h in mine["holdings"]] == [claimed["item"]["id"]]
