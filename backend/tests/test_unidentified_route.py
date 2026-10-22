"""GRPH-934 — a refused unidentified caller is given a route to an identity.

Reported from super-arc: a planner session on the fleet's own credential tried to close an
epic after its children merged, and `update_item(status="done")` was refused with "pass
agent_id — the value register_agent returned". That session had never registered; nothing it
did needed a seat, and `get_context` / `gban whoami` have no agent_id to give it. The hint
pointed at a value that did not exist.

The gate itself stays: an anonymous `done` while a fleet runs on the credential is how a
worker wrote `done` on the acceptance walk. What changes is that the refusal names the call
that mints the id, and following it clears the gate.
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
