"""What a close and a verdict are allowed to be (GRPH-311 + GRPH-314 / PRD-12).

Two corrections from the Grok 4.5 critique of the approved v1.0, both of the same shape:
a rule that is right in the case it was written for and wrong everywhere else.

**GRPH-311** — *"if the judge becomes unavailable during a closing, refuse to close."*
Right for a transient outage. But `CHAT_PROVIDER` defaults to `stub`, so on a default
install the judge is *permanently* unavailable and closing becomes permanently impossible:
the rule blocks the instance that most needs to ship.

**GRPH-314** — every citation must resolve to a node in the code graph. But the
completeness pass reports what is MISSING, and missing work has no path and no symbol. So
the output of the component PRD-12 names as authoritative on completeness was
definitionally malformed under the PRD's own validator.
"""
import pytest

from app.models import CodeNode
from app.services import items as items_svc
from app.services import prds as prd_svc
from app.services.platform import Resolved
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
    prd_svc.record_grill_turns(db, prd.id, [{"role": "user", "text": "An answer."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    return prd


@pytest.fixture()
def approved(db):
    return _approve(db, prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY))


# ---- GRPH-311: a default install must still be able to close ---------------------------
def test_with_no_judge_configured_a_close_is_possible_and_labelled_mechanical(db, approved):
    """THE GRPH-311 test. The shipped default is `stub`; if "no judge" blocked closing, the
    out-of-the-box instance could never close a PRD at all."""
    out = prd_svc.close_readiness(db, approved)

    assert out["can_close"] is True
    assert out["mode"] == "mechanical" and out["judge"] == prd_svc.JUDGE_ABSENT


def test_a_mechanical_close_states_what_it_did_not_assess(db, approved):
    """Degrade AND disclose — the pattern the grill already uses for its stub bar. An
    unjudged close wearing a judged label is the same dishonesty as a number that looks
    like a measurement when it is an opinion, pointed the other way."""
    out = prd_svc.close_readiness(db, approved)

    assert "No judge is configured" in out["disclosure"]
    assert "not assessed" in out["disclosure"]


@pytest.fixture()
def with_a_judge(monkeypatch):
    """A project that HAS chosen a judge. The default test instance is `stub`, so without
    this the "configured but down" branch is unreachable and the distinction untested."""
    from app.services import platform as platform_svc

    real = platform_svc.resolve_chat
    monkeypatch.setattr(platform_svc, "resolve_chat",
                        lambda db, pid: Resolved("openai", real(db, pid).chat))


def test_a_configured_judge_that_is_not_answering_still_blocks(db, approved, with_a_judge):
    """The distinction the correction turns on. This is the case the original rule was
    written for: the judged close is merely LATE, and closing mechanically now would
    silently downgrade a verdict someone is expecting."""
    out = prd_svc.close_readiness(db, approved, judge_reachable=False)

    assert out["can_close"] is False and out["judge"] == prd_svc.JUDGE_DOWN
    assert "not answering" in out["blocked_on"]


def test_a_reachable_judge_gives_a_judged_close(db, approved, with_a_judge):
    out = prd_svc.close_readiness(db, approved)

    assert out["mode"] == "judged" and out["disclosure"] is None


def test_a_stub_instance_is_unaffected_by_the_reachability_flag(db, approved):
    """"Nobody chose a judge" can never become "the judge is down" — waiting for a judge
    that was never configured is waiting forever, which is exactly the brick. So the flag
    must not be able to block a stub instance, whatever a caller passes."""
    out = prd_svc.close_readiness(db, approved, judge_reachable=False)

    assert out["can_close"] is True and out["mode"] == "mechanical"
    assert prd_svc.judge_status(db, "core", reachable=False) == prd_svc.JUDGE_ABSENT


def test_a_prd_with_no_baseline_cannot_be_closed_at_all(db):
    """Nothing to close against. Distinct from a judge problem, and reported as such."""
    prd = prd_svc.create_prd(db, title="Draft", project_id="core", body=BODY)
    out = prd_svc.close_readiness(db, prd)

    assert out["can_close"] is False and "no agreed intent" in out["blocked_on"]


# ---- GRPH-314: absence findings must be able to cite -----------------------------------
def test_an_absence_finding_cites_the_intent_it_found_missing(db, approved):
    """THE GRPH-314 test. Missing work has no path and no symbol, so a code-graph-only rule
    made every completeness finding malformed by construction."""
    ok, why = prd_svc.validate_citation(db, approved, {"kind": "intent", "ref": "Judging"})

    assert ok, why


def test_an_intent_citation_must_actually_resolve_in_the_baseline(db, approved):
    """Falsifiable, not merely permitted. If any string were accepted, the second form
    would be a hole rather than a fix."""
    ok, why = prd_svc.validate_citation(db, approved, {"kind": "intent", "ref": "Invented"})

    assert not ok and "no such section" in why


def test_a_renamed_section_can_still_be_cited_by_its_current_title(db, approved):
    """A rename moved a label, not intent. Refusing it would invalidate every absence
    finding beneath a retitled heading."""
    prd_svc.update_prd(db, approved.id, body=BODY.replace("## Judging", "## Judging work"))
    ok, _ = prd_svc.validate_citation(db, approved, {"kind": "intent", "ref": "Judging work"})

    assert ok


def test_non_code_evidence_can_be_cited(db, approved):
    """`Item.evidence` already accepts test/url/screenshot/health/note. A code-graph-only
    rule rejected valid proof and skewed verdicts toward code-shaped work — a docs or
    infrastructure item could never be signed off."""
    item = items_svc.create_item(db, title="Runbook", project_id="core", prd_id=approved.id)
    attest.complete(db, item.id,
                          evidence=[{"kind": "url", "detail": "runbook", "url": "http://x"}])

    ok, why = prd_svc.validate_citation(db, approved, {"kind": "evidence", "ref": item.key})
    assert ok, why


def test_citing_an_item_that_carries_no_evidence_is_refused(db, approved):
    item = items_svc.create_item(db, title="Bare", project_id="core", prd_id=approved.id)

    ok, why = prd_svc.validate_citation(db, approved, {"kind": "evidence", "ref": item.key})
    assert not ok and "no evidence" in why


def test_a_code_citation_still_has_to_resolve(db, approved):
    db.add(CodeNode(id="cn_1", project_id="core", path="app/services/prds.py", kind="file"))
    db.commit()

    assert prd_svc.validate_citation(db, approved,
                                     {"kind": "code", "ref": "app/services/prds.py"})[0]
    ok, why = prd_svc.validate_citation(db, approved, {"kind": "code", "ref": "nope.py"})
    assert not ok and "no such code node" in why


def test_an_unknown_citation_kind_is_refused(db, approved):
    ok, why = prd_svc.validate_citation(db, approved, {"kind": "vibes", "ref": "trust me"})
    assert not ok and "unknown citation kind" in why


def test_a_citation_naming_nothing_is_refused(db, approved):
    ok, why = prd_svc.validate_citation(db, approved, {"kind": "intent", "ref": "  "})
    assert not ok and "names nothing" in why


# ---- the verdict as a whole -------------------------------------------------------------
def test_a_verdict_citing_nothing_is_malformed(db, approved):
    """The load-bearing half. A verdict that points at nothing cannot be argued with, and
    one that cannot be argued with is an assertion wearing evidence's clothes."""
    out = prd_svc.validate_verdict(db, approved, [])

    assert out["ok"] is False and out["problems"] == ["a verdict must cite something"]


def test_one_bad_citation_invalidates_the_verdict_and_says_which(db, approved):
    out = prd_svc.validate_verdict(db, approved, [
        {"kind": "intent", "ref": "Judging"},
        {"kind": "intent", "ref": "Invented"},
    ])

    assert out["ok"] is False and out["checked"] == 2
    assert len(out["problems"]) == 1 and "Invented" in out["problems"][0]


def test_a_verdict_may_mix_citation_forms(db, approved):
    """A real sign-off cites some code, some absence, and some non-code proof. Forcing one
    form would push authors to file whichever the validator happens to accept."""
    db.add(CodeNode(id="cn_2", project_id="core", path="app/services/prds.py", kind="file"))
    db.commit()
    item = items_svc.create_item(db, title="Runbook", project_id="core", prd_id=approved.id)
    attest.complete(db, item.id,
                          evidence=[{"kind": "note", "detail": "verified by hand"}])

    out = prd_svc.validate_verdict(db, approved, [
        {"kind": "code", "ref": "app/services/prds.py"},
        {"kind": "intent", "ref": "Judging"},
        {"kind": "evidence", "ref": item.key},
    ])
    assert out["ok"] is True and out["checked"] == 3
