"""GRPH-1282 — sign_off refuses a commit that is not a known head of the item.

On GRPH-1255 the reviewer attested `3a99b03348f8…`; PR #1237's head was `3a99b0330cb6…`.
Eight characters matched and nothing else: the commit existed nowhere, all five predicates
passed, and the item sat `done` and unmerged while its dependants read it as finished. The
merge step caught it; the sign-off should have.

Sabotage: delete the `AttestedAnUnknownCommit` raise in `fleet.sign_off` and
`test_a_sha_sharing_eight_characters_with_the_head_is_refused` signs off and fails.
"""
import pytest

from app.models import Event

BASE = "7ea90ebebe2b88d263dcae8ca77f84b69cb00300"
HEAD = "3a99b0330cb6dbf71042be30c06e871aa6edf5de"
NEAR = "3a99b03348f8b1e1e0ee5d3fd03cdd65cc1bca5a"
assert HEAD[:8] == NEAR[:8] and HEAD != NEAR


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
def key(client, auth):
    proj = client.post("/api/projects", json={"name": "KnownHead"}, headers=auth).json()["id"]
    return client.post("/api/api-keys", json={"name": "head", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _in_review(client, key, *, head=HEAD):
    """An item in review with its base recorded and, when `head` is set, the builder's commit
    on the record as the supervisor's lease writes it (`gbfleet: committed <sha> …`)."""
    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/head-1", "label": "w", "capabilities": {"instance": "w"}})
    _ok(client, key, "create_item", {"title": "a slice", "status": "next", "effort": 1})
    item = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})["item"]["id"]
    notes = [{"kind": "note", "detail": f"gbfleet: branch cut from {BASE} (`gb/head-1`)"}]
    if head:
        notes.append({"kind": "note", "detail":
                      f"gbfleet: committed {head} on `gb/head-1` by {worker['agent_id']}"})
    _ok(client, key, "update_item", {"id": item, "agent_id": worker["agent_id"],
                                     "evidence": notes})
    _ok(client, key, "update_item",
        {"id": item, "status": "review", "agent_id": worker["agent_id"]})
    reviewer = _ok(client, key, "register_agent",
                   {"branch": "gb/head-1", "label": "r", "capabilities": {"instance": "r"}})
    return item, reviewer


def _predicate(client, key, item):
    got = _ok(client, key, "get_item_details", {"id": item})
    signed = [e for e in got["evidence"]
              if e.get("kind") == "attestation" and e.get("adapter") == "fleet.sign_off"]
    return {p["name"]: p for p in signed[-1]["predicates"]}["commit_is_the_head"]


def test_a_sha_sharing_eight_characters_with_the_head_is_refused(client, key, db):
    """THE criterion: GRPH-1255's exact pair. Refused, the head is named, the item stays in
    review, and the refusal is in the ledger."""
    item, reviewer = _in_review(client, key)

    res = _rpc(client, key, "sign_off",
               {"id": item, "agent_id": reviewer["agent_id"], "commit": NEAR})

    assert res.get("isError"), "signed off a commit that exists nowhere"
    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict"
    assert HEAD[:12] in err["message"], "the refusal names the known head"
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"
    rules = [e.meta.get("rule_id") for e in db.query(Event).filter(Event.action == "refusal")]
    assert "review.attest_the_head" in rules


def test_the_exact_head_signs_off_and_the_receipt_says_it_was_compared(client, key):
    item, reviewer = _in_review(client, key)

    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "commit": HEAD})

    assert out["status"] == "done"
    pred = _predicate(client, key, item)
    assert pred["passed"] is True and pred["compared"] is True
    assert HEAD[:12] in pred["detail"]


def test_an_abbreviated_head_is_still_the_head(client, key):
    """`git log --oneline` prints twelve; the comparison is by prefix, as for the base."""
    item, reviewer = _in_review(client, key)

    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "commit": HEAD[:12]})

    assert out["status"] == "done"


def test_a_ci_attested_commit_counts_as_a_known_head(client, key, db):
    """The builder's note is one source; CI's attestation on the item is another."""
    from app.models import Item
    from app.services import keys as keys_svc

    item, reviewer = _in_review(client, key, head=None)
    other = "b" * 40
    row = db.get(Item, keys_svc.resolve_item(db, item))
    row.evidence = list(row.evidence or []) + [{
        "kind": "attestation", "adapter": "github-actions", "commit": other,
        "predicates": [{"name": "suite_green", "passed": True, "detail": "CI passed"}]}]
    db.commit()

    refused = _rpc(client, key, "sign_off",
                   {"id": item, "agent_id": reviewer["agent_id"], "commit": HEAD})
    assert refused.get("isError")
    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "commit": other})
    assert out["status"] == "done"


def test_with_no_known_head_the_receipt_says_it_did_not_compare(client, key):
    """Nothing to compare against is not a pass dressed as one: the detail says NOT compared,
    and no `compared: true` is stamped."""
    item, reviewer = _in_review(client, key, head=None)

    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "commit": NEAR})

    assert out["status"] == "done"
    pred = _predicate(client, key, item)
    assert "NOT compared" in pred["detail"]
    assert pred.get("compared") is not True
