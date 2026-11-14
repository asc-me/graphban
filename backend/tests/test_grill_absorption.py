"""Answers the body has not absorbed (GRPH-430).

GRPH-424 closed *repo copy vs ledger*. This is the same absence one level in — *ledger body vs
its own grill*. A PRD can be interrogated across five rounds, settle real decisions, and keep a
body that still describes the older ones. To `decompose_prd`, `prd_coverage`, the completeness
pass and anyone reading it, that document is indistinguishable from one that absorbed
everything.

Observed live while grilling GRPH-P22: five batches settled the seat-file contract as
per-adapter, added a security non-goal, and introduced salvage-on-reap, supervisor adoption, a
repo-scoped lockfile and a notification path — none of which were in the body. Then, writing it
up, the same drift happened three more times in a row: each body push moved the derived status
(`draft` -> `review` -> `approved`) and the header line describing that status was instantly
wrong again.

**The check is staleness, never correctness.** Whether a section genuinely *reflects* an answer
is a judgement, and a check claiming to make it would either nag constantly or pass on anything
— both of which end with somebody switching it off.
"""
import datetime as dt

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


def _prd(db, body="# Spec\n\n## Overview\n\nfirst draft.\n"):
    return prd_svc.create_prd(db, title="Subject", body=body, project_id="core")


def _answer(db, prd, text="the author said so"):
    history = prd_svc.grill_history(db, prd.id, since=prd_svc.grill_window(db, prd.id))
    prd_svc.record_grill_turns(db, prd.id, history + [{"role": "user", "text": text}],
                               via="agent", actor="agent:test")


def test_an_ungrilled_prd_is_absorbed(db):
    """Nothing outstanding to absorb. Reporting these as stale would make the signal
    meaningless on the majority of rows, which is how a check gets switched off."""
    prd = _prd(db)

    out = prd_svc.grill_absorption(db, prd.id)

    assert out["absorbed"] is True
    assert out["answers_behind"] == 0


def test_an_answer_newer_than_the_body_is_reported_behind(db):
    """The whole feature in one assertion."""
    prd = _prd(db)
    _answer(db, prd, "we decided X")

    out = prd_svc.grill_absorption(db, prd.id)

    assert out["absorbed"] is False
    assert out["answers_behind"] == 1
    assert out["behind_seqs"] == [0]


def test_editing_the_body_clears_it(db):
    """And the way out is the intended one: write the answer down."""
    prd = _prd(db)
    _answer(db, prd, "we decided X")
    assert prd_svc.grill_absorption(db, prd.id)["absorbed"] is False

    prd_svc.update_prd(db, prd.id, body="# Spec\n\n## Overview\n\nX, as decided.\n")

    assert prd_svc.grill_absorption(db, prd.id)["absorbed"] is True


def test_a_status_change_does_not_count_as_absorbing_anything(db):
    """THE trap this design exists around, and why `prds.updated_at` could not be used.

    That column carries `onupdate`, so it moves for any row write — and answering a grill
    writes the row whenever the answer changes the derived status. The body would look freshly
    edited at exactly the moments it had NOT been touched, which is the one case the check
    exists to catch. Delete `body_updated_at` and use `updated_at` instead and this test fails
    while every other test in this file still passes."""
    prd = _prd(db)
    _answer(db, prd, "we decided X")
    behind_before = prd_svc.grill_absorption(db, prd.id)["answers_behind"]

    # A status transition with no body edit — the shape a grill answer produces.
    prd_svc.update_prd(db, prd.id, status="review")

    out = prd_svc.grill_absorption(db, prd.id)
    assert out["absorbed"] is False, "a status change was mistaken for the body being written"
    assert out["answers_behind"] == behind_before


def test_resaving_an_identical_body_does_not_count_as_an_edit(db):
    """A client that saves the whole object, or echoes an unchanged body back, must not make
    'the body absorbed the grill' true by autosave."""
    body = "# Spec\n\n## Overview\n\nfirst draft.\n"
    prd = _prd(db, body=body)
    _answer(db, prd, "we decided X")

    prd_svc.update_prd(db, prd.id, body=body)

    assert prd_svc.grill_absorption(db, prd.id)["absorbed"] is False


def test_a_rebaseline_window_only_owes_the_current_round(db):
    """Counted inside the evidence window, matching what `completion` grades: after a
    rebaseline, the previous round's answers are not what this body owes."""
    prd = _prd(db)
    _answer(db, prd, "round one")
    prd_svc.update_prd(db, prd.id, body="# Spec\n\n## Overview\n\nround one written up.\n")
    _answer(db, prd, "round two")

    # Move the evidence window past everything said so far.
    prd.grill_from_seq = 99
    db.commit()

    assert prd_svc.grill_absorption(db, prd.id)["absorbed"] is True


def test_grill_state_carries_it(db):
    """So the REST grill endpoint reports it without a second call — the surface a reviewer
    reads is the surface that should say the document is behind."""
    prd = _prd(db)
    _answer(db, prd, "we decided X")

    state = prd_svc.grill_state(db, prd.id)

    assert state["absorption"]["absorbed"] is False
    assert state["absorption"]["answers_behind"] == 1


def test_legacy_rows_without_a_body_timestamp_are_optimistic(db):
    """0082 backfills `body_updated_at` from `updated_at`, but a row that somehow has neither
    must not flag. A check that cries wolf about history gets switched off before it catches
    anything real."""
    prd = _prd(db)
    prd.body_updated_at = None
    prd.updated_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    db.commit()
    _answer(db, prd, "we decided X")

    assert prd_svc.grill_absorption(db, prd.id)["absorbed"] is True


def test_an_answer_already_written_in_the_body_is_absorbed(db):
    prd = _prd(db, body="# Spec\n\n## Overview\n\nWe decided X.\n")
    prd_svc.update_prd(db, prd.id, body="# Spec\n\n## Overview\n\nWe decided X, firmly.\n")
    _answer(db, prd, "we decided x")

    out = prd_svc.grill_absorption(db, prd.id)

    assert out["absorbed"] is True


def test_an_answer_not_in_the_body_stays_behind(db):
    prd = _prd(db)
    _answer(db, prd, "we decided something unwritten")

    assert prd_svc.grill_absorption(db, prd.id)["absorbed"] is False


# ---- GRPH-1516: a fold the body states in its own markup is absorbed ----------------------


def test_an_answer_the_section_states_in_markdown_is_absorbed(db):
    """The P67 shape: the section was rewritten with `update_prd(section=…)` to state the
    decision, in markdown, before the answer was recorded. Characters differ (`**`, backticks,
    a bullet, a curly apostrophe); the words do not.

    Sabotage: make `answer_in_body` return False and this is `absorbed: False`."""
    prd = _prd(db, body="# Spec\n\n## Wire\n\nTBD.\n")
    prd_svc.update_prd(db, prd.id, section="Wire", body=(
        "- **The wire path is `/v1/systemone`.** It doesn’t change per head.\n"
        "- The credential keeps one `model` field, with a hint.\n"))
    _answer(db, prd, "The wire path is /v1/systemone. It doesn't change per head.")

    out = prd_svc.grill_absorption(db, prd.id)

    assert out["absorbed"] is True, out


def test_two_answer_sentences_restated_in_two_places_are_absorbed(db):
    prd = _prd(db, body=(
        "# Spec\n\n## Goals\n\nSeats expire after thirty minutes.\n\n"
        "## Non-goals\n\nThe grill classifier stays out.\n"))
    _answer(db, prd, "The grill classifier stays out; seats expire after thirty minutes.")

    assert prd_svc.grill_absorption(db, prd.id)["absorbed"] is True


def test_one_sentence_missing_keeps_the_answer_behind(db):
    """Every sentence has to be there: half a decision written down is not absorbed."""
    prd = _prd(db, body="# Spec\n\n## Goals\n\nSeats expire after thirty minutes.\n")
    _answer(db, prd, "Seats expire after thirty minutes. Reviewers may extend them once.")

    out = prd_svc.grill_absorption(db, prd.id)
    assert out["absorbed"] is False and out["answers_behind"] == 1


def test_a_word_inside_a_longer_word_is_not_a_match(db):
    """Matching on word boundaries: "use" is not absorbed by "reuse"."""
    prd = _prd(db, body="# Spec\n\n## Goals\n\nWe reuse it.\n")
    _answer(db, prd, "we use it")

    assert prd_svc.grill_absorption(db, prd.id)["absorbed"] is False


def test_answer_grill_reports_body_absorbed_through_the_tool(client, auth):
    """The field the issue was filed against, read through `answer_grill` itself."""
    key = client.post("/api/api-keys", json={"name": "grill-1516"},
                      headers=auth).json()["plaintext"]

    def call(tool, args):
        res = client.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}},
            headers={"X-API-Key": key}).json()["result"]
        assert not res.get("isError"), res
        return res["structuredContent"]

    prd = call("create_prd", {"title": "Absorb", "project_id": "core",
                              "body": "# Absorb\n\n## Decision\n\nTBD.\n"})
    call("update_prd", {"prd_id": prd["id"], "section": "Decision",
                        "body": "**Seats are single use.** A crash is recovered by reissue.\n"})
    got = call("answer_grill", {"prd_id": prd["id"],
                                "answer": "Seats are single use. A crash is recovered by reissue."})
    assert got["body_absorbed"] is True, got
    assert got["answers_body_has_not_absorbed"] == 0
    behind = call("answer_grill", {"prd_id": prd["id"],
                                   "answer": "Reviewers may never extend a seat."})
    assert behind["body_absorbed"] is False
    assert behind["answers_body_has_not_absorbed"] == 1
