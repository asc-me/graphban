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
from app.services import tool_tiers as tool_tiers_svc
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

def test_a_worker_key_is_shipped_the_review_tools(client, auth, proj):
    """PRD-39 S3: `reviewer` merged into `worker`, so the worker's credential carries the
    review verbs it used to be refused. `release_item` stays — a worker now holds a review
    claim as well as a build lease, and hands either back with the one verb (GRPH-429)."""
    names = _list(client, _fleet_key(client, auth, proj, "worker"))
    for verb in ("claim_next", "claim_cluster", "claim_review", "sign_off", "bounce",
                 "release_item", "heartbeat"):
        assert verb in names, verb


def test_a_worker_key_is_not_shipped_the_planner_tools(client, auth, proj):
    """The contrast that still exists after PRD-39 S3. A worker is never a planner: it cannot
    mint, re-task or retire, and its manifest does not pretend otherwise."""
    names = _list(client, _fleet_key(client, auth, proj, "worker"))
    for planner_only in ("mint_enrolment", "assign_role", "retire_wave"):
        assert planner_only not in names, planner_only


def test_a_planner_key_carries_allocation_but_not_the_work(client, auth, proj):
    names = _list(client, _fleet_key(client, auth, proj, "planner"))

    assert "assign_role" in names and "propose_allocation" in names
    assert "claim_next" not in names and "sign_off" not in names


def test_every_credential_keeps_the_shared_reads(client, auth, proj):
    """A role gate that dropped `get_context` or `search_items` would leave an agent unable to
    orient. Tools with no role requirement belong to everybody.

    `heartbeat` is in this list now, and it is the one that was learned the hard way: staying
    alive is not a role-specific act. An agent that cannot heartbeat is an agent that leaves
    the roster while still working."""
    for role in fleet.ROLES:
        names = _list(client, _fleet_key(client, auth, proj, role))
        for shared in ("get_context", "search_items", "fleet_status", "get_item_details",
                       "heartbeat"):
            assert shared in names, f"{role} lost {shared}"


def test_an_unrestricted_key_is_narrowed_by_no_ROLE(client, auth):
    """Backwards compatibility for the ROLE gate specifically: every setup predating PRD-17
    has a key eligible for all three roles, and role narrowing must take nothing from it.

    Asserted against the TIER-filtered set rather than against `TOOLS` since GRPH-571. The
    equality with `TOOLS` was this test's whole content and it is no longer the claim — a
    plain key is tiered down to core, which is a different filter doing its job. Written so it
    can still fail: if the role gate started removing something from an all-roles key, the
    result would no longer equal core.
    """
    raw = client.post("/api/api-keys", json={"name": "plain"}, headers=auth).json()["plaintext"]

    assert set(_list(client, raw)) == set(tool_tiers_svc.CORE_TOOLS)
    # And with every tier, it is once again the whole manifest — so the role gate is doing
    # nothing here for any tier setting, which is what "unrestricted" has to mean.
    wide = client.post("/api/api-keys",
                       json={"name": "wide", "tool_tiers": list(tool_tiers_svc.TIERS)},
                       headers=auth).json()["plaintext"]
    assert set(_list(client, wide)) == {t["name"] for t in TOOLS}


def test_the_scope_gate_still_runs_first(client, auth):
    """Role gating narrows what is left after scope, never widens it. A read-only key must not
    acquire a write tool by being role-restricted."""
    raw = client.post("/api/api-keys",
                      json={"name": "ro", "scopes": ["read"],
                            "tool_tiers": list(tool_tiers_svc.TIERS)},
                      headers=auth).json()["plaintext"]

    # Every tier granted, so this isolates the SCOPE gate — which is the claim. Without the
    # tiers it would also pass, and would pass for the wrong reason: tiering removes read
    # tools too, so the set would be smaller for a reason this test is not about.
    assert set(_list(client, raw)) == set(_READ_ONLY)


def test_get_context_reports_the_count_it_was_shipped(client, auth, proj):
    """The number an agent is told must match the manifest it received, or `tool_count`
    becomes a fact about the server rather than about this connection."""
    raw = _fleet_key(client, auth, proj, "worker")

    reported = _call(client, raw, "get_context")["structuredContent"]["tool_count"]

    assert reported == len(_list(client, raw))
    assert reported < len(TOOLS), "a narrowed credential should be shipped less"


# ---- the manifest is not the boundary --------------------------------------------------------

def test_a_tool_absent_from_the_manifest_is_still_refused_by_the_gate(client, auth, proj):
    """THE property that makes this safe to be an optimisation. A manifest can only fail to
    MENTION a tool; the call gate refuses it. If trimming were the only protection, an agent
    that hardcoded a tool name would walk straight through."""
    # A planner: refused `claim_next` and not shipped it. (Was a reviewer, which is a worker
    # since PRD-39 S3 and IS shipped claim_next now.)
    raw = _fleet_key(client, auth, proj, "planner")
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
        roles = ["worker"]
        # Every tier, so this measures the ROLE gate alone. A stub with no `tool_tiers`
        # attribute is what this test had before GRPH-571 and it would now measure both
        # filters at once, reporting a saving the role gate did not make.
        tool_tiers = list(tool_tiers_svc.TIERS)

    narrowed = json.dumps({"tools": _visible_tools(Key())})

    # PRD-39 S3, measured on 2026-09-06: the >15% this asserted was a REVIEWER key's — it
    # dropped the three worker claim tools on top of the planner's. That role is gone, and the
    # two that exist save less: worker 13.1% (it carries the review verbs now), planner 12.7%.
    # §8 of the PRD predicted the merge "may reduce the saving for a narrowed session"; this
    # is that number. The floor is 10% for both, which is a measured fact with a margin, not
    # a threshold picked to pass.
    assert len(narrowed) < len(full) * 0.90, "a worker key should still save >10%"

    class Planner(Key):
        roles = ["planner"]
    planner = json.dumps({"tools": _visible_tools(Planner())})
    assert len(planner) < len(full) * 0.90, "a planner key should still save >10%"
