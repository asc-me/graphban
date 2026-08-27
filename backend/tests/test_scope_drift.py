"""Mechanical scope drift — no LLM, no opinion (GRPH-243 / GRPH-315 / PRD-12).

PRD-12 holds two success criteria that pull against each other:

- *"Drift totals never decrease as a result of rebaselining."*
- *"Report judgment as judgment. Never emit a number that looks like a measurement when
  it is an opinion."*

A monotonic total is precisely such a number. GRPH-315's resolution, now in the v1.1
baseline, is that the halves stay apart and only the countable one carries a figure. So
these tests pin two things above all: that the number never launders through a rebaseline,
and that it never counts anything cosmetic.
"""
import pytest

from app.services import items as items_svc
from app.services import prds as prd_svc
from tests import attest

BODY = (
    "# Spec\n\n"
    "## Problem\n\nNothing checks delivery.\n\n"
    "## Baseline\n\nFreeze the spec at approval.\n\n"
    "## Judging\n\nClassify each completed item.\n\n"
    "## Close report\n\nDelivered vs original intent.\n"
)


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _approve(db, prd):
    # A distinct answer per round. Re-posting the previous one records nothing (GRPH-322):
    # a rebaseline is graded only on answers given after it was requested.
    window = prd_svc.grill_window(db, prd.id)
    prior = prd_svc.grill_history(db, prd.id, since=window)
    prd_svc.record_grill_turns(db, prd.id, prior + [
        {"role": "user", "text": f"An answer, round {len(prd_svc.baseline_chain(db, prd.id))}."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    return prd


@pytest.fixture()
def approved(db):
    return _approve(db, prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY))


def _rebaseline(db, prd, body, reason="Reality differed."):
    prd_svc.request_rebaseline(db, prd, reason_type="correction", reason=reason,
                               requested_by="agent")
    prd_svc.update_prd(db, prd.id, body=body)
    return _approve(db, prd)


# ---- the criterion the whole feature turns on -----------------------------------------
def test_a_rebaseline_does_not_reduce_the_total(db, approved):
    """THE test. If rebaselining could lower the number, every inconvenient drift total
    has a one-click cure and the metric measures willingness to rebaseline."""
    prd_svc.update_prd(db, approved.id, body=BODY.replace(
        "Classify each completed item.", "Classify each item, and batch them."))
    before = prd_svc.scope_drift(db, approved)["total"]
    assert before > 0, "fixture must have produced drift for this to prove anything"

    _rebaseline(db, approved, approved.body)

    assert prd_svc.scope_drift(db, approved)["total"] >= before


def test_the_divergence_a_rebaseline_absorbs_becomes_chain_history(db, approved):
    """Not merely "does not decrease" — the exact size carries across. `current` falls to
    zero because the body now IS the baseline, and `accumulated` rises by what it had
    been reporting. Nothing is lost in the handover."""
    prd_svc.update_prd(db, approved.id,
                       body=BODY.replace("Classify each completed item.", "Rewritten."))
    before = prd_svc.scope_drift(db, approved)
    assert (before["accumulated"], before["current"]) == (0, 1)

    _rebaseline(db, approved, approved.body)
    after = prd_svc.scope_drift(db, approved)

    assert (after["accumulated"], after["current"], after["total"]) == (1, 0, 1)


def test_drift_accumulates_across_several_rebaselines(db, approved):
    """Frozen history. Each transition contributes and nothing later can lower it — that
    is what makes the chain, rather than a rule someone must remember, the guarantee."""
    _rebaseline(db, approved, BODY.replace("Freeze the spec at approval.", "Changed once."))
    _rebaseline(db, approved, approved.body.replace("Classify each completed item.",
                                                    "Changed twice."))
    out = prd_svc.scope_drift(db, approved)

    assert out["accumulated"] == 2
    assert [s["from"] + "->" + s["to"] for s in out["segments"]] == ["v1.0->v1.1",
                                                                    "v1.1->v1.2"]


def test_editing_the_body_back_toward_the_baseline_does_lower_the_total(db, approved):
    """Deliberately allowed. The criterion bars *rebaselining* from lowering the number,
    because only rebaselining could launder it. An author who undoes an edit undid the
    drift, and reporting otherwise would be the dishonesty in the other direction."""
    prd_svc.update_prd(db, approved.id,
                       body=BODY.replace("Classify each completed item.", "Rewritten."))
    assert prd_svc.scope_drift(db, approved)["total"] == 1

    prd_svc.update_prd(db, approved.id, body=BODY)
    assert prd_svc.scope_drift(db, approved)["total"] == 0


# ---- cosmetic churn is not scope change -----------------------------------------------
def test_a_rename_is_never_counted(db, approved):
    """"Noise wearing a serious face" is PRD-12's own phrase for this, and the AL-96
    failure it exists to avoid repeating. A retitled heading moved a label, not intent."""
    prd_svc.update_prd(db, approved.id, body=BODY.replace("## Judging", "## Judging work"))
    out = prd_svc.scope_drift(db, approved)

    assert out["current"] == 0 and out["total"] == 0
    assert out["renamed"] == [("Judging", "Judging work")]


def test_reflowing_a_paragraph_is_not_drift(db, approved):
    """Section digests normalise whitespace, so rewrapping prose must not register."""
    prd_svc.update_prd(db, approved.id,
                       body=BODY.replace("Classify each completed item.",
                                         "Classify\neach completed\nitem."))
    assert prd_svc.scope_drift(db, approved)["current"] == 0


# ---- scope added after intent was agreed ----------------------------------------------
def test_an_item_linked_after_approval_is_reported_as_added_scope(db, approved):
    item = items_svc.create_item(db, title="Late", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    added = prd_svc.scope_drift(db, approved)["scope_added"]

    assert [r["id"] for r in added] == [item.key]
    assert added[0]["inferred"] is False


def test_added_scope_is_measured_from_the_first_baseline_not_the_governing_one(db, approved):
    """A rebaseline must not wipe the record of scope that arrived before it — the same
    laundering the accumulated count exists to prevent, one field over."""
    item = items_svc.create_item(db, title="Early addition", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    _rebaseline(db, approved, BODY.replace("Freeze the spec at approval.", "Changed."))

    assert [r["id"] for r in prd_svc.scope_drift(db, approved)["scope_added"]] == [item.key]


def test_relinking_stamps_but_an_ordinary_edit_does_not(db, approved):
    """If every touch restamped the link, drift would climb on activity alone — and a
    metric that rises when you work is one people learn to ignore."""
    item = items_svc.create_item(db, title="Work", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    db.refresh(item)
    stamped = item.prd_linked_at

    items_svc.update_item(db, item.id, title="Work, retitled", prd_id=approved.id)
    db.refresh(item)
    assert item.prd_linked_at == stamped


def test_a_link_with_no_recorded_time_is_flagged_rather_than_invented(db, approved):
    """Rows predating the column read from `created_at`. That is a fallback, not a
    measurement, and it says so — per item and in a total — instead of backfilling a
    timestamp nobody recorded."""
    item = items_svc.create_item(db, title="Legacy", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    item.prd_linked_at = None
    db.commit()

    out = prd_svc.scope_drift(db, approved)
    assert out["inferred_link_times"] == 1
    assert [r["inferred"] for r in out["scope_added"]] == [True]


# ---- intent that ended with nothing delivered -----------------------------------------
def test_baselined_sections_with_no_delivered_work_are_reported(db, approved):
    item = items_svc.create_item(db, title="Built", project_id="core",
                                 prd_id=approved.id, prd_section="Baseline")
    attest.complete(db, item.id)
    items_svc.create_item(db, title="Planned only", project_id="core",
                          prd_id=approved.id, prd_section="Judging")

    out = prd_svc.scope_drift(db, approved)
    assert out["intent_undelivered"] == ["Close report", "Judging"]
    assert "Baseline" not in out["intent_undelivered"]


# ---- contract -------------------------------------------------------------------------
def test_a_prd_with_no_baseline_is_not_governed(db):
    """Never a zero, matching `baseline_drift` and `completeness`. A PRD with no agreed
    intent has not "not drifted"."""
    prd = prd_svc.create_prd(db, title="Draft", project_id="core", body=BODY)
    assert prd_svc.scope_drift(db, prd)["governed"] is False


def test_it_needs_no_chat_provider(db, approved, monkeypatch):
    """The stated reason this anchors the slice: it is the half of drift that works on an
    instance with nothing configured. A provider call here would be a silent dependency."""
    from app.services import platform as platform_svc

    def boom(*a, **k):
        raise AssertionError("scope drift must not consult a model")

    monkeypatch.setattr(platform_svc, "resolve_chat", boom)
    prd_svc.update_prd(db, approved.id,
                       body=BODY.replace("Classify each completed item.", "Rewritten."))

    assert prd_svc.scope_drift(db, approved)["total"] == 1


def test_scope_drift_is_readable_over_the_api(client, auth, db, approved):
    prd_svc.update_prd(db, approved.id,
                       body=BODY.replace("Classify each completed item.", "Rewritten."))
    out = client.get(f"/api/prds/{approved.id}/scope-drift", headers=auth).json()

    assert out["governed"] is True and out["total"] == 1
