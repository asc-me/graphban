"""In-flight work invalidation (GRPH-242 + GRPH-312 / PRD-12).

A rebaseline lands while other agents hold claims. The requesting agent knows intent
moved; the others do not, and keep building against superseded intent — their output then
lands as drift through no fault of their own.

PRD-12 wants the notice **pull-based**, *"so no push channel can fail and the agent cannot
miss it."* GRPH-312 is the correction that the pull was hung off the wrong call: an agent
can complete an item without ever writing a claim update, so a notice delivered only on
the claim path is a notice that whole class of agent never sees.

Two answers here, and both are needed:

- the hold is **derived**, not stored, and rides on the item itself — so it appears on
  every read there is, and there is nothing to acknowledge away;
- completion **stamps** the mismatch onto the item's evidence, so it survives an agent
  that never looked.

These two items were built together deliberately. GRPH-312 is a correction to GRPH-242's
mechanism; shipping either alone means shipping the hole or shipping nothing.
"""
import pytest

from app.services import items as items_svc
from app.services import prds as prd_svc
from tests import attest

BODY = (
    "# Spec\n\n"
    "## Problem\n\nNothing checks delivery.\n\n"
    "## Baseline\n\nFreeze the spec at approval.\n\n"
    "## Judging\n\nClassify each completed item against the goal.\n"
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


def _rebaseline(db, prd, body, reason="Judging was wrong."):
    prd_svc.request_rebaseline(db, prd, reason_type="correction", reason=reason,
                               requested_by="agent:one")
    prd_svc.update_prd(db, prd.id, body=body)
    return _approve(db, prd)


def _claimed(db, prd, section="Judging"):
    item = items_svc.create_item(db, title="Work", project_id="core",
                                 prd_id=prd.id, prd_section=section)
    items_svc.claim_item(db, item.id, "agent:two")
    db.refresh(item)
    return item


# ---- the stamp ------------------------------------------------------------------------
def test_claiming_records_the_baseline_work_started_against(db, approved):
    item = _claimed(db, approved)
    assert item.baseline_at_claim == "v1.0"


def test_moving_to_in_progress_without_a_claim_records_it_too(db, approved):
    """Work reaches `in_progress` without a lease all the time — a human moving a card, an
    agent that edits rather than claims. Stamping only on the claim path would leave the
    hold covering a subset of work and silently missing the rest."""
    item = items_svc.create_item(db, title="Work", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    items_svc.update_item(db, item.id, status="in_progress")
    db.refresh(item)

    assert item.baseline_at_claim == "v1.0"


def test_the_stamp_is_never_overwritten(db, approved):
    """It answers "what was agreed when work began". Restamping on a later claim would
    erase exactly the mismatch the hold is derived from."""
    item = _claimed(db, approved)
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))
    items_svc.release_item(db, item.id, "agent:two")
    items_svc.claim_item(db, item.id, "agent:three")
    db.refresh(item)

    assert item.baseline_at_claim == "v1.0"


def test_work_with_no_prd_is_never_stamped(db):
    item = items_svc.create_item(db, title="Loose work", project_id="core")
    items_svc.claim_item(db, item.id, "agent:two")
    db.refresh(item)

    assert item.baseline_at_claim is None


# ---- the derived hold -----------------------------------------------------------------
def test_a_rebaseline_puts_in_flight_work_on_hold(db, approved):
    item = _claimed(db, approved)
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))
    hold = prd_svc.intent_hold(db, item)

    assert hold["started_against"] == "v1.0" and hold["baseline_version"] == "v1.1"
    assert hold["reason_type"] == "correction" and "Judging was wrong" in hold["reason"]


def test_the_hold_names_which_sections_moved(db, approved):
    """So the holder can tell "my section changed" from "something else in the PRD
    changed" without re-reading the whole spec — the difference between stopping work and
    carrying on."""
    item = _claimed(db, approved, section="Baseline")
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))
    hold = prd_svc.intent_hold(db, item)

    assert hold["sections_changed"] == ["Judging"]
    assert hold["section_affected"] is False


def test_the_hold_flags_work_whose_own_section_moved(db, approved):
    item = _claimed(db, approved, section="Judging")
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))

    assert prd_svc.intent_hold(db, item)["section_affected"] is True


def test_there_is_no_hold_without_a_rebaseline(db, approved):
    assert prd_svc.intent_hold(db, _claimed(db, approved)) is None


def test_a_rename_alone_does_not_hold_work(db, approved):
    """Consistent with drift: a retitled section moved a label, not intent. Holding work
    for it would train agents to ignore the notice, which is the failure mode that makes
    the whole mechanism worthless."""
    item = _claimed(db, approved)
    _rebaseline(db, approved, BODY.replace("## Judging", "## Judging work"))
    hold = prd_svc.intent_hold(db, item)

    assert hold is not None  # the baseline did move
    assert hold["sections_changed"] == [] and hold["section_affected"] is False


def test_work_started_before_the_stamp_existed_makes_no_claim(db, approved):
    """NULL means "we do not know what this targeted". Assuming it targeted the current
    baseline would invent the very fact in doubt, in the record delivery acceptance is
    meant to check."""
    item = _claimed(db, approved)
    item.baseline_at_claim = None
    db.commit()
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))

    assert prd_svc.intent_hold(db, item) is None


# ---- GRPH-312: work that never took a lease -------------------------------------------
def _unclaimed_in_progress(db, prd, section="Judging"):
    """In flight, but with no lease — a human moving a card, or an agent that edits rather
    than claims. This is the population a claim-path notice never reaches."""
    item = items_svc.create_item(db, title="No lease", project_id="core",
                                 prd_id=prd.id, prd_section=section)
    items_svc.update_item(db, item.id, status="in_progress")
    db.refresh(item)
    assert item.claimed_by is None, "fixture is inert if this took a lease"
    return item


def test_work_that_never_took_a_lease_is_still_held(db, approved):
    """The GRPH-312 hole in its purest form. Gating the hold on `claimed_by` would leave
    every unleased item building against superseded intent with nothing to tell it — and
    every test that claims first would still pass."""
    item = _unclaimed_in_progress(db, approved)
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))

    assert prd_svc.intent_hold(db, item)["baseline_version"] == "v1.1"


def test_an_unleased_completion_is_still_stamped(db, approved):
    item = _unclaimed_in_progress(db, approved)
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))
    attest.complete(db, item.id)
    db.refresh(item)

    assert any("superseded intent" in e["detail"] for e in item.evidence)


def test_work_still_in_the_backlog_is_held_too(db, approved):
    """An item linked and stamped but not yet started. It has agreed intent behind it and
    that intent moved; saying nothing until someone picks it up delivers the notice at the
    one moment it is least likely to be acted on."""
    item = items_svc.create_item(db, title="Not started", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    items_svc.update_item(db, item.id, status="in_progress")
    items_svc.update_item(db, item.id, status="backlog")
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))
    db.refresh(item)

    assert prd_svc.intent_hold(db, item) is not None


# ---- GRPH-312: the agent that never reads ---------------------------------------------
def test_completing_against_superseded_intent_is_recorded_on_the_item(db, approved):
    """THE GRPH-312 test. An agent can complete without ever reading the hold; if nothing
    records the mismatch, the resulting drift is blamed on delivery rather than on the
    invalidation nobody saw. The receipt is what survives the agent walking away."""
    item = _claimed(db, approved)
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))

    attest.complete(db, item.id)
    db.refresh(item)

    notes = [e["detail"] for e in item.evidence]
    assert any("superseded intent" in n and "v1.0" in n and "v1.1" in n for n in notes)


def test_an_ordinary_completion_is_not_annotated(db, approved):
    """Otherwise the note appears on everything and stops meaning anything."""
    item = _claimed(db, approved)
    attest.complete(db, item.id)
    db.refresh(item)

    assert not any("superseded intent" in e["detail"] for e in item.evidence)


def test_the_hold_clears_once_the_work_is_done(db, approved):
    """The hold is about work IN FLIGHT. Once complete, the permanent record is the
    receipt on the item, not a live warning that can never be resolved."""
    item = _claimed(db, approved)
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))
    attest.complete(db, item.id)
    db.refresh(item)

    assert prd_svc.intent_hold(db, item) is None


# ---- delivery ---------------------------------------------------------------------------
def test_the_hold_rides_on_every_item_an_agent_reads(db, approved):
    """Delivered from `_item_dict`, so claim, heartbeat, update and search all carry it.
    GRPH-312's hole was a notice hung off one call path; hanging it off the item itself is
    what closes it, because there is no way to work on an item without reading one."""
    from app.mcp_server import _item_dict

    item = _claimed(db, approved)
    assert "intent_hold" not in _item_dict(item)

    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))
    db.refresh(item)

    assert _item_dict(item)["intent_hold"]["baseline_version"] == "v1.1"


def test_the_prd_can_list_who_needs_telling(db, approved):
    """The other direction: `claimed_by` plus the lease give the list, which is why no
    subscription table is needed."""
    held = _claimed(db, approved)
    settled = items_svc.create_item(db, title="Already done", project_id="core",
                                    prd_id=approved.id, prd_section="Baseline")
    attest.complete(db, settled.id)
    _rebaseline(db, approved, BODY.replace("Classify each completed item against the goal.",
                                           "Rewritten."))

    holders = prd_svc.held_claims(db, approved)
    assert [h["id"] for h in holders] == [held.key]
    assert holders[0]["claimed_by"] == "agent:two"
