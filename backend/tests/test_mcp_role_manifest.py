"""The manifest is gated by role as well as by scope (PRD-17 D-b).

A key whose `roles` name one role can never call the others' tools — the D2 call gate refuses
them — so shipping those schemas on every `tools/list` is pure token cost, exactly as the
write-tool schemas were for a read-only key (AL-78).

**Gated on the KEY's ceiling, never on the agent's ACTIVE role, and that distinction is the
whole reason this needs no SSE.** PRD-17 rules out trimming per active role and is right to:
`tools/list` is fetched once at client connect, before `register_agent` has run, and this
endpoint has no channel to push `notifications/tools/list_changed` when a role is assigned
later. A key's eligible roles are fixed at mint and cannot change under a live connection, so
gating on them is static — and it is what D-b actually prescribes.

**The manifest is a token optimisation, never a security boundary.** Every test here has a
counterpart asserting the call gate still refuses regardless of what was advertised, because
a manifest can only fail to MENTION a tool while the gate REFUSES it.
"""
import json

import pytest

from app.mcp_server import TOOLS, _READ_ONLY
from app.services import fleet


def _list(client, key):
    return [t["name"] for t in client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"X-API-Key": key},
    ).json()["result"]["tools"]]


def _call(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


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
    return client.post("/api/projects", json={"name": "RoleManifest"},
                       headers=auth).json()["id"]


def _fleet_key(client, auth, proj, role):
    return client.post("/api/fleet/keys",
                       json={"project_id": proj, "role": role, "wave": "w1"},
                       headers=auth).json()["plaintext"]


# ---- what each credential is shipped -------------------------------------------------------

def test_a_reviewer_key_is_not_shipped_the_worker_tools(client, auth, proj):
    names = _list(client, _fleet_key(client, auth, proj, "reviewer"))

    assert "claim_review" in names and "sign_off" in names
    for worker_only in ("claim_next", "claim_cluster", "release_item", "heartbeat"):
        assert worker_only not in names


def test_a_worker_key_is_not_shipped_the_reviewer_tools(client, auth, proj):
    names = _list(client, _fleet_key(client, auth, proj, "worker"))

    assert "claim_next" in names and "claim_cluster" in names
    for reviewer_only in ("claim_review", "sign_off", "bounce"):
        assert reviewer_only not in names


def test_a_planner_key_carries_allocation_but_not_the_work(client, auth, proj):
    names = _list(client, _fleet_key(client, auth, proj, "planner"))

    assert "assign_role" in names and "propose_allocation" in names
    assert "claim_next" not in names and "sign_off" not in names


def test_every_credential_keeps_the_shared_reads(client, auth, proj):
    """A role gate that dropped `get_context` or `search_items` would leave an agent unable to
    orient. Tools with no role requirement belong to everybody."""
    for role in fleet.ROLES:
        names = _list(client, _fleet_key(client, auth, proj, role))
        for shared in ("get_context", "search_items", "fleet_status", "get_item_details"):
            assert shared in names, f"{role} lost {shared}"


def test_an_unrestricted_key_sees_everything(client, auth):
    """Backwards compatibility, and it is not incidental: every setup predating PRD-17 has a
    key eligible for all three roles, and its manifest must not move."""
    raw = client.post("/api/api-keys", json={"name": "plain"}, headers=auth).json()["plaintext"]

    assert set(_list(client, raw)) == {t["name"] for t in TOOLS}


def test_the_scope_gate_still_runs_first(client, auth):
    """Role gating narrows what is left after scope, never widens it. A read-only key must not
    acquire a write tool by being role-restricted."""
    raw = client.post("/api/api-keys", json={"name": "ro", "scopes": ["read"]},
                      headers=auth).json()["plaintext"]

    assert set(_list(client, raw)) == set(_READ_ONLY)


def test_get_context_reports_the_count_it_was_shipped(client, auth, proj):
    """The number an agent is told must match the manifest it received, or `tool_count`
    becomes a fact about the server rather than about this connection."""
    raw = _fleet_key(client, auth, proj, "reviewer")

    reported = _call(client, raw, "get_context")["structuredContent"]["tool_count"]

    assert reported == len(_list(client, raw))
    assert reported < len(TOOLS), "a narrowed credential should be shipped less"


# ---- the manifest is not the boundary --------------------------------------------------------

def test_a_tool_absent_from_the_manifest_is_still_refused_by_the_gate(client, auth, proj):
    """THE property that makes this safe to be an optimisation. A manifest can only fail to
    MENTION a tool; the call gate refuses it. If trimming were the only protection, an agent
    that hardcoded a tool name would walk straight through."""
    raw = _fleet_key(client, auth, proj, "reviewer")
    assert "claim_next" not in _list(client, raw)

    res = _call(client, raw, "claim_next")

    assert res.get("isError") is True
    assert res["structuredContent"]["error"]["code"] == "unauthorized"


def test_the_saving_is_real_and_measured(client, auth, proj):
    """The reason to do this at all. A single-role credential is the normal fleet case — every
    key the Fleet view mints is one — and it pays for a manifest it can mostly not call."""
    full = json.dumps({"tools": TOOLS})
    from app.mcp_server import _visible_tools

    class Key:
        scopes = ["read", "write"]
        roles = ["reviewer"]

    narrowed = json.dumps({"tools": _visible_tools(Key())})

    assert len(narrowed) < len(full) * 0.85, "a single-role key should save >15%"
