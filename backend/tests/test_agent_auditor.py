"""The agent auditor's brief and its verdicts (GRPH-252 / PRD-12).

The auditor is the only component that can reach actual code, which is why **completeness
authority lives there** — and why it needs no provider key on the instance: it brings its
own model. Graphban's job is the handover and the shape of what comes back, not the
judgement.

Two things these tests mostly pin:

- **The authority split travels in the payload**, not in convention. Drift is the
  platform's finding (it watched the timeline); completeness is the auditor's question
  (it has the repo). An auditor handed a conclusion where it should have been handed a
  question rubber-stamps the platform's own guess.
- **A verdict names the intent element it is about.** One verdict per PRD cannot say which
  part was read, so an auditor that covered three sections of fourteen would be
  indistinguishable from one that covered all of them.
"""
import pytest

from app.models import CodeNode
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


@pytest.fixture()
def approved(db):
    prd = prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY)
    prd_svc.record_grill_turns(db, prd.id, [{"role": "user", "text": "An answer."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    db.add(CodeNode(id="cn_a", project_id="core", path="app/services/prds.py", kind="file"))
    db.commit()
    return prd


def _delivered(db, prd, section, **kw):
    item = items_svc.create_item(db, title=f"Work on {section}", project_id="core",
                                 prd_id=prd.id, prd_section=section, **kw)
    attest.complete(db, item.id,
                          evidence=[{"kind": "test", "detail": "42 passed"}])
    db.refresh(item)
    return item


def _section(brief, title):
    return [s for s in brief["sections"] if s["section"] == title][0]


CITE = [{"kind": "code", "ref": "app/services/prds.py"}]


# ---- the brief ---------------------------------------------------------------------------
def test_the_brief_carries_the_intent_itself_not_just_its_title(db, approved):
    """An auditor asked "was this delivered" without the text of what was promised is
    being asked to guess."""
    brief = prd_svc.audit_brief(db, approved)

    assert _section(brief, "Judging")["intent"] == \
        "Classify each completed item against the goal."


def test_the_brief_states_what_is_outstanding_as_the_auditors_question(db, approved):
    _delivered(db, approved, "Baseline")

    brief = prd_svc.audit_brief(db, approved)
    assert brief["outstanding"] == ["Judging"]


def test_the_brief_labels_drift_as_the_platforms_finding(db, approved):
    """The authority split, carried in the payload rather than left to convention: the
    platform watched the timeline, the auditor sees only the end state."""
    brief = prd_svc.audit_brief(db, approved)

    assert "authoritative on completeness" in brief["platform_findings"]["authority"]
    assert "drift" in brief["platform_findings"]


def test_the_brief_carries_the_annotated_corpus_per_item(db, approved):
    """"Consumes the platform judge's annotated corpus" — the classification travels WITH
    the item, so the auditor can see what the platform thought and disagree with it."""
    item = _delivered(db, approved, "Judging")

    row = _section(prd_svc.audit_brief(db, approved), "Judging")["items"][0]
    assert row["id"] == item.key
    assert row["classification"] in ("serves", "enables", "unrelated", "undecidable",
                                     "unclassified")


def test_the_brief_names_work_the_platform_could_not_judge(db, approved):
    """On a stub instance every classification is `undecidable`. The auditor must be told
    which work arrived unjudged rather than inferring it from silence — otherwise it reads
    an empty annotation as agreement."""
    item = _delivered(db, approved, "Judging")

    assert prd_svc.audit_brief(db, approved)["unjudged_items"] == [item.key]


def test_the_brief_carries_receipts_and_corroboration(db, approved):
    _delivered(db, approved, "Judging", touchpoints=["app/services/prds.py"])

    s = _section(prd_svc.audit_brief(db, approved), "Judging")
    assert s["falsifiable_receipts"] == 1 and s["corroboration"] == "corroborated"


def test_the_brief_shows_verdicts_already_claimed(db, approved):
    """A re-audit that cannot see prior verdicts duplicates or contradicts them without
    noticing it has."""
    prd_svc.record_verdict(db, approved, section="Judging", outcome="not_delivered",
                           citations=CITE, signed_by="agent:first")

    brief = prd_svc.audit_brief(db, approved)
    assert brief["existing_verdicts"] == 1
    assert _section(brief, "Judging")["verdicts"][0]["signed_by"] == "agent:first"


def test_the_brief_ships_no_house_opinion(db, approved):
    """Evidence and open questions only. Shipping a recommendation inside the brief would
    make every auditor agree with us by construction, which is the opposite of an
    independent check."""
    brief = prd_svc.audit_brief(db, approved)

    assert not [k for k in brief if k in ("recommendation", "suggested_outcome",
                                          "verdict", "conclusion")]


def test_a_prd_with_no_baseline_has_nothing_to_audit(db):
    prd = prd_svc.create_prd(db, title="Draft", project_id="core", body=BODY)
    assert prd_svc.audit_brief(db, prd)["governed"] is False


# ---- verdicts name the intent element they judge -------------------------------------------
def test_a_verdict_records_the_section_it_is_about(db, approved):
    v = prd_svc.record_verdict(db, approved, section="Judging", outcome="not_delivered",
                               citations=[{"kind": "intent", "ref": "Judging"}],
                               signed_by="agent:auditor")
    assert v.section == "Judging"


def test_a_verdict_about_a_section_the_baseline_lacks_is_malformed(db, approved):
    """Unfalsifiable in exactly the way a citation to nothing is: there is no intent to
    check it against."""
    with pytest.raises(prd_svc.MalformedVerdict, match="no such section"):
        prd_svc.record_verdict(db, approved, section="Invented", outcome="pass",
                               citations=CITE, signed_by="agent:auditor")


def test_a_prd_level_verdict_is_still_allowed(db, approved):
    """NULL means "about the PRD", not "unknown" — an overall sign-off alongside the
    per-section ones is a legitimate thing to record."""
    v = prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                               signed_by="agent:auditor")
    assert v.section is None


def test_a_renamed_section_can_still_be_verdicted_by_its_current_title(db, approved):
    prd_svc.update_prd(db, approved.id, body=BODY.replace("## Judging", "## Judging work"))

    v = prd_svc.record_verdict(db, approved, section="Judging work", outcome="pass",
                               citations=CITE, signed_by="agent:auditor")
    assert v.section == "Judging work"


# ---- coverage ---------------------------------------------------------------------------------
def test_coverage_names_the_sections_with_no_verdict(db, approved):
    """An audit that verdicts three sections of fourteen is not an audit — and without
    this it is indistinguishable from one that covered everything, because the submission
    succeeded either way."""
    prd_svc.record_verdict(db, approved, section="Judging", outcome="pass",
                           citations=CITE, signed_by="agent:auditor")

    cov = prd_svc.audit_coverage(db, approved)
    assert cov["covered"] == ["Judging"]
    assert cov["uncovered"] == ["Baseline"] and cov["complete"] is False


def test_framing_sections_are_never_demanded_of_the_auditor(db, approved):
    """They describe the work rather than being it. Demanding a verdict on "Problem" would
    train an auditor to emit filler, and filler is what makes a coverage number stop
    meaning anything."""
    cov = prd_svc.audit_coverage(db, approved)

    assert "Problem" not in cov["uncovered"]


def test_coverage_is_complete_only_when_every_demanding_section_has_one(db, approved):
    for title in ("Baseline", "Judging"):
        prd_svc.record_verdict(db, approved, section=title, outcome="pass",
                               citations=CITE, signed_by="agent:auditor")

    assert prd_svc.audit_coverage(db, approved)["complete"] is True


def test_a_prd_level_verdict_does_not_count_as_covering_a_section(db, approved):
    """Otherwise one blanket "looks fine" would report full coverage, which is precisely
    the claim per-section verdicts exist to make impossible."""
    prd_svc.record_verdict(db, approved, outcome="pass", citations=CITE,
                           signed_by="agent:auditor")

    assert prd_svc.audit_coverage(db, approved)["complete"] is False


# ---- api ----------------------------------------------------------------------------------------
def test_the_brief_is_readable_over_the_api(client, auth, db, approved):
    out = client.get(f"/api/prds/{approved.id}/audit-brief", headers=auth).json()
    assert out["governed"] is True and out["outstanding"]

    cov = client.get(f"/api/prds/{approved.id}/audit-coverage", headers=auth).json()
    assert cov["complete"] is False
