"""GRPH-774 — the human above the fleet can re-task an agent.

`assign_role` existed only as a planner-gated MCP tool, so agents could re-task each other and
the person who owns the credential could not. Measured on Super-Arc: a worker refused
`mint_enrolment` with the project's only planner three days cold, and no way to fix it from the
Fleet view.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Agent, ApiKey
from app.services import fleet as fleet_svc


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Retask"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _worker(client, key, label="worker") -> str:
    return _ok(_mcp(client, key, "register_agent",
                    {"label": label, "role_hint": "worker"}))["agent_id"]


def _set_role(client, auth, agent_id, role, reason="", expect=200):
    r = client.put(f"/api/fleet/agents/{agent_id}/role", headers=auth,
                   json={"role": role, "reason": reason})
    assert r.status_code == expect, r.text
    return r.json()


def test_a_human_can_promote_a_worker_with_no_planner_anywhere(client, auth, key, db, proj):
    """The deadlock, ended. Sabotage: drop the route and a lone worker stays a worker."""
    who = _worker(client, key)
    assert db.get(Agent, who).active_role == "worker"

    out = _set_role(client, auth, who, "planner", reason="only agent on the project")
    assert out["active_role"] == "planner"
    assert out["takes_effect"] == "on the agent's next poll"
    db.expire_all()
    assert db.get(Agent, who).active_role == "planner"


def test_the_credential_ceiling_still_holds(client, auth, db, proj):
    """A human may move an agent WITHIN what its key permits and never past it. Widening a
    ceiling is minting a different credential, and keeping those apart is the point.

    Sabotage: bypass `fleet_svc.assign_role` and write the role directly — this passes.
    """
    # Minted through the FLEET route, which is what actually narrows a credential —
    # `POST /api/api-keys` takes no `roles`, so a key made there is unspecified (all three)
    # and this test would pass without testing anything.
    narrowed = client.post("/api/fleet/keys", headers=auth,
                           json={"project_id": proj, "role": "worker", "wave": "w"}
                           ).json()["plaintext"]
    who = _worker(client, narrowed, label="narrow")
    out = client.put(f"/api/fleet/agents/{who}/role", headers=auth,
                     json={"role": "planner"})
    assert out.status_code == 409, out.text
    assert "planner" in out.text
    db.expire_all()
    assert db.get(Agent, who).active_role == "worker"


def test_an_all_in_one_posture_still_refuses(client, auth, db, proj):
    """A posture is not a ceiling: it says this credential was CHOSEN to be one unrestricted
    agent, and narrowing it to a role would leave it unable to finish its own work."""
    made = client.post("/api/fleet/keys", headers=auth,
                       json={"project_id": proj, "role": "all-in-one", "wave": "w"}).json()
    who = _ok(_mcp(client, made["plaintext"], "register_agent", {"label": "solo"}))["agent_id"]
    out = client.put(f"/api/fleet/agents/{who}/role", headers=auth, json={"role": "worker"})
    assert out.status_code == 409, out.text
    assert "posture" in out.text


def test_an_unknown_role_is_refused_before_anything_moves(client, auth, key, db):
    who = _worker(client, key)
    _set_role(client, auth, who, "overlord", expect=422)
    db.expire_all()
    assert db.get(Agent, who).active_role == "worker"


def test_a_stranger_cannot_retask_somebody_elses_agent(client, auth, key, db, proj):
    """404, not 403: a project you cannot write is one you should not learn the shape of."""
    who = _worker(client, key)
    kate = client.post("/api/auth/login", json={"email": "kate@ascme-labs.com",
                                                "password": "graphban"}).json()["access_token"]
    out = client.put(f"/api/fleet/agents/{who}/role",
                     headers={"Authorization": f"Bearer {kate}"}, json={"role": "planner"})
    assert out.status_code == 404, out.text


def test_re_tasking_is_audited_against_the_human_who_did_it(client, auth, key, db, proj):
    """A role change is an authority act, and the trail has to name a person."""
    from app.models import Event

    who = _worker(client, key)
    # `planner`, not a literal third role: PRD-39 reduced ROLES to (planner, worker) and a
    # test naming a role that no longer exists is how this file turned main red.
    _set_role(client, auth, who, "planner", reason="the only agent on the project")
    rows = [e for e in db.scalars(select(Event)).all()
            if e.action == "assign_role" and e.target_id == who]
    assert rows, "no audit row for a role change"
    assert rows[-1].actor_type == "user"
    assert rows[-1].meta.get("reason") == "the only agent on the project"


# ---- the roster says what an agent was refused for -------------------------------------------

def test_the_roster_shows_what_an_agent_was_last_refused_for(client, auth, key, db, proj):
    """The Super-Arc symptom: the roster said "idle worker" while the agent was being told no
    on every planner-only call, and diagnosing it took a database query.

    Sabotage: record only the count and this fails.
    """
    who = _worker(client, key)
    refused = _mcp(client, key, "mint_enrolment", {"agent_id": who, "role": "worker"})
    assert refused.get("isError"), refused

    roster = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()
    row = next(a for a in roster["agents"] if a["id"] == who)
    assert row["last_refusal"]["tool"] == "mint_enrolment"
    assert "planner" in row["last_refusal"]["reason"]
    assert row["last_refusal"]["count"] == 1


def test_a_successful_call_clears_the_refusal(client, auth, key, db, proj):
    """Consecutive is the property that matters. A stale reason beside a zero count reads as a
    live problem, which is the same defect pointing the other way."""
    who = _worker(client, key)
    assert _mcp(client, key, "mint_enrolment", {"agent_id": who, "role": "worker"}).get("isError")
    db.expire_all()
    assert db.get(Agent, who).last_refusal is not None

    _ok(_mcp(client, key, "heartbeat", {"agent_id": who}))
    db.expire_all()
    assert db.get(Agent, who).last_refusal is None


# ---- GRPH-780: the ceiling was a trap, found by the PRD-40 acceptance walk --------------------

def _fleet_key(client, auth, proj, role, also=(), wave="wave-1") -> dict:
    r = client.post("/api/fleet/keys", headers=auth,
                    json={"project_id": proj, "role": role, "also": list(also), "wave": wave})
    assert r.status_code == 201, r.text
    return r.json()


def _agent_on(client, key, role) -> str:
    return _ok(_mcp(client, key, "register_agent",
                    {"label": f"{role} agent", "role_hint": role}))["agent_id"]


def test_a_one_role_fleet_key_still_refuses_and_that_is_deliberate(client, auth, proj):
    """The bound is not the bug. A key minted for one role is what stops a client config from
    registering a worker as a planner, and it should keep refusing."""
    minted = _fleet_key(client, auth, proj, "worker")
    assert minted["roles"] == ["worker"]
    agent = _agent_on(client, minted["plaintext"], "worker")
    r = client.put(f"/api/fleet/agents/{agent}/role", headers=auth,
                   json={"role": "planner", "reason": "no planner alive"})
    assert r.status_code == 409, r.text


def test_the_refusal_carries_the_remedy_over_rest_and_not_only_over_mcp(client, auth, proj):
    """THE DEFECT the walk hit. `assign_role` has always attached a hint — MCP passes it
    through — and the REST route serialised `str(e)` and dropped it. So the same refusal was
    actionable for an agent and a dead end for the human, on the surface built for the human.

    Sabotage: go back to `str(e)` and this fails."""
    minted = _fleet_key(client, auth, proj, "worker")
    agent = _agent_on(client, minted["plaintext"], "worker")
    r = client.put(f"/api/fleet/agents/{agent}/role", headers=auth, json={"role": "planner"})

    detail = r.json()["detail"]
    assert isinstance(detail, dict), "the hint was dropped on the way out"
    assert "planner" in detail["message"]
    # And the remedy must be TRUE. "Mint a credential for that role" was true and useless:
    # the ceiling belongs to the key the agent already holds, so a new key moves nothing
    # until the agent is running on it.
    assert "already holds" in detail["hint"]
    assert "--role worker --role planner" in detail["hint"]


def test_a_credential_can_be_minted_re_taskable(client, auth, proj):
    """The half a hint cannot fix. Until now every fleet key permitted exactly one role, so
    the route GRPH-774 added for the Super-Arc deadlock refused for every agent a fleet
    actually runs."""
    minted = _fleet_key(client, auth, proj, "worker", also=["planner"])
    assert minted["roles"] == ["worker", "planner"]
    agent = _agent_on(client, minted["plaintext"], "worker")

    r = client.put(f"/api/fleet/agents/{agent}/role", headers=auth,
                   json={"role": "planner", "reason": "the only agent left"})
    assert r.status_code == 200, r.text
    assert r.json()["active_role"] == "planner"


def test_a_re_taskable_key_carries_the_tools_of_every_role_it_permits(client, auth, proj):
    """Otherwise promotion produces a planner that cannot see `propose_allocation` — which
    fails as those tools NOT EXISTING rather than as a missing grant, the same argument the
    single-role case already makes."""
    minted = _fleet_key(client, auth, proj, "worker", also=["planner"])
    assert "prd" in minted["tool_tiers"] and "fleet" in minted["tool_tiers"]
    # …and the gate scope the worker half needs is not lost by widening.
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        row = s.get(ApiKey, minted["id"])
        assert "gate" in (row.scopes or [])
    finally:
        s.close()


def test_the_roster_says_what_each_agent_could_be_moved_to(client, auth, proj):
    """A selector that offers a role the key refuses is a control that always fails — the
    interface form of absence reading clean. Sabotage: drop `credential_roles` and the UI is
    back to discovering the ceiling one click at a time."""
    narrow = _fleet_key(client, auth, proj, "worker")
    wide = _fleet_key(client, auth, proj, "worker", also=["planner"])
    _agent_on(client, narrow["plaintext"], "worker")
    _agent_on(client, wide["plaintext"], "worker")

    rows = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["agents"]
    ceilings = sorted(tuple(a["credential_roles"]) for a in rows)
    assert ceilings == [("worker",), ("worker", "planner")]


def test_all_in_one_cannot_be_combined_with_a_role(client, auth, proj):
    """It is already every role. Storing the pair would leave the posture and the ceiling
    disagreeing, and `is_single_posture` would then refuse the role the caller just added."""
    r = client.post("/api/fleet/keys", headers=auth,
                    json={"project_id": proj, "role": "all-in-one", "also": ["planner"],
                          "wave": "w"})
    assert r.status_code == 422, r.text
    assert "posture" in r.json()["detail"]


def test_widening_a_ceiling_makes_anonymous_calls_stricter_not_looser(client, auth, proj):
    """The consequence worth naming rather than discovering. `role_for_call` falls back to the
    key's ceiling only when it is ONE role — so an agent on a two-role key that does not
    identify itself resolves as `unidentified` and is refused, where a one-role key would have
    let it act as that role.

    It fails CLOSED, which is the right direction, but it means widening a ceiling is not a
    free act: a client that never passes `agent_id` gets stricter, not looser."""
    wide = _fleet_key(client, auth, proj, "worker", also=["planner"])
    agent = _agent_on(client, wide["plaintext"], "worker")

    named = _mcp(client, wide["plaintext"], "mint_enrolment",
                 {"project_id": proj, "agent_id": agent, "role": "worker"})
    anonymous = _mcp(client, wide["plaintext"], "mint_enrolment",
                     {"project_id": proj, "role": "worker"})
    # The named call is refused for a REASON ABOUT ITS ROLE; the anonymous one for not saying
    # who it is. Two different refusals, and conflating them is what sends a person hunting a
    # permissions problem they do not have.
    assert "worker" in str(named)
    assert anonymous.get("isError"), anonymous
    assert "identif" in str(anonymous).lower() or "agent_id" in str(anonymous)
