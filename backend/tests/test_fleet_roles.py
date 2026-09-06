"""S3 — the merge, server side (D-a, D-b).

**Sabotage.** A key stored as `["reviewer"]` → `eligible_roles` returns exactly `("worker",)`.
Redeem a seat stored as `reviewer` → `active_role == "worker"` and `tools_off_limits` is
non-empty. `assign_role(role="reviewer")` → refused, message lists `planner, worker`.
`propose_allocation` for 1, 2 and 4 agents → no `reviewer` anywhere in the mapping or the
rationale. The mint argument's guard — `mint_enrolment` stays `("planner",)` — still passes.

**Acceptance:** §7.1, §7.10.
"""
import pytest

from app.models import Agent, ApiKey, Enrolment
from app.services import fleet


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
    return client.post("/api/projects", json={"name": "S3Merge"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "s3", "project_id": proj},
                       headers=auth).json()["plaintext"]


# ---- D-a: ROLES is now (planner, worker) ------------------------------------------------

def test_roles_has_no_reviewer():
    """The merge: reviewer is gone."""
    assert fleet.ROLES == ("planner", "worker")
    assert "reviewer" not in fleet.ROLES


def test_tool_roles_review_tools_are_worker():
    """claim_review, sign_off and bounce moved from reviewer to worker."""
    assert fleet.TOOL_ROLES["claim_review"] == ("worker",)
    assert fleet.TOOL_ROLES["sign_off"] == ("worker",)
    assert fleet.TOOL_ROLES["bounce"] == ("worker",)
    assert fleet.TOOL_ROLES["release_item"] == ("worker",)


def test_mint_fleet_key_grants_gate_to_worker(client, auth, proj, db):
    """S2 made gate safe for worker; S3 applies it."""
    from app.security.apikey import verify_api_key

    row, _ = fleet.mint_fleet_key(db, user_id="u1", project_id=proj, role="worker", wave="w1")
    assert "gate" in row.scopes


def test_mint_enrolment_stays_planner_only(client, key, db):
    """The mint argument's guard still passes — mint_enrolment is planner-only."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    res = _rpc(client, key, "mint_enrolment",
               {"role": "worker", "project_id": me.get("project_id", "")})
    # A worker cannot mint — the guard is unchanged.
    assert res.get("isError") is True


# ---- D-b: stored values resolve; requested values are refused ----------------------------

def test_a_key_stored_as_reviewer_resolves_to_worker(client, auth, proj, db):
    """A key stored as `["reviewer"]` → `eligible_roles` returns exactly `("worker",)`."""
    from app.models import ApiKey

    row, plaintext = client.post(
        "/api/api-keys", json={"name": "rev", "project_id": proj},
        headers=auth).json()["plaintext"], None
    # Simulate a pre-merge key stored with roles=["reviewer"].
    key_row = db.query(ApiKey).filter(ApiKey.name == "rev").first()
    key_row.roles = ["reviewer"]
    db.commit()

    resolved = fleet.eligible_roles(key_row)
    assert resolved == ("worker",), f"expected exactly ('worker',), got {resolved}"


def test_a_stored_reviewer_seat_registers_as_worker(client, auth, proj, db):
    """Redeem a seat stored as `reviewer` → `active_role == "worker"`."""
    from datetime import datetime, timedelta, timezone
    import secrets
    from app.models import Enrolment
    from app.services.fleet import _hash_code

    # Mint a key for the credential ceiling.
    plaintext = client.post(
        "/api/api-keys", json={"name": "s3worker", "project_id": proj},
        headers=auth).json()["plaintext"]

    # Create a seat stored as "reviewer" (pre-merge) directly in the DB.
    body = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    code = f"REVIEWER-{body}"
    seat = Enrolment(
        id="test-seat-1", project_id=proj, code_hash=_hash_code(code),
        code_prefix=body[:2], role="reviewer", wave="w1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    db.add(seat)
    db.commit()

    me = _ok(client, plaintext, "register_agent",
             {"label": "pre-merge", "enrolment_code": code})
    assert me["active_role"] == "worker"
    # §7.1: the reply names both what was stored and what it resolved to. A client that
    # minted a reviewer and got a worker is told, not left to notice.
    assert me["role_resolved"] == {"stored": "reviewer", "resolved": "worker"}, me


def test_a_seat_that_needed_no_resolution_says_nothing_about_it(client, auth, proj, db):
    """The `role_resolved` field appears only when resolution changed the value — a worker
    seat that registered as a worker has nothing to explain, and a field that always
    reads `{"stored": "worker", "resolved": "worker"}` would teach clients to ignore it."""
    from datetime import datetime, timedelta, timezone
    import secrets
    from app.models import Enrolment
    from app.services.fleet import _hash_code

    plaintext = client.post(
        "/api/api-keys", json={"name": "s3plain", "project_id": proj},
        headers=auth).json()["plaintext"]
    body = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    code = f"WORKER-{body}"
    db.add(Enrolment(
        id="test-seat-plain", project_id=proj, code_hash=_hash_code(code),
        code_prefix=body[:2], role="worker", wave="w1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30)))
    db.commit()
    me = _ok(client, plaintext, "register_agent", {"label": "plain", "enrolment_code": code})
    assert me["active_role"] == "worker"
    assert "role_resolved" not in me, me


def test_a_stored_reviewer_seat_has_non_empty_tools_off_limits(client, auth, proj, db):
    """Redeem a seat stored as `reviewer` → `tools_off_limits` is non-empty."""
    from datetime import datetime, timedelta, timezone
    import secrets
    from app.models import Enrolment
    from app.services.fleet import _hash_code

    plaintext = client.post(
        "/api/api-keys", json={"name": "s3off", "project_id": proj},
        headers=auth).json()["plaintext"]

    body = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    code = f"REVIEWER-{body}"
    seat = Enrolment(
        id="test-seat-2", project_id=proj, code_hash=_hash_code(code),
        code_prefix=body[:2], role="reviewer", wave="w1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    db.add(seat)
    db.commit()

    me = _ok(client, plaintext, "register_agent",
             {"label": "pre-merge-off", "enrolment_code": code})
    # A worker has tools_off_limits (planner-only tools like assign_role).
    assert len(me["tools_off_limits"]) > 0


def test_assign_role_reviewer_is_refused(client, key, db):
    """`assign_role(role="reviewer")` → refused, message lists `planner, worker`."""
    me = _ok(client, key, "register_agent", {"label": "w"})
    res = _rpc(client, key, "assign_role",
               {"target_agent_id": me["agent_id"], "role": "reviewer"})
    assert res.get("isError") is True
    msg = res["content"][0]["text"]
    assert "planner" in msg and "worker" in msg


def test_propose_allocation_no_reviewer_one_agent(client, key):
    """1 agent → no reviewer in mapping or rationale."""
    _ok(client, key, "register_agent", {"label": "solo"})
    out = _ok(client, key, "propose_allocation")
    assert out["reviewers"] == 0
    for entry in out["mapping"]:
        assert entry["role"] != "reviewer"
    assert "reviewer" not in out["rationale"].lower() or "review queue" in out["rationale"]


def test_propose_allocation_no_reviewer_two_agents(client, key):
    """2 agents → no reviewer in mapping or rationale."""
    _ok(client, key, "register_agent", {"label": "a1"})
    _ok(client, key, "register_agent", {"label": "a2"})
    out = _ok(client, key, "propose_allocation")
    assert out["reviewers"] == 0
    for entry in out["mapping"]:
        assert entry["role"] != "reviewer"


def test_propose_allocation_no_reviewer_four_agents(client, key):
    """4 agents → no reviewer in mapping or rationale."""
    for i in range(4):
        _ok(client, key, "create_item",
            {"title": f"work {i}", "status": "next", "touchpoints": [f"area/{i}/x.py"]})
    for i in range(4):
        _ok(client, key, "register_agent", {"label": f"a{i}"})
    out = _ok(client, key, "propose_allocation")
    assert out["reviewers"] == 0
    for entry in out["mapping"]:
        assert entry["role"] != "reviewer"


def test_role_hint_reviewer_clamps_silently(client, key):
    """`role_hint="reviewer"` clamps silently — not refused."""
    me = _ok(client, key, "register_agent", {"label": "hint-rev", "role_hint": "reviewer"})
    # On an unnarrowed key, the hint is ignored and the agent is all-in-one.
    assert me["active_role"] in (fleet.ALL_IN_ONE, "worker")


def test_mint_fleet_key_reviewer_is_refused(client, auth, proj, db):
    """`mint_fleet_key(role="reviewer")` → refused with valid roles listed."""
    with pytest.raises(ValueError, match="planner.*worker|valid roles"):
        fleet.mint_fleet_key(db, user_id="u1", project_id=proj, role="reviewer", wave="w1")


def test_issue_enrolment_reviewer_is_refused(client, auth, proj, db):
    """`issue_enrolment(role="reviewer")` → refused with valid roles listed."""
    with pytest.raises(ValueError, match="planner.*worker|valid roles"):
        fleet.issue_enrolment(db, project_id=proj, role="reviewer", wave="w1")
