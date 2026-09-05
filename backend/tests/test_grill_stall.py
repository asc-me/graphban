"""A grill that has stopped moving says so (follow-up to GRPH-485 / #610).

The loop, reported from use: the grill keeps asking, the author keeps answering, no
dimension changes, and every surface reports the same unchanged outcomes as though the
last answer merely had not been good enough. Four rounds in, the author is the only thing
in the system that knows it is going nowhere.

What this is NOT: a way out. The signal decides nothing — it cannot approve, defer or
grade. The two real exits belong to the author (defer a dimension) and the operator (fix
the grader). A stall detector that quietly approved would be the escalation-to-approved
that #610 refused, wearing a diagnostic's clothes.
"""
from __future__ import annotations

import pytest

from app.services import prds as prd_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _prd(client, auth) -> str:
    return client.post("/api/prds", json={"title": "Spec", "body": "## D1\n\nwork",
                                          "project_id": "core"}, headers=auth).json()["id"]


def _answer(db, prd_id: str, text: str) -> None:
    """One more answer on the end of the transcript, the way every caller records one."""
    history = prd_svc.grill_history(db, prd_id, since=prd_svc.grill_window(db, prd_id))
    prd_svc.record_grill_turns(db, prd_id, history + [{"role": "user", "text": text}],
                               via="test", actor="test")


def test_answers_that_move_nothing_are_counted_and_named(client, auth, db):
    prd_id = _prd(client, auth)
    for i in range(prd_svc.STALL_AFTER_ANSWERS):
        _answer(db, prd_id, f"answer {i}")

    out = prd_svc.stall(db, prd_id)

    assert out["answers_since_progress"] == prd_svc.STALL_AFTER_ANSWERS
    assert out["stalled"] is True, (
        "three answers changed nothing and the grill reports itself as fine; this is the "
        "loop the author was left to notice on their own"
    )


def test_a_grill_that_is_moving_is_not_accused(client, auth, db):
    """The control. Without it `stalled` could be hardcoded true on any active grill."""
    prd_id = _prd(client, auth)
    for i in range(prd_svc.STALL_AFTER_ANSWERS):
        _answer(db, prd_id, f"answer {i}")
    prd_svc.set_dimension(db, prd_id, "scope_edges", "resolved", graded_by="test")

    out = prd_svc.stall(db, prd_id)

    assert out["answers_since_progress"] == 0
    assert out["stalled"] is False


def test_a_deferral_counts_as_progress(client, auth, db):
    """THE case that decided the implementation.

    A deferral is a decision, and it settles a dimension — but it cites no answer, so
    `grill_dimensions.turn_seq` stays NULL. Inferring progress from citations would leave
    an author who just deferred still being told their grill is going nowhere, which is
    both wrong and precisely backwards: they took the exit the message recommends.
    """
    prd_id = _prd(client, auth)
    for i in range(prd_svc.STALL_AFTER_ANSWERS):
        _answer(db, prd_id, f"answer {i}")
    assert prd_svc.stall(db, prd_id)["stalled"] is True

    client.post(f"/api/prds/{prd_id}/grill/defer",
                json={"dimension": "contracts", "reason": "settled by the spike"},
                headers=auth)

    out = prd_svc.stall(db, prd_id)
    assert out["stalled"] is False
    assert out["answers_since_progress"] == 0


def test_writing_an_unanswered_verdict_is_not_progress(client, auth, db):
    """Absence IS `unanswered`, so a verdict that says so changes nothing. Creating the
    row must not read as movement — that would clear the stall on precisely the rounds
    where the grader is finding nothing."""
    prd_id = _prd(client, auth)
    for i in range(prd_svc.STALL_AFTER_ANSWERS):
        _answer(db, prd_id, f"answer {i}")
    prd_svc.set_dimension(db, prd_id, "scope_edges", "unanswered", graded_by="test")

    assert prd_svc.stall(db, prd_id)["stalled"] is True


def test_a_rebaseline_does_not_inherit_the_old_grill_s_stall(client, auth, db):
    """The evidence window moves on a rebaseline (GRPH-322) and the count lives inside
    it. A new interrogation starts with nothing held against it."""
    prd_id = _prd(client, auth)
    for i in range(prd_svc.STALL_AFTER_ANSWERS):
        _answer(db, prd_id, f"answer {i}")
    assert prd_svc.stall(db, prd_id)["stalled"] is True

    from app.models import GrillTurn, Prd
    from sqlalchemy import func, select
    prd = db.get(Prd, prd_id)
    prd.grill_from_seq = (db.scalar(select(func.max(GrillTurn.seq))
                                    .where(GrillTurn.prd_id == prd_id)) or 0) + 1
    db.commit()

    out = prd_svc.stall(db, prd_id)
    assert out["answers_since_progress"] == 0
    assert out["stalled"] is False


def test_the_editor_s_read_carries_the_stall(client, auth, db):
    """It has to arrive where the author is looking. `answer_grill` returning it is not
    the same as the panel showing it — that gap is exactly what #610 had to fix for the
    ungraded flag."""
    prd_id = _prd(client, auth)
    for i in range(prd_svc.STALL_AFTER_ANSWERS):
        _answer(db, prd_id, f"answer {i}")

    state = client.get(f"/api/prds/{prd_id}/grill", headers=auth).json()

    assert state["stall"]["stalled"] is True
    assert state["stall"]["answers_since_progress"] == prd_svc.STALL_AFTER_ANSWERS


def test_answer_grill_tells_a_relaying_agent(client, auth, db, monkeypatch):
    """An agent has no author reading a panel to get impatient for it — it will relay a
    fourth and a fifth answer as readily as the first. Driven through the real MCP
    endpoint, because the tool returning a key and the transport carrying it are two
    different claims (GRPH-495 was the last time they disagreed).

    The grader here WORKS and keeps finding nothing, which is the stall worth naming. On
    the shipped stub it cannot arise: that bar resolves one dimension per answer, in
    order, so every answer is progress by construction.
    """
    import json

    monkeypatch.setattr(prd_svc, "_grader_id", lambda *a, **k: "anthropic")
    monkeypatch.setattr(prd_svc, "_classify_dimensions", lambda *a, **k: {
        name: {"outcome": "unanswered", "note": "still hand-waving"}
        for name in prd_svc.DIMENSIONS
    })

    key = client.post("/api/api-keys", json={"name": "stall", "scopes": ["read", "write"]},
                      headers=auth).json()["plaintext"]
    prd_id = _prd(client, auth)
    for i in range(prd_svc.STALL_AFTER_ANSWERS - 1):
        _answer(db, prd_id, f"answer {i}")

    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "answer_grill",
                                     "arguments": {"prd_id": prd_id, "answer": "one more"}}},
                    headers={"X-API-Key": key})
    out = json.loads(r.json()["result"]["content"][0]["text"])

    assert out["answers_since_progress"] == prd_svc.STALL_AFTER_ANSWERS
    assert out["stalled"] is True
