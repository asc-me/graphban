"""GRPH-971 — the sign_off receipt says what reviewed what.

**Accept:** the `reviewer_diversity` predicate names both sides' vendor, model and tier, and
says whether they differ. It passes either way — this records, it does not gate. Two
`undeclared` vendors read as not comparable rather than as one vendor, and an item with no
`built_by` says nothing was compared.

**Why this is not a hole in the self-review ban.** The finding that produced this ticket
(GRPH-970) said `independent_review` "let two children of the same model review each other",
implying `independent()` was wrong. It was not. Both p47-ui children held their own bound seat,
so `reviewer.enrolment_id != author.enrolment_id` and they were two sessions the SERVER
arbitrated — exactly what PRD-19 built. Independence asks "are these separate processes" and
answered correctly.

Diversity is a different property, and nothing asked it. A wave run with one `--adapter`
produces reviewers that are the same model as the builders: each independent, each carrying
little information about the other's work. The receipt named the reviewer's vendor beside the
builder's bare agent id, so "qwen reviewed qwen" required a second lookup to see.

**And same-vendor review is not worthless.** The sharpest review in that wave was a cheap
child bouncing GRPH-955 because its tests did not assert the copy they claimed to — a
judgement no gate would have produced. So this predicate reports and never refuses; a
diversity requirement is a policy decision with a default nobody has chosen yet.
"""
import pytest

from app.services import fleet as fleet_svc


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
    return client.post("/api/projects", json={"name": "Diversity"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "div", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _signed(client, key, *, builder_caps, reviewer_caps):
    """One item built by an agent and signed off by another, and the minted receipt."""
    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/d-1", "label": "w", "capabilities": builder_caps})
    _ok(client, key, "create_item", {"title": "a slice", "status": "next", "effort": 1})
    c = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    item = c["item"]["id"]
    # A base, because these tests are about reviewer DIVERSITY and since GRPH-1007 a sign-off
    # whose `commit_is_not_the_base` could not run is refused — without it every test here
    # would fail on a predicate it is not about.
    _ok(client, key, "update_item",
        {"id": item, "agent_id": worker["agent_id"],
         "evidence": [{"kind": "note",
                       "detail": "gbfleet: branch cut from "
                                 "1111222233334444555566667777888899990000 (`gb/d-1`)"}]})
    _ok(client, key, "update_item",
        {"id": item, "status": "review", "agent_id": worker["agent_id"]})
    reviewer = _ok(client, key, "register_agent",
                   {"branch": "gb/d-1", "label": "r", "role_hint": "reviewer",
                    "capabilities": reviewer_caps})
    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "commit": "a" * 40})
    assert out["status"] == "done"
    got = _ok(client, key, "get_item_details", {"id": item})
    receipts = [e for e in got["evidence"]
                if e.get("kind") == "attestation" and e.get("adapter") == "fleet.sign_off"]
    named = {p["name"]: p for p in receipts[-1]["predicates"]}
    return named["reviewer_diversity"]


# ---- what the receipt says --------------------------------------------------------------------

def test_the_same_vendor_and_model_is_named_as_such(client, key):
    """THE case. Two independent sessions of one cheap model, which is what the wave ran."""
    pred = _signed(
        client, key,
        builder_caps={"instance": "w", "vendor": "qwen-code", "model": "qwen3", "tier": "cheap"},
        reviewer_caps={"instance": "r", "vendor": "qwen-code", "model": "qwen3", "tier": "cheap"},
    )

    assert pred["passed"] is True, "reports, does not refuse"
    assert "SAME vendor" in pred["detail"]
    assert "same model" in pred["detail"]
    # Both sides legible without a second lookup — the whole point of the ticket.
    assert pred["detail"].count("qwen-code:qwen3") == 2
    assert "tier cheap" in pred["detail"]


def test_different_vendors_are_named_as_such(client, key):
    pred = _signed(
        client, key,
        builder_caps={"instance": "w", "vendor": "qwen-code", "model": "qwen3", "tier": "cheap"},
        reviewer_caps={"instance": "r", "vendor": "claude", "model": "opus", "tier": "frontier"},
    )

    assert pred["passed"] is True
    assert "different vendors" in pred["detail"]
    assert "claude:opus" in pred["detail"] and "qwen-code:qwen3" in pred["detail"]


def test_same_vendor_different_model_is_not_the_same_model(client, key):
    """"Same vendor, different model" and "the same model twice" are different observations,
    and a reader deciding whether a review meant anything wants to know which."""
    pred = _signed(
        client, key,
        builder_caps={"instance": "w", "vendor": "claude", "model": "haiku", "tier": "cheap"},
        reviewer_caps={"instance": "r", "vendor": "claude", "model": "opus", "tier": "frontier"},
    )

    assert "SAME vendor" in pred["detail"]
    assert "different model" in pred["detail"]
    assert "same model" not in pred["detail"]


def test_two_undeclared_vendors_are_two_unknowns_not_one_vendor(client, key):
    """The absence-polarity case, and the one this repo gets wrong in both directions.
    `declared_capabilities` substitutes "undeclared" for a missing field, so a naive equality
    check would report two silent agents as the SAME vendor — manufacturing a finding out of
    an absence."""
    pred = _signed(
        client, key,
        builder_caps={"instance": "w"},
        reviewer_caps={"instance": "r"},
    )

    assert pred["passed"] is True
    assert "not comparable" in pred["detail"]
    assert "SAME vendor" not in pred["detail"], "two unknowns are not one vendor"
    assert "reviewer and builder declared no vendor" in pred["detail"]


def test_one_side_undeclared_names_which_side(client, key):
    pred = _signed(
        client, key,
        builder_caps={"instance": "w"},
        reviewer_caps={"instance": "r", "vendor": "claude", "model": "opus", "tier": "frontier"},
    )

    assert "not comparable" in pred["detail"]
    assert "builder declared no vendor" in pred["detail"]
    assert "reviewer and builder" not in pred["detail"], "only the side that was missing"


# ---- the unit, where the polarity lives -------------------------------------------------------

def test_no_built_by_says_nothing_was_compared():
    """An item that reached review before `built_by` existed, or with a human author. The
    receipt must not imply a comparison — the same discipline as `commit_is_not_the_base`
    reporting an unrecorded base."""
    caps = {"vendor": "claude", "model": "opus", "tier": "frontier"}
    differs, detail = fleet_svc.review_diversity(caps, caps, built_by=None)

    assert differs is False
    assert "author unrecorded" in detail
    assert "nothing was compared" in detail
    assert "SAME vendor" not in detail


def test_differs_is_true_only_when_both_are_declared_and_differ():
    """The return value a future policy would gate on, so its polarity is pinned now rather
    than when something depends on it."""
    a = {"vendor": "claude", "model": "opus", "tier": "frontier"}
    b = {"vendor": "qwen-code", "model": "qwen3", "tier": "cheap"}
    none = {"vendor": "undeclared", "model": "undeclared", "tier": "undeclared"}

    assert fleet_svc.review_diversity(a, b, built_by="GRPH-A1")[0] is True
    assert fleet_svc.review_diversity(a, a, built_by="GRPH-A1")[0] is False
    assert fleet_svc.review_diversity(a, none, built_by="GRPH-A1")[0] is False, \
        "an unknown is not a difference"
    assert fleet_svc.review_diversity(none, none, built_by="GRPH-A1")[0] is False
