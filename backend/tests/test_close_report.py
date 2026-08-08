"""Delivered work against ORIGINAL intent (GRPH-245 / PRD-12).

The payoff of the whole feature, and the one surface that does NOT read the governing
baseline. Sign-off is judged against the current spec; closing reads against the **first**
one — *"the work is done and signed off, and here is the drift that accumulated along the
way: what was added, what was dropped, and at which baseline each change happened."*

Reading the governing baseline here would make the report agree with itself by
construction, since the governing baseline is where the spec ended up. That is the failure
these tests mostly exist to prevent.

The audience is a product manager deciding whether dropped scope should be picked back up,
so the two distinctions that matter are: **dropped from the spec** versus **never built**
(somebody decided, versus nobody did), and intent that was never in the original at all.
"""
import pytest

from app.services import items as items_svc
from app.services import prds as prd_svc

BODY = (
    "# Spec\n\n"
    "## Problem\n\nNothing checks delivery.\n\n"
    "## Baseline\n\nFreeze the spec at approval.\n\n"
    "## Judging\n\nClassify each completed item against the goal.\n\n"
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
    window = prd_svc.grill_window(db, prd.id)
    prior = prd_svc.grill_history(db, prd.id, since=window)
    prd_svc.record_grill_turns(db, prd.id, prior + [
        {"role": "user", "text": f"Answer, round {len(prd_svc.baseline_chain(db, prd.id))}."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    return prd


@pytest.fixture()
def approved(db):
    return _approve(db, prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY))


def _rebaseline(db, prd, body, reason="Reality differed."):
    prd_svc.request_rebaseline(db, prd, reason_type="correction", reason=reason,
                               requested_by="agent:t")
    prd_svc.update_prd(db, prd.id, body=body)
    return _approve(db, prd)


def _deliver(db, prd, section, title="Work"):
    item = items_svc.create_item(db, title=title, project_id="core",
                                 prd_id=prd.id, prd_section=section)
    items_svc.update_item(db, item.id, status="done")
    db.refresh(item)
    return item


def _row(out, section):
    return [s for s in out["sections"] if s["section"] == section][0]


# ---- it reads the ORIGINAL, not the governing baseline ----------------------------------
def test_intent_dropped_by_a_rebaseline_still_appears(db, approved):
    """THE test. Against the governing baseline this section does not exist, so the report
    would agree with itself and the PM would never learn something was cut."""
    _rebaseline(db, approved, BODY.replace(
        "## Judging\n\nClassify each completed item against the goal.\n\n", ""))

    out = prd_svc.close_report(db, approved)
    assert out["dropped"] == ["Judging"]
    assert _row(out, "Judging")["dropped_at"] == "v1.1"


def test_dropped_from_the_spec_is_not_the_same_as_never_built(db, approved):
    """Somebody decided, versus nobody did. Conflating them tells a PM a choice was made
    when none was — and this report exists for exactly that choice."""
    _rebaseline(db, approved, BODY.replace(
        "## Judging\n\nClassify each completed item against the goal.\n\n", ""))

    out = prd_svc.close_report(db, approved)
    assert out["dropped"] == ["Judging"]
    assert "Judging" not in out["never_delivered"]
    assert "Close report" in out["never_delivered"]


def test_the_report_names_the_baseline_each_change_happened_at(db, approved):
    """"At which baseline each change happened" — without it the drift is a fact with no
    story, and a PM cannot tell an early correction from a late one."""
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten once."))
    _rebaseline(db, approved, approved.body.replace(
        "## Judging\n\nRewritten once.\n\n", ""))

    hist = _row(prd_svc.close_report(db, approved), "Judging")["history"]
    assert [(e["version"], e["change"]) for e in hist] == [("v1.1", "modified"),
                                                           ("v1.2", "removed")]


def test_the_original_and_governing_versions_are_both_reported(db, approved):
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))
    out = prd_svc.close_report(db, approved)

    assert (out["original_version"], out["governing_version"]) == ("v1.0", "v1.1")
    assert [c["version"] for c in out["chain"]] == ["v1.0", "v1.1"]
    assert out["chain"][1]["reason_type"] == "correction"


# ---- renames do not orphan the work done under the old name ------------------------------
def test_a_renamed_section_keeps_its_original_name_and_its_work(db, approved):
    """A PM agreed to the ORIGINAL name, so that is what the report leads with. Work filed
    under either title belongs to the same intent — reporting it as one drop and one
    arrival would invent a decision nobody made."""
    item = _deliver(db, approved, "Judging")
    _rebaseline(db, approved, BODY.replace("## Judging", "## Judging work"))

    row = _row(prd_svc.close_report(db, approved), "Judging")
    assert row["current_title"] == "Judging work"
    assert row["fate"] == "delivered" and row["delivered_items"] == [item.key]


def test_work_filed_under_the_new_title_also_counts(db, approved):
    _rebaseline(db, approved, BODY.replace("## Judging", "## Judging work"))
    item = _deliver(db, approved, "Judging work")

    assert _row(prd_svc.close_report(db, approved), "Judging")["delivered_items"] == [item.key]


def test_work_filed_under_an_INTERMEDIATE_title_is_not_orphaned(db, approved):
    """A section renamed twice, with the work done while it held the middle name. Matching
    only the first and last titles handles a single rename and quietly loses this — the
    shape that survives a test suite, because one rename is what everyone writes.

    The work is real and was done against this intent. Reporting the section as
    undelivered would tell a PM to pick up something already built."""
    _rebaseline(db, approved, BODY.replace("## Judging", "## Judging work"))
    item = _deliver(db, approved, "Judging work")
    _rebaseline(db, approved, approved.body.replace("## Judging work", "## Assessment"))

    row = _row(prd_svc.close_report(db, approved), "Judging")
    assert row["current_title"] == "Assessment"
    assert row["delivered_items"] == [item.key] and row["fate"] == "delivered"


# ---- retroactive legitimisation stays visible ---------------------------------------------
def test_intent_that_entered_later_is_reported_as_expanded_scope(db, approved):
    """PRD-12: work later covered by an expanded baseline must show as expanded-scope
    rather than quietly reading as though it had been agreed at the start. GRPH-318 now
    refuses a rebaseline that adds sections, so this should be empty in practice — it is
    still computed, because a PRD baselined before that rule is exactly the case worth
    surfacing."""
    prd_svc.request_rebaseline(db, approved, reason_type="scope-change", reason="More.",
                               requested_by="agent:t")
    # Bypass the GRPH-318 guard the way legacy data does — by freezing directly.
    approved.body = BODY + "\n## Snuck in later\n\nWas never agreed.\n"
    db.commit()
    prd_svc.freeze_baseline(db, approved)

    out = prd_svc.close_report(db, approved)
    assert out["expanded_scope"] == ["Snuck in later"]
    assert _row(out, "Snuck in later")["introduced_at"] == "v1.1"


def test_work_attached_after_approval_is_carried_through(db, approved):
    """The other half of "what was added": items linked after intent was first agreed."""
    item = _deliver(db, approved, "Judging")

    added = prd_svc.close_report(db, approved)["added_after_approval"]
    assert [r["id"] for r in added] == [item.key]


# ---- the close itself ----------------------------------------------------------------------
def test_the_report_carries_the_dispositions_from_the_close(db, approved):
    """What a PM most needs: not just that something was dropped, but what was decided
    about it and where it went."""
    _deliver(db, approved, "Baseline")
    dispositions = [{"section": s, "disposition": "deferred", "reason": "Not for v1."}
                    for s in prd_svc.dropped_intent(db, approved)]
    prd_svc.close_prd(db, approved, dispositions=dispositions, closed_by="user:1")

    out = prd_svc.close_report(db, approved)
    assert out["closed"]["mode"] == "mechanical"
    assert _row(out, "Judging")["disposition"]["reason"] == "Not for v1."


def test_the_report_works_before_the_prd_is_closed(db, approved):
    """It is the artifact you read to DECIDE whether to close. Requiring a close first
    would make it useless at the only moment it matters."""
    out = prd_svc.close_report(db, approved)
    assert out["governed"] is True and out["closed"] is None


# ---- naming honesty --------------------------------------------------------------------------
def test_it_never_claims_the_prd_is_complete(db, approved):
    """PRD-12 is explicit: the platform assesses whether CLAIMED work covers STATED intent
    and must never render that as a finished PRD. So no verdict, no score, no percentage —
    the counts describe what happened and the judgement belongs to the reader."""
    _deliver(db, approved, "Baseline")
    out = prd_svc.close_report(db, approved)

    assert not [k for k in out if k in ("complete", "percent", "score", "pass", "verdict")]


def test_a_prd_with_no_baseline_is_not_governed(db):
    prd = prd_svc.create_prd(db, title="Draft", project_id="core", body=BODY)
    assert prd_svc.close_report(db, prd)["governed"] is False


def test_the_report_is_readable_over_the_api(client, auth, db, approved):
    _rebaseline(db, approved, BODY.replace(
        "## Judging\n\nClassify each completed item against the goal.\n\n", ""))
    out = client.get(f"/api/prds/{approved.id}/close-report", headers=auth).json()

    assert out["original_version"] == "v1.0" and out["dropped"] == ["Judging"]
