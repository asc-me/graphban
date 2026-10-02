"""GRPH-1007, the half its own tests missed — a waiver that arrives over MCP.

**Where this came from.** Signing off GRPH-1003 against its merge commit, the refusal arrived
exactly as designed (`commit_is_not_the_base` could not be checked, no supervisor base on the
item) and the documented escape hatch returned `internal error executing 'sign_off'` twice.

Every test in `test_a_check_that_did_not_run.py` reaches the waiver one of two ways: it calls
`_predicate(..., waived={...})` directly, or it hand-builds an attestation row carrying
`waived`. Sixteen tests, and **not one of them calls `sign_off(waive=...)`** — so the only path
production ever takes was the one path unproven. `sign_off` does
`{... for k, v in (waive or {}).items() ...}`, which is correct for a dict and raises
`AttributeError` for a JSON *string*, and `waive` is declared `{"type": "object"}` with no
coercion between the JSON-RPC envelope and that comprehension.

So there are two facts here, and they need separate tests:

1. A waiver sent as an object is honoured through the whole call, not just inside `_predicate`.
2. A waiver sent as a JSON string is honoured too, rather than crashing. An MCP client that
   serialises an object-typed argument as text is not sending bad input — the schema it was
   given says `object`, and a 500 teaches the caller the hatch does not work rather than that
   it typed the wrong shape.

The control at the bottom is the half that matters: malformed input must still REFUSE, because
a coercion generous enough to accept anything would turn the waiver into a way past the gate
for a caller that named no predicate at all.
"""
import pytest


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
def proj(client, auth):
    return client.post("/api/projects", json={"name": "WireWaiver"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "wire", "project_id": proj},
                       headers=auth).json()["plaintext"]


HEAD = "00540649493720353bbf51b27e0258b56858275f"
WHY = "signed against the squash-merge commit; no supervisor base on this item"


def _awaiting_review(client, key):
    """One item in review with NO recorded base, which is what leaves the predicate
    uncompared. Effort 1 keeps the adversarial gate out of a test about the waiver."""
    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/w", "label": "w", "capabilities": {"instance": "w"}})
    _ok(client, key, "create_item", {"title": "a slice", "status": "next", "effort": 1})
    item = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})["item"]["id"]
    _ok(client, key, "update_item",
        {"id": item, "status": "review", "agent_id": worker["agent_id"]})
    reviewer = _ok(client, key, "register_agent",
                   {"branch": "gb/w", "label": "r", "role_hint": "reviewer",
                    "capabilities": {"instance": "r"}})
    return item, reviewer["agent_id"]


def _waived_names(client, key, item):
    """The predicate names carrying a waiver on the item's stored receipts — read back from
    the ledger rather than from the response, because storage is where GRPH-1007's first
    version dropped the field (`_normalize_predicates` rebuilds each row from named fields)."""
    out = []
    for e in _ok(client, key, "get_item_details", {"id": item})["evidence"]:
        for q in e.get("predicates") or []:
            if str(q.get("waived") or "").strip():
                out.append((q["name"], q["passed"], q["waived"]))
    return out


# ---- the uncompared predicate still blocks ----------------------------------------------------

def test_without_a_waiver_the_sign_off_is_refused(client, key):
    """The control for everything below: this item genuinely cannot compare the predicate, so
    an unwaived sign_off must refuse. Without this, a waiver test passes against a server that
    stopped gating at all."""
    item, reviewer = _awaiting_review(client, key)

    err = _rpc(client, key, "sign_off",
               {"id": item, "agent_id": reviewer, "commit": HEAD})["structuredContent"]["error"]

    assert err["code"] == "conflict"
    assert "commit_is_not_the_base" in err["message"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


# ---- a waiver is honoured through the whole call ----------------------------------------------

def test_a_waiver_sent_as_an_object_is_honoured(client, key):
    """Fact 1. Sixteen tests prove `_predicate` records a waiver; none proved `sign_off` gets
    one as far as `_predicate`, which is the only journey production makes."""
    item, reviewer = _awaiting_review(client, key)

    out = _ok(client, key, "sign_off", {
        "id": item, "agent_id": reviewer, "commit": HEAD,
        "waive": {"commit_is_not_the_base": WHY},
    })

    assert out["status"] == "done"
    stored = _waived_names(client, key, item)
    assert ("commit_is_not_the_base", False, WHY) in stored, (
        "the waiver must survive storage, and must NOT flip the predicate to passed", stored)


def test_a_waiver_sent_as_a_json_string_is_honoured(client, key):
    """Fact 2, and THE criterion — the shape that returned `internal error` twice on the live
    server. `waive` is declared `{"type": "object"}` and nothing coerces the argument, so a
    client that serialises it as text reached `str.items()`."""
    item, reviewer = _awaiting_review(client, key)

    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer, "commit": HEAD,
        "waive": '{"commit_is_not_the_base": "' + WHY + '"}',
    })

    assert not res.get("isError"), res
    assert res["structuredContent"]["status"] == "done"
    assert ("commit_is_not_the_base", False, WHY) in _waived_names(client, key, item)


# ---- and it is still a gate --------------------------------------------------------------------

@pytest.mark.parametrize("waive", ["{}", '{"commit_is_not_the_base": ""}',
                                   '{"commit_is_not_the_base": "   "}'])
def test_a_waiver_naming_no_reason_still_refuses(client, key, waive):
    """A well-formed waiver with nothing in it is not an error, it is a waiver of nothing — so
    the gate refuses with its own message. This is the half that keeps tolerating a string from
    becoming tolerating ANYTHING: a reason is what the override records, and a blank one would
    leave `done` standing on an empty string."""
    item, reviewer = _awaiting_review(client, key)

    err = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer, "commit": HEAD, "waive": waive,
    })["structuredContent"]["error"]

    assert err["code"] == "conflict", (waive, err)
    assert "commit_is_not_the_base" in err["message"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


@pytest.mark.parametrize("waive", [
    "not json at all",
    '["commit_is_not_the_base"]',   # valid JSON, wrong shape — a list carries no reason
    '"commit_is_not_the_base"',     # a bare JSON string, likewise
    7,
    True,
])
def test_a_waiver_the_server_cannot_read_is_a_validation_error(client, key, waive):
    """Malformed input is reported as malformed. Reading it as an empty waiver would answer a
    caller who meant to waive something with the gate's message about a missing base, sending
    them to fix the wrong thing — and a 500 (what every one of these did before the fix) is
    indistinguishable from the hatch not existing."""
    item, reviewer = _awaiting_review(client, key)

    err = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer, "commit": HEAD, "waive": waive,
    })["structuredContent"]["error"]

    assert err["code"] == "validation", (waive, err)
    assert "waive" in err["message"] and err["hint"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_a_waiver_for_a_different_predicate_does_not_admit_this_one(client, key):
    """A reason attached to the wrong name is not a reason for this one. Pinned at `_predicate`
    already; pinned here because the wire is where a lenient coercion would flatten the mapping
    into something that admits every name."""
    item, reviewer = _awaiting_review(client, key)

    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer, "commit": HEAD,
        "waive": '{"acceptance_coverage": "wrong predicate"}',
    })

    assert res["structuredContent"]["error"]["code"] == "conflict"
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_an_absent_waiver_is_not_a_malformed_one(client, key):
    """The default path. `waive` is optional, so omitting it — and passing it as null or as an
    empty string, which is what some clients send for an unset object — must reach the gate's
    own refusal rather than the validation error."""
    for arg in ({}, {"waive": None}, {"waive": ""}):
        item, reviewer = _awaiting_review(client, key)
        err = _rpc(client, key, "sign_off", {
            "id": item, "agent_id": reviewer, "commit": HEAD, **arg,
        })["structuredContent"]["error"]
        assert err["code"] == "conflict", (arg, err)
        assert "commit_is_not_the_base" in err["message"]
