"""A bold acceptance label is a clause list, and an unopened one is not a pass (GRPH-1062).

Found while signing off two items whose descriptions put a checklist under `**Acceptance**`.
`parse_acceptance` returned nothing, and `acceptance_coverage` recorded a pass: "description
states no acceptance clauses; nothing to check". The clauses were there. The label was bold,
which is neither a `##` heading nor an `Acceptance:` line, and it was not reported in
`unreadable` either.
"""
import inspect

import pytest

from app.services import fleet

_CHECKLIST = "- [ ] clause one\n- [ ] clause two\n"

#: Distinct from any commit these tests attest, so `commit_is_not_the_base` compares.
_CUT_FROM = "0000111122223333444455556666777788889999"
_PROBE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


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
    return client.post("/api/projects", json={"name": "Bold acceptance"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "bold", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _ready(client, key, description):
    """An item in review, built by one agent, with a recorded base, waiting on another."""
    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/test", "label": "w", "capabilities": {"instance": "w"}})
    _ok(client, key, "create_item",
        {"title": "some work", "status": "next", "effort": 1, "description": description})
    claimed = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    item_id = claimed["item"]["id"]
    _ok(client, key, "update_item",
        {"id": item_id, "agent_id": worker["agent_id"],
         "evidence": [{"kind": "note",
                       "detail": f"gbfleet: branch cut from {_CUT_FROM} (`gb/test`)"}]})
    _ok(client, key, "update_item",
        {"id": item_id, "status": "review", "agent_id": worker["agent_id"]})
    reviewer = _ok(client, key, "register_agent",
                   {"branch": "gb/test", "label": "r", "role_hint": "reviewer",
                    "capabilities": {"instance": "r"}})
    return item_id, reviewer


@pytest.mark.parametrize("label", [
    "**Acceptance**",
    "**Acceptance:**",
    "**Acceptance criteria**",
    "__Acceptance__",
])
def test_a_bold_acceptance_label_returns_both_clauses(label):
    """The repro, and the two spellings planners also write. Checkboxes are markup."""
    clauses, unreadable = fleet.parse_acceptance(f"{label}\n{_CHECKLIST}")
    assert clauses == ["clause one", "clause two"]
    assert unreadable == []


def test_a_bold_label_still_reads_when_a_blank_line_follows():
    clauses, unreadable = fleet.parse_acceptance(
        "**Acceptance**\n\n- [ ] clause one\n- [ ] clause two\n")
    assert clauses == ["clause one", "clause two"]
    assert unreadable == []


@pytest.mark.parametrize("label", ["Acceptance", "Acceptance criteria", "# Acceptance"])
def test_an_unopened_acceptance_label_is_unreadable(label):
    """Acceptance text in no readable shape. Empty clauses must not read as none written."""
    clauses, unreadable = fleet.parse_acceptance(f"{label}\n{_CHECKLIST}")
    assert clauses == []
    assert unreadable
    assert any(label in line for line in unreadable)


def test_a_sentence_that_mentions_acceptance_is_still_no_clauses():
    """The control from GRPH-893. The word in a sentence, then a blank, then a list, is not
    an acceptance section — flagging it would refuse every description that uses the word."""
    clauses, unreadable = fleet.parse_acceptance(
        "## Problem\n\nThe acceptance of this is obvious.\n\n- a list\n")
    assert clauses == []
    assert unreadable == []


def test_the_walk_opens_on_the_bold_label():
    """Sabotage the call. A defined `_ACCEPTANCE_BOLD` that `_walk_acceptance` never matches
    leaves the new clauses unparsed, and a test of the regex alone stays green."""
    src = inspect.getsource(fleet._walk_acceptance)
    assert "_ACCEPTANCE_BOLD.match" in src


def test_bold_clauses_are_what_coverage_counts(client, key):
    """The gate sees two clauses. The pass receipt must not say there were none."""
    item, reviewer = _ready(client, key, f"**Acceptance**\n{_CHECKLIST}")
    out = _ok(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"], "commit": _PROBE_SHA,
        "evidence": [
            {"kind": "test", "detail": "clause one"},
            {"kind": "test", "detail": "clause two"},
        ]})
    assert out["status"] == "done"
    from app.services import items as items_svc
    [att] = [a for a in items_svc.valid_attestations(out["evidence"], commit=_PROBE_SHA)
             if a.get("adapter") == "fleet.sign_off"]
    cov = next(p for p in att["predicates"] if p["name"] == "acceptance_coverage")
    assert cov["passed"] is True
    assert cov["detail"] == (
        "all 2 acceptance clause(s) have a named test from the reviewer "
        "and none whose latest evidence block denies them")
    assert "no acceptance clauses" not in cov["detail"]


def test_an_unopened_label_does_not_record_no_clauses(client, key):
    """`acceptance_coverage` never gets to write the empty pass: the gate refuses first."""
    item, reviewer = _ready(client, key, f"Acceptance\n{_CHECKLIST}")
    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"], "commit": _PROBE_SHA,
        "evidence": [
            {"kind": "test", "detail": "clause one"},
            {"kind": "test", "detail": "clause two"},
        ]})
    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict", err
    assert "cannot read" in err["message"], err["message"]
    assert "no acceptance clauses" not in err["message"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"
