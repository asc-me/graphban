"""The completeness pass: what the baseline demands with nothing delivered (GRPH-251).

PRD-12's success criterion, verbatim: *"Work the goal demands but nothing delivered is
reported explicitly — absence is a first-class finding, not an empty list."*

This runs intent → work, the opposite direction from every existing surface. Classifying
work that exists can only find drift and stowaway scope; it can never surface work that
was never done, which is the whole of the completeness question.

The unit of intent is the **section** (GRPH-313). These tests exist mostly to pin the four
ways that choice can go quietly wrong: reading the live body instead of the baseline,
inventing an absence out of a rename, treating a linked-but-unshipped section the same as
an unplanned one, and structurally hiding the section that defines "done".
"""
import pytest

from app.services import items as items_svc
from app.services import prds as prd_svc
from tests import attest

BODY = (
    "# Spec\n\n"
    "## Problem\n\nNothing checks delivery.\n\n"
    "## Success criteria\n\nA reviewer can answer it from one screen.\n\n"
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


def _link(db, prd, section, status="done", title="Work"):
    item = items_svc.create_item(db, title=title, project_id="core",
                                 prd_id=prd.id, prd_section=section)
    if status != "backlog":
        if status == "done":
            attest.complete(db, item.id)
        else:
            items_svc.update_item(db, item.id, status=status)
    return item


def _section(out, title):
    return [s for s in out["sections"] if s["section"] == title][0]


# ---- absence is the product -----------------------------------------------------------
def test_a_section_with_nothing_linked_is_named_as_absent(db, approved):
    """The success criterion in its own words. An empty list here is the failure mode —
    the pass has to say WHICH intent has nothing behind it."""
    _link(db, approved, "Baseline")
    out = prd_svc.completeness(db, approved)

    assert "Judging" in out["absent"] and "Close report" in out["absent"]
    assert "Baseline" not in out["absent"]


def test_planned_but_unshipped_is_not_the_same_finding_as_absent(db, approved):
    """Different failures with different owners. Merging them into one red count tells a
    PM nothing about which of the two they actually have."""
    _link(db, approved, "Judging", status="backlog")
    out = prd_svc.completeness(db, approved)

    assert out["undelivered"] == ["Judging"]
    assert "Judging" not in out["absent"]
    assert _section(out, "Judging")["state"] == "undelivered"


def test_partial_delivery_is_neither_absent_nor_complete(db, approved):
    _link(db, approved, "Judging", status="done", title="Half")
    _link(db, approved, "Judging", status="backlog", title="Other half")
    out = prd_svc.completeness(db, approved)

    s = _section(out, "Judging")
    assert (s["state"], s["planned"], s["delivered"]) == ("partial", 2, 1)
    assert "Judging" not in out["absent"] and "Judging" not in out["undelivered"]


# ---- it measures against the baseline, not the living body ----------------------------
def test_a_section_added_to_the_body_after_approval_is_not_intent(db, approved):
    """Post-approval edits are drift, not agreed intent. Counting them would let anyone
    manufacture work-to-do by typing a heading."""
    prd_svc.update_prd(db, approved.id, body=BODY + "\n## Invented later\n\nSurprise.\n")
    out = prd_svc.completeness(db, approved)

    assert "Invented later" not in [s["section"] for s in out["sections"]]
    assert "Invented later" not in out["absent"]


def test_a_section_deleted_from_the_body_still_demands_work(db, approved):
    """The one that matters most. If deleting a heading erased its demand, the way to make
    a PRD complete would be to delete the parts you did not build."""
    prd_svc.update_prd(db, approved.id, body=BODY.split("## Judging")[0])
    out = prd_svc.completeness(db, approved)

    assert "Judging" in out["absent"]


def test_a_rebaseline_moves_what_completeness_measures_against(db, approved):
    """The pass reads the GOVERNING baseline — `baseline_of` returns the latest — so a
    rebaseline changes the demand. Reading the original instead is the close report's job
    (GRPH-245), and conflating the two would make a rebaseline unable to correct anything.
    """
    prd_svc.request_rebaseline(db, approved, reason_type="correction",
                               reason="Judging was never in scope.", requested_by="agent")
    prd_svc.update_prd(db, approved.id, body=BODY.replace(
        "## Judging\n\nClassify each completed item.\n\n", ""))
    _approve(db, approved)

    out = prd_svc.completeness(db, approved)
    assert out["baseline_version"] == "v1.1"
    assert "Judging" not in out["absent"]


# ---- a rename is not an absence -------------------------------------------------------
def test_a_renamed_section_keeps_its_baseline_identity(db, approved):
    """The section stays addressable by the title the baseline froze, and the report says
    where it went. Note this alone does not exercise the alias lookup — work filed under
    the OLD title matches either way. The test below is the one that catches its loss."""
    _link(db, approved, "Judging")
    prd_svc.update_prd(db, approved.id, body=BODY.replace("## Judging", "## Judging work"))

    out = prd_svc.completeness(db, approved)
    s = _section(out, "Judging")
    assert s["renamed_to"] == "Judging work"
    assert s["state"] == "delivered" and "Judging" not in out["absent"]


def test_work_filed_under_the_new_title_counts_too(db, approved):
    """The payoff of choosing the section as the atom, and the case that actually needs
    the alias lookup. Items created after a rename carry the new title; matching only the
    baseline's would invent an absence for intent that was in fact delivered — a false
    "nothing was built here" in the one report a PM is meant to act on. It would also
    half-work, which is worse than failing, because it stays correct on older data.
    """
    prd_svc.update_prd(db, approved.id, body=BODY.replace("## Judging", "## Judging work"))
    _link(db, approved, "Judging work")

    assert _section(prd_svc.completeness(db, approved), "Judging")["state"] == "delivered"


# ---- framing sections are shown, never demanded of ------------------------------------
def test_framing_sections_are_reported_rather_than_hidden(db, approved):
    """PRD-12's third named problem: the section defining "done" is parsed and then
    structurally ignored, so acceptance criteria are exempt from every check. Dropping
    them here would repeat that exactly."""
    titles = [s["section"] for s in prd_svc.completeness(db, approved)["sections"]]
    assert "Success criteria" in titles and "Problem" in titles


def test_framing_sections_are_never_counted_as_absent(db, approved):
    """"Problem: nothing delivered" is noise, and noise wearing a serious face is the
    AL-96 trust failure. `_PROSE_SECTIONS` stays authoritative for what work means."""
    out = prd_svc.completeness(db, approved)

    assert "Problem" not in out["absent"] and "Success criteria" not in out["absent"]
    assert _section(out, "Success criteria")["framing"] is True
    assert out["demanding_sections"] == 3  # Baseline, Judging, Close report


def test_the_grills_own_decisions_section_is_not_a_missing_feature(db):
    """Found by running the pass against PRD-12's real baseline, not by a test.

    `grill_apply` WRITES `## Decisions from grilling`, and since PRD-15 made grilling the
    approval path it lands on essentially every approved PRD. Classified implementable, it
    reported "nothing delivered" as the top finding on almost every PRD in the instance —
    for a section that records why decisions were settled, not work to build.
    """
    prd = _approve(db, prd_svc.create_prd(
        db, title="Spec", project_id="core",
        body=BODY + "\n## Decisions from grilling\n\n- The atom is the section.\n"))

    out = prd_svc.completeness(db, prd)
    assert "Decisions from grilling" not in out["absent"]
    assert _section(out, "Decisions from grilling")["framing"] is True


# ---- nothing is silently discarded ----------------------------------------------------
def test_work_filed_against_no_baseline_section_is_surfaced(db, approved):
    """GRPH-319 hid a third of this PRD's own work by dropping items on a join mismatch.
    Items that match no baseline section are named, not quietly skipped."""
    _link(db, approved, "Some section that is not in the baseline")

    assert prd_svc.completeness(db, approved)["outside_baseline"] == [
        "Some section that is not in the baseline"]


def test_a_prd_with_no_baseline_is_not_governed(db):
    """Never a zero. "Complete" and "never had agreed intent to be complete against" are
    different facts, and reporting the second as the first is the misleading green this
    whole PRD exists to stop."""
    prd = prd_svc.create_prd(db, title="Draft", project_id="core", body=BODY)
    out = prd_svc.completeness(db, prd)

    assert out["governed"] is False and out["sections"] == [] and out["absent"] == []


def test_no_percentage_is_reported(db, approved):
    """Deliberate. PRD-12: the pass "must never render that as 'PRD complete'", and a
    ratio over sections would weight a one-line section equally with a ten-bullet one."""
    out = prd_svc.completeness(db, approved)
    assert not [k for k in out if "percent" in k or "ratio" in k]


def test_completeness_is_readable_over_the_api(client, auth, db, approved):
    _link(db, approved, "Baseline")
    out = client.get(f"/api/prds/{approved.id}/completeness", headers=auth).json()

    assert out["governed"] is True
    assert sorted(out["absent"]) == ["Close report", "Judging"]
