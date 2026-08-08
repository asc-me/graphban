"""What delivered work offers as proof (GRPH-250 / PRD-12).

Two independent signals, kept apart because they fail differently.

**Receipts**, split by whether anyone but their author could check them. PRD-12: a free-text
`note` is *"as easy to fabricate as a description"*, while a `test`, `health` result or
`url` can be re-run or re-fetched. The split IS the finding — neither half is filtered out.

**Structural corroboration**, which needs no model and no author cooperation: did code
actually appear where the item said it would? *"Not proof, but not self-attestation
either, and both halves already exist."*

The tests below mostly pin the three refusals — no score, no silent pass for an item that
claimed nothing, and no collapsing of the sections a reader most needs to see.
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
    return prd


def _node(db, path):
    db.add(CodeNode(id=f"cn_{abs(hash(path))}", project_id="core", path=path, kind="file"))
    db.commit()


def _delivered(db, prd, section, *, evidence=None, touchpoints=None, title="Work"):
    item = items_svc.create_item(db, title=title, project_id="core", prd_id=prd.id,
                                 prd_section=section, touchpoints=touchpoints or [])
    items_svc.update_item(db, item.id, status="done", evidence=evidence or [])
    db.refresh(item)
    return item


def _section(out, title):
    return [s for s in out["sections"] if s["section"] == title][0]


# ---- the falsifiable / unfalsifiable split -----------------------------------------------
def test_receipts_are_split_by_whether_anyone_else_can_check_them(db, approved):
    _delivered(db, approved, "Judging", evidence=[
        {"kind": "test", "detail": "42 passed"},
        {"kind": "note", "detail": "looks right to me"},
        {"kind": "url", "detail": "PR", "url": "http://x"},
    ])
    s = _section(prd_svc.evidence_rollup(db, approved), "Judging")

    assert s["falsifiable"] == 2 and s["unfalsifiable"] == 1
    assert s["receipts"] == {"test": 1, "note": 1, "url": 1}


def test_work_supported_only_by_a_note_is_named(db, approved):
    """The case a reader most needs. A note is as easy to fabricate as the description it
    sits next to, so delivered work resting on nothing else is the finding — and it is
    exactly what a single total would hide."""
    _delivered(db, approved, "Judging", evidence=[{"kind": "note", "detail": "did it"}])
    _delivered(db, approved, "Baseline", evidence=[{"kind": "test", "detail": "42 passed"}])

    out = prd_svc.evidence_rollup(db, approved)
    assert out["unsupported"] == ["Judging"]


def test_a_screenshot_counts_as_unfalsifiable(db, approved):
    """PRD-12 names test/health/url as falsifiable and `note` as not; it does not name
    `screenshot`. Classified here as unfalsifiable — nothing can re-run or re-fetch it, so
    it is exactly as easy to produce as a note claiming the same thing."""
    _delivered(db, approved, "Judging", evidence=[{"kind": "screenshot", "detail": "ui"}])
    s = _section(prd_svc.evidence_rollup(db, approved), "Judging")

    assert (s["falsifiable"], s["unfalsifiable"]) == (0, 1)


def test_a_section_with_nothing_delivered_is_not_unsupported(db, approved):
    """Absence is the completeness pass's finding, not this one. Reporting it here too
    would double-count the same fact under two names."""
    out = prd_svc.evidence_rollup(db, approved)
    assert out["unsupported"] == []


def test_evidence_on_unfinished_work_is_not_counted(db, approved):
    """A receipt attached to an item still in progress is a plan, not proof."""
    item = items_svc.create_item(db, title="Ongoing", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    items_svc.update_item(db, item.id, status="in_progress",
                          evidence=[{"kind": "test", "detail": "42 passed"}])

    assert _section(prd_svc.evidence_rollup(db, approved), "Judging")["falsifiable"] == 0


# ---- structural corroboration --------------------------------------------------------------
def test_a_touchpoint_backed_by_the_code_graph_corroborates(db, approved):
    _node(db, "app/services/prds.py")
    _delivered(db, approved, "Judging", touchpoints=["app/services/prds.py"],
               evidence=[{"kind": "test", "detail": "42 passed"}])

    s = _section(prd_svc.evidence_rollup(db, approved), "Judging")
    assert s["corroboration"] == "corroborated"


def test_a_touchpoint_with_no_code_behind_it_is_reported(db, approved):
    """The signal that needs no author cooperation: the item said code would appear here
    and the graph says it did not."""
    item = _delivered(db, approved, "Judging", touchpoints=["app/services/ghost.py"])

    out = prd_svc.evidence_rollup(db, approved)
    assert _section(out, "Judging")["corroboration"] == "partial"
    assert out["uncorroborated"] == [f"{item.key} → app/services/ghost.py"]


def test_an_item_that_claimed_nothing_is_unknown_not_uncorroborated(db, approved):
    """Treating "made no claim" as "claim unmet" punishes honesty; treating it as met
    rewards saying nothing. Neither — it is a third answer."""
    _delivered(db, approved, "Judging", evidence=[{"kind": "test", "detail": "ok"}])

    out = prd_svc.evidence_rollup(db, approved)
    assert _section(out, "Judging")["corroboration"] == "unknown"
    assert out["uncorroborated"] == []


def test_a_symbol_node_satisfies_the_file_that_contains_it(db, approved):
    _node(db, "app/services/prds.py::completeness")
    _delivered(db, approved, "Judging", touchpoints=["app/services/prds.py"])

    assert _section(prd_svc.evidence_rollup(db, approved), "Judging")["corroboration"] == "corroborated"


def test_a_sibling_file_in_the_same_directory_does_not_corroborate(db, approved):
    """`clustering._match` relates two touchpoints that merely share a directory, which is
    right for "are these items related" and wrong here. A neighbouring file appearing is
    not evidence that the code an item promised was written."""
    _node(db, "app/services/memory.py")
    _delivered(db, approved, "Judging", touchpoints=["app/services/ghost.py"])

    assert _section(prd_svc.evidence_rollup(db, approved), "Judging")["corroboration"] == "partial"


def test_a_glob_touchpoint_matches_what_it_globs(db, approved):
    _node(db, "web/src/features/prds/IntentDiff.tsx")
    _delivered(db, approved, "Judging", touchpoints=["web/src/features/prds/*"])

    assert _section(prd_svc.evidence_rollup(db, approved), "Judging")["corroboration"] == "corroborated"


# ---- contract ------------------------------------------------------------------------------
def test_it_measures_against_the_baseline_not_the_living_body(db, approved):
    _delivered(db, approved, "Judging", evidence=[{"kind": "test", "detail": "ok"}])
    prd_svc.update_prd(db, approved.id, body=BODY + "\n## Invented later\n\nSurprise.\n")

    titles = [s["section"] for s in prd_svc.evidence_rollup(db, approved)["sections"]]
    assert "Invented later" not in titles and "Judging" in titles


def test_work_filed_under_a_renamed_section_still_counts(db, approved):
    _delivered(db, approved, "Judging", evidence=[{"kind": "test", "detail": "ok"}])
    prd_svc.update_prd(db, approved.id, body=BODY.replace("## Judging", "## Judging work"))

    assert _section(prd_svc.evidence_rollup(db, approved), "Judging")["falsifiable"] == 1


def test_a_prd_with_no_baseline_is_not_governed(db):
    prd = prd_svc.create_prd(db, title="Draft", project_id="core", body=BODY)
    assert prd_svc.evidence_rollup(db, prd)["governed"] is False


def test_no_score_is_reported(db, approved):
    """A weighted number here would be an opinion wearing a measurement's clothes, which
    PRD-12 forbids in as many words."""
    out = prd_svc.evidence_rollup(db, approved)
    assert not [k for k in out if k in ("score", "percent", "confidence", "rating")]


def test_the_rollup_is_readable_over_the_api(client, auth, db, approved):
    _delivered(db, approved, "Judging", evidence=[{"kind": "note", "detail": "trust me"}])
    out = client.get(f"/api/prds/{approved.id}/evidence", headers=auth).json()

    assert out["governed"] is True and out["unsupported"] == ["Judging"]
