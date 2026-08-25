"""An item's evidence only grows, and the sign-off gate survives someone else writing (GRPH-494).

**Found by doing it.** Recording an independent review verdict on GRPH-487 with
`update_item(evidence=[{note}])` silently deleted the author's four receipts — a test summary,
a 7-mutation sabotage receipt, and two design notes. They were recoverable only because the
reviewer happened to still have them in context.

The reason that is worse than a lost note is the whole point of this file: `fleet.sign_off`
gates on `has_effective_sabotage` over the item's STORED evidence, so destroying a builder's
receipts can leave the item unsignable by its own proof — and there is no audit trail, because
`evidence` has no history. Nothing about the item afterwards reads as damaged: it still has
evidence, several entries of it. A populated array says nothing about what used to be in it.

So the assertions here are mostly about the CONSEQUENCE. "Append works" passes trivially and
proves nothing; "a second agent's note cannot cost the first agent their sign-off" is the
property that was broken.
"""
from __future__ import annotations

import pytest

from app.services import items as items_svc


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


SABOTAGE = {
    "kind": "sabotage",
    "claim": "the guard is load-bearing",
    "mutation": "deleted the branch",
    "tests_failed": 3,
}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(db, **kw):
    return items_svc.create_item(db, project_id="core", title="Built it", **kw)


# ---- the consequence ------------------------------------------------------------------


def test_a_reviewers_note_does_not_cost_the_builder_their_sabotage_receipt(db):
    """THE ONE THAT MATTERS. The builder records a sabotage; a second agent adds an unrelated
    note; the adversarial-evidence gate must still see the sabotage.

    Before GRPH-494 the note assigned straight over the array and the receipt was gone, so
    the gate refused an item whose proof had been destroyed by the person reviewing it.
    """
    item = _item(db, effort=4)
    items_svc.update_item(db, item.id, evidence=[SABOTAGE])

    items_svc.update_item(db, item.id, evidence=[
        {"kind": "note", "detail": "INDEPENDENT REVIEW — reads correct to me"},
    ])

    db.refresh(item)
    assert items_svc.has_effective_sabotage(item.evidence), (
        "the reviewer's note destroyed the receipt the sign-off gate reads"
    )
    assert len(item.evidence) == 2


def test_the_builders_receipts_are_still_there_by_content_not_just_by_count(db):
    """A count survives a write that replaced one receipt with another. Assert the receipt."""
    item = _item(db, effort=4)
    items_svc.update_item(db, item.id, evidence=[
        SABOTAGE, {"kind": "test", "detail": "2198 passed"},
    ])

    items_svc.update_item(db, item.id, evidence=[{"kind": "note", "detail": "reviewed"}])

    db.refresh(item)
    details = [e["detail"] for e in item.evidence]
    assert "2198 passed" in details
    assert any(e.get("mutation") == "deleted the branch" for e in item.evidence)


def test_sign_off_still_passes_the_gate_after_a_third_party_write(client, auth):
    """END TO END THROUGH THE REAL TOOLS, because the predicate is not what broke.

    `needs_adversarial_evidence` fires on effort and `sign_off` counts receipts across the
    item's whole stored evidence set — which is precisely why deleting them is expensive.
    The builder records a sabotage, a third agent leaves a note through `update_item`, and
    the reviewer must still be able to sign off.

    Under the old replace semantics the note wiped the sabotage and this call was refused
    with MissingAdversarialEvidence — the item made unsignable by the loss of its own proof.
    """
    proj = client.post("/api/projects", json={"name": "Appending"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "app", "project_id": proj},
                      headers=auth).json()["plaintext"]

    worker = _ok(client, key, "register_agent",
                 {"label": "w", "capabilities": {"instance": "w"}})
    made = _ok(client, key, "create_item",
               {"title": "some work", "status": "next", "effort": 4})
    claimed = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    item_id = claimed["item"]["id"]
    _ok(client, key, "update_item",
        {"id": item_id, "status": "review", "agent_id": worker["agent_id"],
         "evidence": [SABOTAGE]})

    # A third agent leaves an unrelated note — the drive-by that used to be destructive.
    _ok(client, key, "update_item", {"id": item_id, "evidence": [
        {"kind": "note", "detail": "INDEPENDENT REVIEW — reads correct to me"}]})

    reviewer = _ok(client, key, "register_agent",
                   {"label": "r", "role_hint": "reviewer", "capabilities": {"instance": "r"}})
    claimed_review = _ok(client, key, "claim_review", {"agent_id": reviewer["agent_id"]})
    assert claimed_review["item"]["id"] == item_id, "the reviewer claimed a different item"
    res = _rpc(client, key, "sign_off", {"id": item_id, "agent_id": reviewer["agent_id"],
                                         "evidence": [{"kind": "note", "detail": "pass"}]})

    # Named specifically: any other error here is a broken test, not the defect.
    assert "adversarial evidence" not in str(res), (
        f"the drive-by note destroyed the sabotage the gate reads: {res}"
    )
    assert not res.get("isError"), res
    assert res["structuredContent"]["status"] == "done"


# ---- the semantics --------------------------------------------------------------------


def test_an_identical_receipt_sent_twice_is_stored_once(db):
    """`update_item` has no idempotency key, so a call that commits and then times out gets
    retried. Appending blindly would double every receipt on a flaky link."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[SABOTAGE])
    items_svc.update_item(db, item.id, evidence=[SABOTAGE])

    db.refresh(item)
    assert len(item.evidence) == 1


def test_two_different_receipts_of_the_same_kind_both_survive(db):
    """The control for the test above. Dedupe on the normalised dict, not on `kind` — two
    sabotages of different claims are two findings."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[SABOTAGE])
    items_svc.update_item(db, item.id, evidence=[{**SABOTAGE, "claim": "a second claim"}])

    db.refresh(item)
    assert len(item.evidence) == 2


def test_an_empty_list_removes_nothing(db):
    """`evidence: []` is not a delete. It is the one spelling somebody would reach for to
    clear the field, and under append-only it must be a no-op rather than a surprise."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[SABOTAGE])

    items_svc.update_item(db, item.id, evidence=[])

    db.refresh(item)
    assert len(item.evidence) == 1


def test_a_receipt_that_normalises_to_nothing_is_still_dropped(db):
    """Append does not weaken `normalize_evidence`: a receipt with neither detail nor url
    carries no information and never enters the record."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[{"kind": "note", "detail": "", "url": ""}])

    db.refresh(item)
    assert item.evidence == []


# ---- the surfaces ---------------------------------------------------------------------


def test_the_rest_patch_appends_too(client, auth, db):
    """One service layer, so this should be free — which is exactly why it is asserted. The
    REST route and the MCP tool reaching the same function is the invariant, not a hope."""
    item = _item(db, effort=4)
    items_svc.update_item(db, item.id, evidence=[SABOTAGE])

    r = client.patch(f"/api/items/{item.id}",
                     json={"evidence": [{"kind": "note", "detail": "via REST"}]}, headers=auth)

    assert r.status_code == 200, r.text
    kinds = [e["kind"] for e in r.json()["evidence"]]
    assert kinds == ["sabotage", "note"]


def test_the_mcp_tool_says_it_appends(client, auth):
    """The description is the only place an agent learns this. A behaviour change that leaves
    the manifest saying the old thing is how the wrong mental model persists."""
    key = client.post("/api/api-keys", json={"name": "agent"}, headers=auth).json()
    r = client.post("/api/mcp", headers={"X-API-Key": key["plaintext"]},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}

    desc = tools["update_item"]["inputSchema"]["properties"]["evidence"]["description"]
    assert "APPEND" in desc.upper()
