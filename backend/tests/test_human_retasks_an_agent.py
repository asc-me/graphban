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
    _set_role(client, auth, who, "reviewer", reason="needs a second pair of eyes")
    rows = [e for e in db.scalars(select(Event)).all()
            if e.action == "assign_role" and e.target_id == who]
    assert rows, "no audit row for a role change"
    assert rows[-1].actor_type == "user"
    assert rows[-1].meta.get("reason") == "needs a second pair of eyes"


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
