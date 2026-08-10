"""Sign-off verdicts are claims with provenance, never truth (GRPH-253 / PRD-12).

PRD-12 is blunt about the limit: an agent-side signer **moves** the self-attestation
problem rather than solving it. So the mitigation is falsifiability rather than trust —
a verdict must cite, the citations must resolve to things that exist, and who signed it is
on the record.

Two rules pull in opposite directions and both are deliberate:

- a verdict that cites nothing is **rejected as malformed**, not stored as a failure;
- a verdict signed by someone who also implemented the work is **flagged, never refused**.

The second is the one that would be easy to get wrong by being stricter. On a solo project
the signer and the implementer are the same person, and refusing there means nobody can
ever sign off — a rule that blocks the ordinary case gets routed around within a day.
"""
import pytest

from app.models import CodeNode
from app.services import items as items_svc
from app.services import prds as prd_svc

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


@pytest.fixture()
def approved(db):
    prd = prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY)
    prd_svc.record_grill_turns(db, prd.id, [{"role": "user", "text": "An answer."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    db.add(CodeNode(id="cn_1", project_id="core", path="app/services/prds.py", kind="file"))
    db.commit()
    return prd


CITE = [{"kind": "code", "ref": "app/services/prds.py"}]


def _claimed_item(db, prd, agent, section="Judging"):
    item = items_svc.create_item(db, title="Work", project_id="core",
                                 prd_id=prd.id, prd_section=section)
    items_svc.claim_item(db, item.id, agent)
    db.refresh(item)
    return item


# ---- admissibility ---------------------------------------------------------------------
def test_a_verdict_citing_nothing_is_rejected_as_malformed(db, approved):
    """Not recorded as a failed pass — refused. A verdict that points at nothing cannot be
    argued with, and one that cannot be argued with is not evidence."""
    with pytest.raises(prd_svc.MalformedVerdict, match="must cite"):
        prd_svc.record_verdict(db, approved, outcome="pass", citations=[],
                               signed_by="agent:auditor")

    assert prd_svc.verdicts(db, approved) == []


def test_a_verdict_citing_something_that_does_not_exist_is_rejected(db, approved):
    """The server cannot check whether code is correct. It can check the cited thing
    exists, and a claim that can be checked at all is the achievable upgrade."""
    with pytest.raises(prd_svc.MalformedVerdict, match="no such code node"):
        prd_svc.record_verdict(db, approved, outcome="pass",
                               citations=[{"kind": "code", "ref": "app/nope.py"}],
                               signed_by="agent:auditor")


def test_an_absence_verdict_may_cite_intent_instead_of_code(db, approved):
    """The GRPH-314 form, exercised through the storage path: missing work has no path and
    no symbol, so a completeness verdict cites the section with nothing delivered."""
    v = prd_svc.record_verdict(db, approved, outcome="not_delivered",
                               citations=[{"kind": "intent", "ref": "Judging"}],
                               signed_by="agent:auditor")

    assert v.id and v.citations == [{"kind": "intent", "ref": "Judging"}]


# ---- provenance -------------------------------------------------------------------------
def test_the_signer_and_the_key_behind_the_verdict_are_recorded(db, approved):
    """Two agents can share a display name; a credential cannot be borrowed by accident,
    so the key is the identity that survives a dispute."""
    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:auditor", api_key_id="key_123",
                               reasoning="Everything demanded was delivered.")

    assert (v.signed_by, v.api_key_id) == ("agent:auditor", "key_123")
    assert v.reasoning.startswith("Everything demanded")


def test_the_verdict_is_stamped_with_the_baseline_it_judged(db, approved):
    """A verdict outlives the intent it was made about. Without this, a judgement of v1.0
    silently reads as a judgement of today."""
    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:auditor")
    assert v.baseline_version == "v1.0"


def test_verdicts_are_append_only(db, approved):
    """A later verdict supersedes an earlier one by being later. Nothing overwrites what
    was claimed before — the same rule the baseline chain follows, for the same reason."""
    prd_svc.record_verdict(db, approved, outcome="not_delivered", citations=CITE,
                           signed_by="agent:one")
    prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                           signed_by="agent:two")

    got = prd_svc.verdicts(db, approved)
    assert [v.outcome for v in got] == ["not_delivered", "pass"]


# ---- separation of duties ----------------------------------------------------------------
def test_signing_your_own_work_is_flagged(db, approved):
    """THE rule. Otherwise sign-off is the worker grading their own exam through a second
    door — and the point of an audit is that someone else looked."""
    item = _claimed_item(db, approved, "agent:builder")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:builder")

    assert v.self_signed is True
    assert v.self_signed_items == [item.key]


def test_the_flag_names_the_work_that_triggered_it(db, approved):
    """"Self-signed" with nothing behind it is an accusation, not a finding. A reader has
    to be able to check which items overlapped."""
    a = _claimed_item(db, approved, "agent:builder", section="Judging")
    _claimed_item(db, approved, "agent:other", section="Baseline")
    b = _claimed_item(db, approved, "agent:builder", section="Baseline")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:builder")

    assert v.self_signed_items == sorted([a.key, b.key])


def test_an_independent_signer_is_not_flagged(db, approved):
    _claimed_item(db, approved, "agent:builder")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:auditor")

    assert v.self_signed is False and v.self_signed_items == []


def test_a_self_signed_verdict_is_still_recorded(db, approved):
    """Flagged, never refused. On a solo project the signer and the implementer are the
    same person; refusing there would mean nobody can ever sign off, and a rule that blocks
    the ordinary case is one people route around."""
    _claimed_item(db, approved, "agent:builder")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:builder")

    assert v.id is not None and prd_svc.verdicts(db, approved) == [v]


def test_work_on_another_prd_does_not_trigger_the_flag(db, approved):
    """Overlap has to be with the work UNDER AUDIT. Flagging on any shared history would
    make the signal fire constantly and stop meaning anything — the AL-96 failure."""
    other = prd_svc.create_prd(db, title="Elsewhere", project_id="core", body=BODY)
    _claimed_item(db, other, "agent:builder")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:builder")

    assert v.self_signed is False


def test_an_unclaimed_item_cannot_make_a_verdict_self_signed(db, approved):
    items_svc.create_item(db, title="Nobody claimed this", project_id="core",
                          prd_id=approved.id, prd_section="Judging")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="")
    assert v.self_signed is False


# ---- why it is or is not self-signed (GRPH-327) -------------------------------------------
def test_a_prd_nobody_claimed_reports_unverifiable_not_independent(db, approved):
    """THE finding, from auditing PRD-12 through its own surface: 0 of 27 items carried a
    claimant, so the check compared against an empty set and returned False for 14 verdicts
    signed by the author of the work.

    It did not fail. It had nothing to check, and reported that as a pass. "Nobody recorded
    who built this" and "someone else built this" are opposite claims."""
    items_svc.create_item(db, title="Nobody claimed me", project_id="core",
                          prd_id=approved.id, prd_section="Judging")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:auditor")

    assert v.separation == prd_svc.UNVERIFIABLE
    assert v.self_signed is False, "unverifiable is not an accusation either"


def test_a_claim_by_someone_else_reports_independent(db, approved):
    _claimed_item(db, approved, "agent:builder")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:auditor")
    assert v.separation == prd_svc.INDEPENDENT


def test_signing_your_own_claim_reports_self_signed(db, approved):
    _claimed_item(db, approved, "agent:builder")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:builder")
    assert v.separation == prd_svc.SELF_SIGNED and v.self_signed is True


def test_the_event_log_catches_an_implementer_who_never_claimed(db, approved):
    """The signal `claimed_by` cannot give. Working without a lease is the ORDINARY path
    for a single agent — it was the entire population on PRD-12 — so a check that only
    reads claims is blind exactly where it is most needed."""
    from app.services import events as events_svc

    item = items_svc.create_item(db, title="Built without a lease", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    events_svc.record(db, actor_type="apikey", actor_id="key_abc", actor_label="builder",
                      surface="mcp", action="update_item", target_type="item",
                      target_id=item.id, project_id="core")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:builder", api_key_id="key_abc")

    assert v.separation == prd_svc.SELF_SIGNED
    assert v.self_signed_items == [item.key]


def test_the_key_id_is_what_matches_not_the_display_name(db, approved):
    """Two agents can share a display name; a key id cannot be borrowed by accident, so it
    is the identity that survives a dispute."""
    from app.services import events as events_svc

    item = items_svc.create_item(db, title="Someone else's work", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    events_svc.record(db, actor_type="apikey", actor_id="key_other", actor_label="someone",
                      surface="mcp", action="update_item", target_type="item",
                      target_id=item.id, project_id="core")

    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:auditor", api_key_id="key_mine")
    assert v.separation == prd_svc.INDEPENDENT
