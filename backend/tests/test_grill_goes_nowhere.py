"""A grill that advances nothing says so (GRPH-513).

There are two grill modes and the manifest could not tell them apart. `grill_prd` generates
questions and writes nothing — no turn, no dimension, no status move. `answer_grill` is the
loop that records, grades and advances. And `grill_prd`'s description ended:

> *"Markdown list; answer via update_prd."*

True about how to record an answer, and silent about what recording it that way achieves —
which is nothing, as far as the grill is concerned. `update_prd` replaces a body; it touches
neither `grill_turns` nor `grill_dimensions`, and those are exactly what `sync_status` reads.

Found by doing it. Four rounds against GRPH-P25, every answer recorded as instructed, ending
with 25,293 characters of worked-through document, **0 turns, 0 dimensions, status `draft`** —
while the smaller GRPH-P24 reached `approved` the ordinary way. Nothing was broken. Nothing
said so.

**It is not a wording nit.** A credential can hold `grill_prd` without `answer_grill`: the
manifest is trimmed by scope and `answer_grill` is not role-gated, so this is a tool-list
difference rather than a refusal an agent would notice. Approval is earned by finishing the
grill, so a PRD grilled this way can never leave `draft`.

**The load-bearing test is the CONSEQUENCE**, per the ticket — not that a description mentions
`answer_grill`, which passes on a string match and proves nothing.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture()
def key(client, auth):
    return client.post("/api/api-keys", json={"name": "griller"}, headers=auth).json()["plaintext"]


def _call(client, key, tool, args):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": tool, "arguments": args}},
                    headers={"X-API-Key": key}).json()["result"]
    assert not r.get("isError"), r
    return json.loads(r["content"][0]["text"])


@pytest.fixture()
def prd(client, auth):
    return client.post("/api/prds", json={
        "title": "A spec to grill", "project_id": "core",
        "body": "# A spec\n\n## D1 — the thing\n\nbuild the thing.\n",
    }, headers=auth).json()


# ── the gap, pinned as understood rather than accidental ──────────────────────

def test_grilling_and_answering_via_update_prd_advances_nothing(client, auth, key, prd):
    """THE test the ticket asks for. Run the tool, record an answer exactly as the old
    description instructed, and assert the PRD has not moved.

    This pins the gap rather than fixing it: the split between a drafting aid and the
    recording loop is legitimate. What was not legitimate is that the two looked
    interchangeable to an agent holding only one of them.
    """
    from app.db import SessionLocal
    from app.services import prds as prd_svc

    _call(client, key, "grill_prd", {"prd_id": prd["id"]})
    read = _call(client, key, "get_prd", {"prd_id": prd["id"]})
    _call(client, key, "update_prd", {"prd_id": prd["id"], "base_hash": read["body_hash"],
                                      "body": read["body"] + "\n## Answer\n\nBecause X.\n"})

    db = SessionLocal()
    try:
        row = prd_svc.get_prd(db, prd["id"])
        assert row.status == "draft", "answering through update_prd moved the status"
        assert prd_svc.grill_turns(db, row.id) == [], "update_prd recorded a grill turn"
    finally:
        db.close()


# ── what the payload now says about that ──────────────────────────────────────

def test_the_payload_says_it_records_nothing(client, key, prd):
    """The signal fires at the moment it becomes true, not four rounds later. An agent reads
    the description once at connect and the response on every call."""
    out = _call(client, key, "grill_prd", {"prd_id": prd["id"]})
    assert out["records_answers"] is False


def test_the_counts_are_real_and_start_at_zero(client, key, prd):
    """`turns_recorded: 0` is the number that would have shown, on round one, that four
    rounds of work were not counting."""
    out = _call(client, key, "grill_prd", {"prd_id": prd["id"]})
    assert out["turns_recorded"] == 0
    # All four, because `completion` treats an ungraded dimension as unanswered. Asserting
    # only "is a list" passed against a hardcoded `[]` and survived the sabotage pass.
    assert sorted(out["dimensions_outstanding"]) == [
        "contracts", "failure_modes", "open_decisions", "scope_edges"]


def test_the_counts_move_when_something_actually_records(client, auth, key, prd):
    """The complement, and the one that makes the counts worth printing. Hardcoded zeros
    would satisfy every test above while telling an agent nothing — they have to track the
    state they claim to report."""
    from app.db import SessionLocal
    from app.services import prds as prd_svc

    db = SessionLocal()
    try:
        prd_svc.record_grill_turns(db, prd["id"], [{"role": "user", "text": "an answer"}])
    finally:
        db.close()

    out = _call(client, key, "grill_prd", {"prd_id": prd["id"]})
    assert out["turns_recorded"] == 1, "the count is not reading real grill state"


def test_dimensions_outstanding_shrinks_as_the_grill_is_answered(client, key, prd):
    """The complement for the other count. Resolving a dimension must remove it, or the
    field is a constant wearing a measurement's name."""
    from app.db import SessionLocal
    from app.services import prds as prd_svc

    db = SessionLocal()
    try:
        prd_svc.set_dimension(db, prd["id"], "contracts", "resolved",
                              note="settled", turn_seq=1, graded_by="test")
    finally:
        db.close()

    out = _call(client, key, "grill_prd", {"prd_id": prd["id"]})
    assert "contracts" not in out["dimensions_outstanding"]
    assert sorted(out["dimensions_outstanding"]) == [
        "failure_modes", "open_decisions", "scope_edges"]


def test_the_existing_retry_signal_survives(client, key, prd):
    """GRPH-505 put `retried` here so a caller could tell contention from a hung server.
    Adding fields must not quietly displace it."""
    out = _call(client, key, "grill_prd", {"prd_id": prd["id"]})
    assert "retried" in out and "questions" in out


def test_the_description_no_longer_signposts_update_prd():
    """Weaker than the consequence test above and still worth having: the wrong instruction
    is what sent someone down a path with no exit, and it was in the manifest."""
    from app.mcp_server import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "grill_prd")["description"]
    assert "update_prd" not in desc
    assert "answer_grill" in desc
