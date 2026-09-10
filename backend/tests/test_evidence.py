"""A receipt the server refuses is SAID, and a plain string is not refused (GRPH-839).

**Found by doing it.** Recording GRPH-838 with
`update_item(id, status="review", evidence=["fleet suite: 1072 passed ...", ...])` returned
the updated item, moved the status, and stored nothing. `normalize_evidence` skipped every
non-dict and `append_evidence` appended the empty result, so the call succeeded with its
proof discarded — an item reaching `review` with "receipts sent" and a bare record.

That is the absence-reads-as-clean shape in the tool that produces it most expensively: the
reply's `evidence` array did show the truth (zero), but only to a caller that counts it
against what it sent, and a caller holding a 200 has no reason to. So the assertions here are
about the CONSEQUENCE, not the coercion. "A string normalises to a note" passes trivially;
what was broken is that a caller could not tell the difference between a receipt stored and a
receipt destroyed.

The fix picked COERCION over refusal (the item offered both): `kind` is advisory and already
falls back to `note`, so a bare string is the degenerate note and inventing its wrapper loses
nothing. Refusal would have been correct and more expensive — an agent whose receipts bounce
mid-handover has to reformat and resend, and the failure this replaces was that it did not
know to.
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


@pytest.fixture()
def mcp_key(client, auth):
    return client.post("/api/api-keys", json={"name": "ev", "project_id": "core"},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def item(client, mcp_key):
    """Made through the tool, and the session is CLOSED before any test runs: a `db` fixture
    left open holds the SQLite write lock, and `update_item` writes an llm span."""
    return _ok(client, mcp_key, "create_item", {"title": "Built it"})["id"]


# ---- the defect, through the tool that had it --------------------------------------------


def test_a_string_receipt_and_a_dict_receipt_both_land(client, mcp_key, item):
    """The reported failure, end to end. One string, one dict, one call — never one landing
    quietly while the other vanishes."""
    out = _ok(client, mcp_key, "update_item", {
        "id": item,
        "status": "review",
        "evidence": ["fleet suite: 1072 passed, 0 failed",
                     {"kind": "test", "detail": "backend suite green on sqlite"}],
    })

    details = [e["detail"] for e in out["evidence"]]
    assert details == ["fleet suite: 1072 passed, 0 failed", "backend suite green on sqlite"]
    # The degenerate note, not some new kind: an unlabelled receipt goes where every other
    # unlabelled receipt goes, and the dict's own `kind` is untouched beside it.
    assert [e["kind"] for e in out["evidence"]] == ["note", "test"]
    assert out["evidence_intake"] == {"sent": 2, "added": 2, "dropped": []}


def test_the_reply_counts_what_was_accepted_against_what_was_sent(client, mcp_key, item):
    """The half that survives the next drop. Coercion fixes the strings; this is what tells a
    caller when something else was refused, without asking it to go and count."""
    out = _ok(client, mcp_key, "update_item", {
        "id": item,
        "evidence": ["a note", 42, {"kind": "test", "detail": "green"}, {"kind": "note"}],
    })

    intake = out["evidence_intake"]
    assert intake["sent"] == 4
    assert intake["added"] == 2
    assert [d["index"] for d in intake["dropped"]] == [1, 3]
    # Named, not counted. A caller holding the payload can point at the entry.
    assert "not a receipt" in intake["dropped"][0]["reason"]
    assert "neither detail nor url" in intake["dropped"][1]["reason"]
    assert intake["sent"] - len(intake["dropped"]) == intake["added"]


def test_an_update_that_sent_no_evidence_reports_no_intake_at_all(client, mcp_key, item):
    """`dropped: []` on a call that carried nothing says "nothing was discarded" where the
    truth is that nobody looked — the exact reading this field exists to prevent. Absent is
    the third answer, and it is the one the schema's optionality buys."""
    out = _ok(client, mcp_key, "update_item", {"id": item, "title": "Renamed"})
    assert "evidence_intake" not in out


def test_an_empty_evidence_list_still_reports_the_intake(client, mcp_key, item):
    """`evidence: []` is a caller that sent evidence and had none survive — different from a
    caller that sent none. Zero of zero is a fact; silence is not."""
    out = _ok(client, mcp_key, "update_item", {"id": item, "evidence": []})
    assert out["evidence_intake"] == {"sent": 0, "added": 0, "dropped": []}


def test_a_receipt_already_recorded_counts_as_added_not_dropped(client, mcp_key, item):
    """`append_evidence` treats an identical resend as the retry it is. Reporting the retry as
    a refusal would send an agent to re-run work that is already on the record — which is the
    same class of wrong answer, pointing the other way."""
    args = {"id": item, "evidence": [{"kind": "test", "detail": "green"}]}
    _ok(client, mcp_key, "update_item", args)
    again = _ok(client, mcp_key, "update_item", args)

    assert again["evidence_intake"] == {"sent": 1, "added": 1, "dropped": []}
    assert len(again["evidence"]) == 1, "the retry must not double the receipt"


# ---- the shapes that must not become notes -----------------------------------------------


def test_one_receipt_sent_unwrapped_is_one_note_not_one_per_character():
    """`evidence="ran the suite"` is a payload shape mistake, and iterating it would store
    thirteen single-letter notes — a worse silent corruption than the drop being fixed."""
    rows = items_svc.normalize_evidence("ran the suite")
    assert rows == [{"kind": "note", "detail": "ran the suite", "url": ""}]
    assert items_svc.evidence_intake("ran the suite")["evidence_intake"]["sent"] == 1


def test_a_lone_receipt_object_is_one_receipt_not_one_note_per_key():
    """The same mistake with the shape the schema documents, and the one coercion made
    dangerous: iterating a dict yields its KEYS, which used to be dropped as non-dicts and
    would now each store as a note reading `kind`, `detail`, `tests_failed`."""
    rows = items_svc.normalize_evidence({"kind": "test", "detail": "green"})
    assert rows == [{"kind": "test", "detail": "green", "url": ""}]


def test_a_blank_string_is_dropped_and_said_rather_than_stored_empty():
    """A receipt with nothing in it is still nothing in it. Coercion repairs the wrapper, not
    the content — an empty note on the record would read as a receipt to every surface that
    counts them."""
    intake = items_svc.evidence_intake(["   ", "real"])["evidence_intake"]
    assert intake["added"] == 1
    assert [d["index"] for d in intake["dropped"]] == [0]


def test_a_string_does_not_smuggle_past_a_structured_kind():
    """`sabotage`, `attestation` and `lesson` are not advisory: a structured kind that accepts
    unstructured input is the free-text field with a new name. A string has no kind at all, so
    it lands as `note` — it cannot arrive claiming to be proof."""
    [row] = items_svc.normalize_evidence(["I sabotaged it, trust me"])
    assert row["kind"] == "note"
    assert not items_svc.has_effective_sabotage([row])
