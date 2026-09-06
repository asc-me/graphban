"""S2 — attestation keyed on authorship (D-j).

The `gate` scope decides who may WRITE an attestation; the authorship check decides
whether the identified caller may attest THIS item.  Same column (`built_by`) that
`claim_review` (fleet.py:1311) and `sign_off` (fleet.py:1420) already use — the
self-review ban was never held up by the role, it was held up by authorship.

Three tests, written against the failure modes the item names:

1. A `gate`-scoped registered agent attests the item it built → refused, and the
   refusal text contains `built_by` (so the next person can tell an authorship
   refusal from a scope refusal without reading the source).
2. The same agent attests an item built by another → accepted.
3. A `gate`-scoped key with no registered agent attests either → accepted (an
   adapter key carries no agent identity, so the comparison has nothing to
   compare and the write proceeds exactly as today).

Sabotage: delete the new comparison and confirm exactly the first test fails.
"""
import pytest


SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


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


def _err(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    if not res.get("isError"):
        return None
    return (res.get("structuredContent") or {}).get("error") or {}


def _mint(client, auth, scopes, proj):
    return client.post(
        "/api/api-keys",
        json={"name": "gt", "scopes": scopes, "project_id": proj},
        headers=auth,
    ).json()["plaintext"]


def _attest():
    return {"kind": "attestation", "adapter": "ci", "commit": SHA,
            "predicates": [{"name": "suite_green", "passed": True, "detail": "ok"}]}


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "GateS2"},
                       headers=auth).json()["id"]


@pytest.fixture()
def plain_key(client, auth, proj):
    """read+write, no gate — creates items and registers agents."""
    return _mint(client, auth, ["read", "write"], proj)


@pytest.fixture()
def gate_key(client, auth, proj):
    """read+write+gate — may attest, and is the key the agents register on."""
    return _mint(client, auth, ["read", "write", "gate"], proj)


# ---- sabotage: the three refusal classes ------------------------------------------

def test_registered_agent_cannot_attest_own_work(client, plain_key, gate_key):
    """A `gate`-scoped registered agent calls `update_item` with an `attestation`
    receipt on the item it built → refused, and the refusal text contains `built_by`.

    Delete the authorship comparison and THIS test fails — the other two still pass.
    That is the sabotage.
    """
    w = _ok(client, gate_key, "register_agent",
            {"label": "w", "capabilities": {"vendor": "test", "instance": "w"}})
    agent_id = w["agent_id"]

    made = _ok(client, plain_key, "create_item", {"title": "own work"})
    _ok(client, plain_key, "update_item",
        {"id": made["id"], "status": "next"})
    claimed = _ok(client, gate_key, "claim_next", {"agent_id": agent_id})
    item_id = claimed["item"]["id"]
    _ok(client, gate_key, "update_item",
        {"id": item_id, "status": "review", "agent_id": agent_id})

    err = _err(client, gate_key, "update_item",
               {"id": item_id, "evidence": [_attest()], "agent_id": agent_id})

    assert err, "self-attestation was permitted — the authorship check is absent"
    assert "built_by" in err["message"], (
        f"refusal does not name the column: {err['message']!r}")


def test_registered_agent_may_attest_another_clients_work(client, plain_key, gate_key):
    """The same agent attests an item built by another → accepted.  The check is
    keyed on authorship, not on the caller's identity in the abstract.
    """
    builder = _ok(client, gate_key, "register_agent",
                  {"label": "builder", "capabilities": {"vendor": "test", "instance": "b"}})
    attester = _ok(client, gate_key, "register_agent",
                   {"label": "attester", "capabilities": {"vendor": "test", "instance": "a"}})

    made = _ok(client, plain_key, "create_item", {"title": "someone else"})
    _ok(client, plain_key, "update_item",
        {"id": made["id"], "status": "next"})
    claimed = _ok(client, gate_key, "claim_next", {"agent_id": builder["agent_id"]})
    item_id = claimed["item"]["id"]
    _ok(client, gate_key, "update_item",
        {"id": item_id, "status": "review", "agent_id": builder["agent_id"]})

    out = _rpc(client, gate_key, "update_item", {
        "id": item_id, "evidence": [_attest()], "agent_id": attester["agent_id"]})

    assert not out.get("isError"), (
        f"attestation of another agent's work was refused: "
        f"{(out.get('structuredContent') or {}).get('error')}")


def test_unidentified_gate_caller_may_attest_either(client, plain_key, gate_key):
    """A `gate`-scoped key with no registered agent attests → accepted.  An adapter
    key (CI, a sync bridge) carries no agent identity, so the comparison has nothing
    to compare and the write proceeds exactly as today.
    """
    builder = _ok(client, gate_key, "register_agent",
                  {"label": "builder", "capabilities": {"vendor": "test", "instance": "b"}})

    made = _ok(client, plain_key, "create_item", {"title": "adapter attest"})
    _ok(client, plain_key, "update_item",
        {"id": made["id"], "status": "next"})
    claimed = _ok(client, gate_key, "claim_next", {"agent_id": builder["agent_id"]})
    item_id = claimed["item"]["id"]
    _ok(client, gate_key, "update_item",
        {"id": item_id, "status": "review", "agent_id": builder["agent_id"]})

    out = _rpc(client, gate_key, "update_item",
               {"id": item_id, "evidence": [_attest()]})

    assert not out.get("isError"), (
        f"unidentified gate caller was refused: "
        f"{(out.get('structuredContent') or {}).get('error')}")
