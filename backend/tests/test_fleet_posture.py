"""An all-in-one credential is a POSTURE, and a role hint cannot narrow it (GRPH-362 / PRD-17).

Found by a human on the D-h acceptance walk, not by a test: an `all-in-one` key connected to
Grok and the roster showed `worker`. The fleet prompt had passed `role_hint="worker"`,
`register_agent` honoured it because the key permits that role, and the agent was gated as a
worker — losing `sign_off` and the ability to write `done`.

`register_agent` already promised this could not happen: *"Registering must never cost an agent
capability it already had — that is what made skipping registration rational."* The clause was
true only in the branch where no hint was given, which is the shape this repo keeps producing —
a claim stated unconditionally and enforced conditionally.

The cause was representational. An all-in-one mint writes `["planner","worker","reviewer"]`,
byte-identical to a key that never set roles, so the credential could not say that all-in-one
was CHOSEN and an unverified string from a client config outranked a posture picked in the UI.

**The test that matters is the capability one, not the label one.** A roster reading
`all-in-one` while the agent still cannot write `done` would be the same defect wearing the
fix's clothes.
"""
import pytest

from app.services import fleet
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
    return client.post("/api/projects", json={"name": "Posture"}, headers=auth).json()["id"]


def _fleet_key(client, auth, proj, role):
    return client.post("/api/fleet/keys",
                       json={"project_id": proj, "role": role, "wave": "w1"},
                       headers=auth).json()["plaintext"]


def _take_an_item_to_done(client, key, agent_id, title):
    """Claim work and try to finish it — the capability a worker does not have."""
    _ok(client, key, "create_item", {"title": title, "status": "next"})
    got = _ok(client, key, "claim_next", {"agent_id": agent_id})
    return _rpc(client, key, "update_item",
                {"id": got["item"]["id"], "agent_id": agent_id, **attest.complete_body()})


# ---- what the mint records -------------------------------------------------------------------

def test_an_all_in_one_mint_records_that_the_posture_was_chosen(client, auth, proj, db):
    """`roles` cannot carry this: all three is also what an unset key resolves to."""
    from app.models import ApiKey

    row_id = client.post("/api/fleet/keys",
                         json={"project_id": proj, "role": "all-in-one", "wave": "w1"},
                         headers=auth).json()["id"]

    key = db.get(ApiKey, row_id)
    assert key.posture == fleet.POSTURE_SINGLE
    assert list(key.roles) == list(fleet.ROLES), "still unrestricted — the ceiling is unchanged"


def test_a_role_narrowed_mint_records_no_posture(client, auth, proj, db):
    from app.models import ApiKey

    row_id = client.post("/api/fleet/keys",
                         json={"project_id": proj, "role": "worker", "wave": "w1"},
                         headers=auth).json()["id"]

    assert db.get(ApiKey, row_id).posture is None


def test_an_ordinary_key_has_no_posture_and_still_honours_a_hint(client, auth, proj):
    """THE regression this must not cause. A fleet sharing one unrestricted credential relies
    on hints to differentiate its members; making every unrestricted key ignore them would
    break the shared-key fleet to fix the single-agent one."""
    raw = client.post("/api/api-keys", json={"name": "shared", "project_id": proj},
                      headers=auth).json()["plaintext"]

    me = _ok(client, raw, "register_agent", {"label": "w", "role_hint": "reviewer"})

    assert me["active_role"] == "reviewer"


# ---- the hint cannot narrow the posture ------------------------------------------------------

def test_a_hint_does_not_narrow_an_all_in_one_credential(client, auth, proj):
    """The reported bug, exactly: an all-in-one key, a worker hint, a roster saying `worker`."""
    raw = _fleet_key(client, auth, proj, "all-in-one")

    me = _ok(client, raw, "register_agent", {"label": "grok", "role_hint": "worker"})

    assert me["active_role"] == fleet.ALL_IN_ONE


def test_the_capability_the_hint_used_to_cost_is_kept(client, auth, proj):
    """The one that decides whether this is fixed or merely relabelled. A worker's ceiling is
    `review`; `done` is the reviewer's word. In the single-agent posture there IS no reviewer
    agent — the human is the reviewer — so an agent silently demoted to worker leaves its work
    parked in `review` with nothing explaining why it stopped."""
    raw = _fleet_key(client, auth, proj, "all-in-one")
    me = _ok(client, raw, "register_agent", {"label": "grok", "role_hint": "worker"})

    res = _take_an_item_to_done(client, raw, me["agent_id"], "finish me")

    assert not res.get("isError"), res
    assert res["structuredContent"]["status"] == "done", "it reached done, not just review"


def test_a_real_worker_credential_is_still_refused_done(client, auth, proj):
    """The gate is not what was wrong, and must not be loosened by this. A key narrowed to
    `worker` still stops at `review` — otherwise the self-review ban is decorative."""
    raw = _fleet_key(client, auth, proj, "worker")
    me = _ok(client, raw, "register_agent", {"label": "w"})

    res = _take_an_item_to_done(client, raw, me["agent_id"], "not yours to finish")

    assert res.get("isError") is True
    assert res["structuredContent"]["error"]["code"] == "unauthorized"


# ---- the same narrowing through the other door -----------------------------------------------

def test_a_planner_cannot_re_task_a_single_agent_credential(client, auth, proj, db):
    """Fixing only `register_agent` would leave the identical demotion available to
    `assign_role` — the agent registers correctly as all-in-one and is narrowed a second
    later, which is harder to see than the original bug rather than easier."""
    from app.security import authz

    raw = _fleet_key(client, auth, proj, "all-in-one")
    me = _ok(client, raw, "register_agent", {"label": "solo"})

    with pytest.raises(authz.Forbidden):
        fleet.assign_role(db, agent_id=me["agent_id"], role="worker")


def test_re_asserting_all_in_one_is_still_allowed(client, auth, proj, db):
    """The refusal is about NARROWING, not about the field being frozen."""
    raw = _fleet_key(client, auth, proj, "all-in-one")
    me = _ok(client, raw, "register_agent", {"label": "solo"})

    out = fleet.assign_role(db, agent_id=me["agent_id"], role=fleet.ALL_IN_ONE)

    assert out.active_role == fleet.ALL_IN_ONE


def test_a_fleet_agent_can_still_be_re_tasked(client, auth, proj, db):
    """D6's whole promise. Re-tasking must keep working for every credential that is not a
    deliberate single-agent one."""
    raw = client.post("/api/api-keys", json={"name": "shared", "project_id": proj},
                      headers=auth).json()["plaintext"]
    me = _ok(client, raw, "register_agent", {"label": "w"})

    out = fleet.assign_role(db, agent_id=me["agent_id"], role="reviewer")

    assert out.active_role == "reviewer"


def test_posture_can_never_widen_a_narrowed_ceiling(client, auth, proj, db):
    """Posture may decline to narrow; it may not promote. `role_for_call` maps an all-in-one
    agent to `*`, so a `single` marker landing on a role-restricted key — by a backfill, a
    hand edit, a future mint path — would convert a label into an escalation. The credential
    stays the ceiling under every combination."""
    from app.models import ApiKey

    row_id = client.post("/api/fleet/keys",
                         json={"project_id": proj, "role": "worker", "wave": "w1"},
                         headers=auth).json()["id"]
    row = db.get(ApiKey, row_id)
    row.posture = fleet.POSTURE_SINGLE          # the corrupt combination
    db.commit()
    raw = _fleet_key(client, auth, proj, "worker")  # a clean one to authenticate with
    del raw

    agent = fleet.register_agent(db, project_id=proj, api_key=row, label="sneaky")

    assert agent.active_role == "worker", "a narrowed key cannot become all-in-one"


# ---- the roster names the credential (GRPH-363) ----------------------------------------------

def test_the_roster_says_which_credential_an_agent_used(client, auth, proj, db):
    """The fix above was reported as not working, and it WAS working — the client was still
    presenting the previous key. Nothing in the roster could say so: the agent, its role and
    its state all read correctly, and the one fact that explained the surprise was the one
    thing not shown. A wrong role is a question about the credential, so the row names it."""
    raw = _fleet_key(client, auth, proj, "all-in-one")
    me = _ok(client, raw, "register_agent", {"label": "grok", "role_hint": "worker"})

    row = next(a for a in fleet.list_agents(db, proj) if a["id"] == me["agent_id"])

    assert row["credential"] and raw.startswith(row["credential"])
    assert row["credential_posture"] == fleet.POSTURE_SINGLE


def test_the_roster_never_emits_more_than_the_display_prefix(client, auth, proj, db):
    """THE property that makes showing it safe. A roster leaking a usable credential would be
    far worse than the confusion it resolves — and this endpoint is read by every agent on the
    project, not only by the human who minted the key."""
    raw = _fleet_key(client, auth, proj, "worker")
    me = _ok(client, raw, "register_agent", {"label": "w"})

    row = next(a for a in fleet.list_agents(db, proj) if a["id"] == me["agent_id"])

    assert raw not in str(row), "the plaintext key reached the roster"
    assert len(row["credential"]) < len(raw), "only the display fragment belongs here"


def test_an_old_credential_is_distinguishable_from_a_new_one_in_the_roster(client, auth, proj, db):
    """The exact confusion this exists to end: two agents, same client, same prompt, different
    keys, different roles. Side by side the reason is visible instead of looking like a gate
    that works intermittently."""
    from app.models import ApiKey

    fresh = _fleet_key(client, auth, proj, "all-in-one")
    stale_id = client.post("/api/fleet/keys",
                           json={"project_id": proj, "role": "all-in-one", "wave": "w1"},
                           headers=auth).json()["id"]
    row = db.get(ApiKey, stale_id)
    row.posture = None                      # a key minted before GRPH-362
    db.commit()

    new_agent = _ok(client, fresh, "register_agent", {"label": "new", "role_hint": "worker"})
    old_agent = fleet.register_agent(db, project_id=proj, api_key=row, label="old",
                                     role_hint="worker")

    roster = {a["id"]: a for a in fleet.list_agents(db, proj)}
    assert roster[new_agent["agent_id"]]["active_role"] == fleet.ALL_IN_ONE
    assert roster[old_agent.id]["active_role"] == "worker"
    assert roster[new_agent["agent_id"]]["credential"] != roster[old_agent.id]["credential"]
