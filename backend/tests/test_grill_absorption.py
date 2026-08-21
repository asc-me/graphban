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
