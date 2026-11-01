"""GRPH-934 — a refused unidentified caller is given a route to an identity.

Reported from super-arc: a planner session on the fleet's own credential tried to close an
epic after its children merged, and `update_item(status="done")` was refused with "pass
agent_id — the value register_agent returned". That session had never registered; nothing it
did needed a seat, and `get_context` / `gban whoami` have no agent_id to give it. The hint
pointed at a value that did not exist.

The gate itself stays: an anonymous `done` while a fleet runs on the credential is how a
worker wrote `done` on the acceptance walk. What changes is that the refusal names the call
that mints the id, and following it clears the gate.

The role in that call is the one the refused tool needs. The first cut always said
`role_hint='planner'`, which clears `update_item(status="done")` and is the wrong role for
every worker tool on the same gate: following it registers a planner, and `claim_next` is
still unauthorized. The evidence sentence belongs on `update_item` only — the worker tools
have no such fields.
"""
from __future__ import annotations

import pytest


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture()
def key(client, auth):
    proj = client.post("/api/projects", json={"name": "Route"}, headers=auth).json()["id"]
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def epic(client, key):
    # A live worker on the credential is what makes an anonymous caller "unidentified".
    worker = _mcp(client, key, "register_agent", {"label": "w", "role_hint": "worker"})
    assert worker["structuredContent"]["active_role"] == "worker"
    return _mcp(client, key, "create_item", {"title": "epic"})["structuredContent"]["id"]


def test_an_anonymous_done_is_refused_with_a_route_that_exists(client, key, epic):
    res = _mcp(client, key, "update_item", {"id": epic, "status": "done"})
    err = res["structuredContent"]["error"]
    assert err["code"] == "unauthorized", err
    hint = err.get("hint", "")
    assert "register_agent(role_hint='planner')" in hint, hint
    assert "no enrolment code" in hint, hint
    # The old hint, verbatim: a value the caller had no way to get.
    assert hint != "pass agent_id — the value register_agent returned"


def test_following_the_hint_clears_the_gate(client, key, epic):
    """THE CALL. A hint naming a route that does not work is the same dead end, reworded.

    Sabotage: have `register_agent` hand an unhinted-planner request `worker`, and the
    follow-up is refused at the worker ceiling."""
    refused = _mcp(client, key, "update_item", {"id": epic, "status": "done"})
    assert refused["structuredContent"]["error"]["code"] == "unauthorized"

    planner = _mcp(client, key, "register_agent", {"label": "planner", "role_hint": "planner"})
    agent_id = planner["structuredContent"]["agent_id"]
    res = _mcp(client, key, "update_item", {"id": epic, "status": "done", "agent_id": agent_id})
    err = (res.get("structuredContent") or {}).get("error") or {}
    # It may still meet the completion gate (attestation) — that is a different refusal, and
    # the right one. What it must not meet again is the identity gate.
    assert err.get("code") != "unauthorized", err


def test_evidence_needs_no_identity(client, key, epic):
    """The receipt half of the report: appending a PR url is not gated, and the refusal says so."""
    res = _mcp(client, key, "update_item", {
        "id": epic, "evidence": [{"kind": "url", "detail": "PR", "url": "https://x/pull/1"}]})
    assert not res.get("isError"), res
    refused = _mcp(client, key, "update_item", {"id": epic, "status": "done"})
    assert "evidence and other fields write without an identity" in (
        refused["structuredContent"]["error"]["message"])


# Worker tools on the same unidentified gate. A hint hardcoded to planner passes the three
# tests above and still strands `claim_next`. `next_cluster` and `claim_cluster` are the same
# map entry as `claim_next`; leaving them out would let a `claim_next`-only special case pass.
_WORKER_CALLS = [
    ("claim_next", lambda item: {}),
    ("next_cluster", lambda item: {}),
    ("claim_cluster", lambda item: {}),
    ("claim_review", lambda item: {}),
    ("sign_off", lambda item: {"id": item}),
    ("bounce", lambda item: {"id": item, "reason": "not this"}),
    ("release_item", lambda item: {"id": item}),
]


@pytest.mark.parametrize("tool,args_for", _WORKER_CALLS, ids=[name for name, _ in _WORKER_CALLS])
def test_a_worker_tool_names_worker_and_skips_the_evidence_sentence(
        client, key, epic, tool, args_for):
    """Sabotage: `_role_that_clears` always returns planner — every id here fails.
    Sabotage: append the evidence sentence for every tool — the message assertion fails."""
    res = _mcp(client, key, tool, args_for(epic))
    err = res["structuredContent"]["error"]
    assert err["code"] == "unauthorized", err
    hint = err.get("hint", "")
    assert "register_agent(role_hint='worker')" in hint, hint
    assert "role_hint='planner'" not in hint, hint
    assert "no enrolment code" in hint, hint
    assert "evidence and other fields write without an identity" not in err["message"], err


def test_following_the_worker_hint_clears_claim_next(client, key, epic):
    """THE CALL. Naming worker in the hint is not enough if registering that way still
    cannot claim.

    Sabotage: `register_agent` ignores `role_hint='worker'` and stores planner. The
    follow-up is refused with "claim_next requires role 'worker'"."""
    refused = _mcp(client, key, "claim_next", {})
    err = refused["structuredContent"]["error"]
    assert err["code"] == "unauthorized", err
    assert "register_agent(role_hint='worker')" in err.get("hint", ""), err

    worker = _mcp(client, key, "register_agent", {"label": "follows", "role_hint": "worker"})
    assert worker["structuredContent"]["active_role"] == "worker"
    res = _mcp(client, key, "claim_next",
               {"agent_id": worker["structuredContent"]["agent_id"]})
    follow = (res.get("structuredContent") or {}).get("error") or {}
    assert follow.get("code") != "unauthorized", follow


def test_a_branch_write_names_planner(client, key, epic):
    """`branch` is planner work. A worker hint would clear the identity gate and die on
    the ceiling, which is the dead end the other way around."""
    res = _mcp(client, key, "update_item", {"id": epic, "branch": "gb/retarget"})
    err = res["structuredContent"]["error"]
    assert err["code"] == "unauthorized", err
    hint = err.get("hint", "")
    assert "register_agent(role_hint='planner')" in hint, hint
    assert "role_hint='worker'" not in hint, hint
    # update_item does have those fields, so the sentence stays on this refusal.
    assert "evidence and other fields write without an identity" in err["message"]


def test_a_planner_tool_names_planner(client, key, epic):
    """The other direction of the same hint. Hardcoding worker for every unidentified
    refusal would clear `claim_next` and strand `assign_role`."""
    assert epic  # the live worker on this item is what makes the caller unidentified
    res = _mcp(client, key, "assign_role", {"target_agent_id": "nobody", "role": "worker"})
    err = res["structuredContent"]["error"]
    assert err["code"] == "unauthorized", err
    hint = err.get("hint", "")
    assert "register_agent(role_hint='planner')" in hint, hint
    assert "role_hint='worker'" not in hint, hint
    assert "evidence and other fields write without an identity" not in err["message"], err
