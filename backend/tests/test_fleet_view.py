"""D5 — the Fleet view's server half (GRPH-336 / PRD-17).

**Accept:** a human with an empty fleet stands up a worker on a second machine using only this
view — no visit to Settings, no hand-edited config. The roster shows it within one heartbeat.
**End wave** revokes exactly the keys this view issued and leaves hand-minted keys untouched.

That last clause is the one worth testing hardest. "End wave" is a destructive button, and a
destructive button whose blast radius is larger than advertised is the kind of thing somebody
presses once and never trusts again.
"""
from datetime import datetime, timezone

import pytest

from app.models import Agent, ApiKey, AreaReservation, Item
from app.services import fleet


def _mcp(client, key, tool, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]
    assert not r.get("isError"), r
    return r["structuredContent"]


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
    return client.post("/api/projects", json={"name": "FleetView"},
                       headers=auth).json()["id"]


# ---- onboarding a wave --------------------------------------------------------------------

def test_a_minted_fleet_key_is_narrowed_to_one_role(client, auth, proj, db):
    """The D2 ceiling, applied at mint time. An agent on this credential cannot register into
    a different role however its client config is written."""
    made = client.post("/api/fleet/keys",
                       json={"project_id": proj, "role": "reviewer", "wave": "w1"},
                       headers=auth)

    assert made.status_code == 201, made.text
    row = db.get(ApiKey, made.json()["id"])
    assert row.roles == ["reviewer"]
    assert row.fleet_wave == "w1"
    assert row.expires_at is not None, "fleet credentials are ephemeral by default"


def test_the_role_ceiling_actually_binds_the_agent(client, auth, proj):
    """Minting with a narrow `roles` list would be decorative if registration ignored it."""
    raw = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "reviewer", "wave": "w1"},
                      headers=auth).json()["plaintext"]

    me = _mcp(client, raw, "register_agent", {"label": "r", "role_hint": "worker"})

    assert me["active_role"] == "reviewer", "the hint cannot climb past the credential"


def test_an_unknown_role_is_refused(client, auth, proj):
    r = client.post("/api/fleet/keys", json={"project_id": proj, "role": "admin"},
                    headers=auth)
    assert r.status_code == 422


def test_a_registered_agent_appears_on_the_roster(client, auth, proj):
    """The acceptance criterion's first half: stand one up and see it, using only this view."""
    raw = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "worker", "wave": "w1"},
                      headers=auth).json()["plaintext"]
    me = _mcp(client, raw, "register_agent", {"label": "opus @ macbook:wt-2"})

    view = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()

    assert view["total"] == 1
    row = view["agents"][0]
    assert row["id"] == me["agent_id"] and row["label"] == "opus @ macbook:wt-2"
    assert row["state"] != "offline"
    # The cadence travels with the roster, so a client never has to read it out of docs.
    assert view["heartbeat_interval_seconds"] * 3 == view["presence_ttl_seconds"]


# ---- end wave ---------------------------------------------------------------------------------

def test_end_wave_leaves_hand_minted_keys_alone(client, auth, proj, db):
    """THE acceptance clause. A destructive button whose blast radius exceeds what it
    advertised is one somebody presses once and never trusts again."""
    mine = client.post("/api/api-keys", json={"name": "my long-lived key", "project_id": proj},
                       headers=auth).json()
    fleet_key = client.post("/api/fleet/keys",
                            json={"project_id": proj, "role": "worker", "wave": "w1"},
                            headers=auth).json()

    client.post("/api/fleet/end-wave", json={"project_id": proj, "wave": "w1"}, headers=auth)

    assert db.get(ApiKey, fleet_key["id"]).revoked is True
    assert db.get(ApiKey, mine["id"]).revoked is False, "a hand-minted key is not this button's"


def test_end_wave_releases_leases_and_reservations(client, auth, proj, db):
    """All of it, at once. A half-ended wave — keys revoked, leases still held — is the
    genuinely confusing state: work no living agent can finish, held by credentials that no
    longer authenticate, with nothing explaining why the queue is stuck."""
    raw = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "worker", "wave": "w1"},
                      headers=auth).json()["plaintext"]
    me = _mcp(client, raw, "register_agent", {"label": "w"})
    _mcp(client, raw, "create_item",
         {"title": "A", "status": "next", "touchpoints": ["backend/app/services"]})
    got = _mcp(client, raw, "claim_cluster", {"agent_id": me["agent_id"]})
    assert got["claimed"]

    out = client.post("/api/fleet/end-wave",
                      json={"project_id": proj, "wave": "w1"}, headers=auth).json()

    assert out["leases_released"] >= 1 and out["reservations_released"] >= 1
    assert db.query(Item).filter(Item.claimed_by == me["agent_id"]).all() == []
    assert db.query(AreaReservation).filter(
        AreaReservation.agent_id == me["agent_id"]).all() == []


def test_an_agent_on_a_revoked_key_reads_offline_at_once(client, auth, proj, db):
    """Derived from the revocation rather than written. "End wave" never backdates
    `last_seen_at` into a time we know to be false — the agent DID just call us; what changed
    is that its credential no longer works."""
    raw = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "worker", "wave": "w1"},
                      headers=auth).json()["plaintext"]
    me = _mcp(client, raw, "register_agent", {"label": "w"})
    seen_before = db.get(Agent, me["agent_id"]).last_seen_at

    client.post("/api/fleet/end-wave", json={"project_id": proj, "wave": "w1"}, headers=auth)

    view = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()
    assert view["agents"][0]["state"] == "offline"
    assert db.get(Agent, me["agent_id"]).last_seen_at == seen_before, "no falsified timestamp"


def test_the_confirm_can_name_the_damage_before_acting(client, auth, proj):
    """A confirm reading "are you sure?" teaches people to click through it. One reading
    "revoke 1 key, release 1 lease?" is a decision."""
    raw = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "worker", "wave": "w1"},
                      headers=auth).json()["plaintext"]
    me = _mcp(client, raw, "register_agent", {"label": "w"})
    _mcp(client, raw, "create_item", {"title": "A", "status": "next"})
    _mcp(client, raw, "claim_next", {"agent_id": me["agent_id"]})

    preview = client.get(f"/api/fleet/end-wave?project_id={proj}&wave=w1", headers=auth).json()

    # Exact equality on purpose: a confirm that silently gains a field is a confirm naming
    # damage nobody reviewed. `seats` arrived with PRD-19 — this wave is a legacy key-based
    # one, so it owns none.
    assert preview == {"keys": 1, "seats": 0, "agents": 1, "leases": 1, "reservations": 0}


def test_ending_one_wave_does_not_end_another(client, auth, proj, db):
    a = client.post("/api/fleet/keys", json={"project_id": proj, "role": "worker", "wave": "w1"},
                    headers=auth).json()
    b = client.post("/api/fleet/keys", json={"project_id": proj, "role": "worker", "wave": "w2"},
                    headers=auth).json()

    client.post("/api/fleet/end-wave", json={"project_id": proj, "wave": "w1"}, headers=auth)

    assert db.get(ApiKey, a["id"]).revoked is True
    assert db.get(ApiKey, b["id"]).revoked is False


# ---- the review queue renders the ban ---------------------------------------------------------

def test_the_review_queue_says_who_built_each_item(client, auth, proj):
    """The ban rendered as a negative ON THE ITEM — "AGT-4 built it" — rather than a list of
    who is eligible. The refusal belongs to the item, and stating it that way is what makes
    the invariant legible at a glance instead of something a reader reconstructs."""
    raw = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "worker", "wave": "w1"},
                      headers=auth).json()["plaintext"]
    me = _mcp(client, raw, "register_agent", {"label": "opus @ macbook"})
    _mcp(client, raw, "create_item", {"title": "A", "status": "next"})
    c = _mcp(client, raw, "claim_next", {"agent_id": me["agent_id"]})
    _mcp(client, raw, "update_item",
         {"id": c["item"]["id"], "status": "review", "agent_id": me["agent_id"]})

    view = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()

    row = view["review_queue"][0]
    assert row["built_by"] == me["agent_id"]
    assert row["built_by_label"] == "opus @ macbook", "a human reads the label, not the id"


# ---- the cluster board explains itself ---------------------------------------------------------

def test_a_held_back_cluster_says_what_it_is_waiting_for(client, auth, proj):
    """Without the reason a queued cluster looks like the fleet being stuck, and a human
    overrides the divvy. With it, they trust it."""
    raw = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "worker", "wave": "w1"},
                      headers=auth).json()["plaintext"]
    me = _mcp(client, raw, "register_agent", {"label": "w"})
    _mcp(client, raw, "create_item",
         {"title": "A", "status": "next", "touchpoints": ["backend/app/models"]})
    _mcp(client, raw, "create_item",
         {"title": "B", "status": "next", "touchpoints": ["backend/app/models"]})
    _mcp(client, raw, "claim_cluster", {"agent_id": me["agent_id"], "max_items": 1})

    view = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()

    held = [c for c in view["clusters"] if c["held_by"]]
    assert held, "the remaining cluster should be visibly blocked"
    assert held[0]["held_by"] == me["agent_id"]
    assert "backend/app/models" in held[0]["blocked_on"]


def test_the_view_needs_a_session(client, proj):
    """The caller is a human deciding how to spend a fleet, not an agent working inside one."""
    assert client.get(f"/api/fleet?project_id={proj}").status_code == 401
    assert client.post("/api/fleet/end-wave", json={"project_id": proj}).status_code == 401


def test_an_all_in_one_credential_is_unnarrowed(client, auth, proj, db):
    """The roster REPORTS `all-in-one`, so the Fleet view must be able to mint one — a page
    that counts a posture it cannot create names a category the reader has no way to produce.

    It mints an UNNARROWED credential (all three roles), which is exactly what makes an agent
    registering on it unrestricted.
    """
    made = client.post("/api/fleet/keys",
                       json={"project_id": proj, "role": "all-in-one", "wave": "w1"},
                       headers=auth)

    assert made.status_code == 201, made.text
    row = db.get(ApiKey, made.json()["id"])
    assert set(row.roles) == set(fleet.ROLES), "unnarrowed — no ceiling"
    assert row.fleet_wave == "w1", "still swept by End wave; the posture differs, not the lifecycle"


def test_an_agent_on_that_credential_registers_all_in_one(client, auth, proj):
    """End to end: the option produces the posture it names."""
    raw = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "all-in-one", "wave": "w1"},
                      headers=auth).json()["plaintext"]

    me = _mcp(client, raw, "register_agent", {"label": "solo"})

    assert me["active_role"] == fleet.ALL_IN_ONE
    view = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()
    assert view["by_role"]["all-in-one"] == 1
    assert view["posture"] == "single-agent"


def test_an_all_in_one_credential_is_still_ended_by_end_wave(client, auth, proj, db):
    """It is a fleet-issued key like any other. Exempting it would leave a live write-scoped
    credential behind that the button appeared to have cleaned up."""
    made = client.post("/api/fleet/keys",
                       json={"project_id": proj, "role": "all-in-one", "wave": "w1"},
                       headers=auth).json()

    client.post("/api/fleet/end-wave", json={"project_id": proj, "wave": "w1"}, headers=auth)

    assert db.get(ApiKey, made["id"]).revoked is True
