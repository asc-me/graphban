"""A `url` receipt keeps the commit it names, through the real normalizer (GRPH-846).

**Bounced once for exactly this.** The fleet's merge receipt was a `url` row carrying
`commit=<squash SHA>` so `gbfleet.deps._commits` would read the merge commit as one of the
item's commits — and the fleet's hold-lift walk passed because its FAKE ledger extended the
raw payload, while the real `normalize_evidence` stored a url as `{kind, detail, url}` and
dropped the field. After a squash the reviewed commit is not an ancestor of the trunk, the
merge commit was never on the item, and the GRPH-798 hold kept every dependant forever with
a green suite on both sides. So the pin lives HERE, on the server's normalizer and on the
tool that reaches it, and the fleet's fakes now drop what this drops rather than keep what it
keeps.

The row below is the one `gbfleet.supervisor.Merger._record` writes, field for field.
"""
from __future__ import annotations

from app.services import items as items_svc

MERGE_OID = "9c1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e"

# What the supervisor sends. Keep in step with `Merger._record` in fleet/src/gbfleet/supervisor.py.
FLEET_MERGE_RECEIPT = {
    "kind": "url",
    "detail": f"merged by gbfleet: squash of #9 landed as {MERGE_OID[:12]}",
    "url": "https://github.com/asc-me/graphban/pull/9",
    "commit": MERGE_OID,
}


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def test_the_fleet_merge_receipt_keeps_its_commit_through_normalize_evidence():
    [row] = items_svc.normalize_evidence([dict(FLEET_MERGE_RECEIPT)])
    assert row["kind"] == "url"
    assert row["commit"] == MERGE_OID, row
    assert row["url"] == FLEET_MERGE_RECEIPT["url"]


def test_a_url_without_a_commit_stores_none():
    """The field is kept, never invented: a plain PR link stays `{kind, detail, url}`, so
    `deps._commits` reads nothing from the draft-PR receipt the supervisor writes at open."""
    [row] = items_svc.normalize_evidence([{"kind": "url", "detail": "PR", "url": "http://x"}])
    assert "commit" not in row
    [row] = items_svc.normalize_evidence([{"kind": "url", "url": "http://x", "commit": "   "}])
    assert "commit" not in row


def test_only_a_url_carries_a_commit_among_the_advisory_kinds():
    """`note` and `test` rows point at nothing, so a commit on them is dropped exactly as
    before — the pin is on the one kind the merge receipt uses, not a general loosening."""
    for kind in ("note", "test", "screenshot", "health"):
        [row] = items_svc.normalize_evidence([{"kind": kind, "detail": "x", "commit": MERGE_OID}])
        assert "commit" not in row, kind


def test_a_url_naming_a_commit_satisfies_no_completion_gate():
    """No authority escalates through the field: the gate reads attestations by kind, and a
    building agent that writes `url` + `commit` has attested nothing."""
    rows = items_svc.normalize_evidence([dict(FLEET_MERGE_RECEIPT)])
    assert items_svc.attestation_receipts(rows) == []
    assert not items_svc.has_valid_attestation(rows, commit=MERGE_OID)


def test_a_write_key_without_the_gate_scope_can_record_the_merge(client, auth):
    """THE CALL. The supervisor holds a plain write key — an attestation would be refused to
    it — so the receipt has to land through `update_item` under exactly that key, with the
    commit still on the stored row the next `get_item_details` returns."""
    minted = client.post("/api/api-keys", json={"name": "fleet", "project_id": "core"},
                         headers=auth).json()
    assert "gate" not in (minted.get("scopes") or []), minted
    key = minted["plaintext"]
    item_id = _rpc(client, key, "create_item", {"title": "SA-417"})["structuredContent"]["id"]

    res = _rpc(client, key, "update_item", {"id": item_id, "evidence": [dict(FLEET_MERGE_RECEIPT)]})
    assert not res.get("isError"), res
    assert res["structuredContent"]["evidence_intake"]["dropped"] == []

    got = _rpc(client, key, "get_item_details", {"id": item_id})["structuredContent"]
    urls = [e for e in got["evidence"] if e.get("kind") == "url"]
    assert urls and urls[-1]["commit"] == MERGE_OID, got["evidence"]
