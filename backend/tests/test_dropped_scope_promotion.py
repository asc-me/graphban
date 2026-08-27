"""Dropped-scope promotion and PRD lineage (GRPH-246 / PRD-12).

PRD-12: *"A dropped intent element in the close report can be promoted directly into a
backlog item or a successor PRD. Since post-close changes must become a new PRD, the
successor carries a lineage link back to the closed one — keeping the chain from original
intent, through what was dropped, to what came next walkable."*

The load-bearing guard is that you can only promote intent that genuinely has nothing
delivered. Promoting delivered work would manufacture duplicates and, worse, write a
lineage record asserting something was dropped when it shipped — corrupting the exact
artifact this feature exists to make trustworthy.
"""
import pytest

from app.services import items as items_svc
from app.services import prds as prd_svc
from tests import attest

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
    prd_svc.record_grill_turns(db, prd.id, [{"role": "user", "text": "An answer."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    return prd


@pytest.fixture()
def approved(db):
    """One section delivered, the rest dropped — the shape promotion exists for."""
    prd = _approve(db, prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY))
    item = items_svc.create_item(db, title="Built it", project_id="core",
                                 prd_id=prd.id, prd_section="Baseline")
    attest.complete(db, item.id)
    return prd


# ---- what may be promoted -------------------------------------------------------------
def test_dropped_intent_lists_baselined_sections_with_nothing_delivered(db, approved):
    assert prd_svc.dropped_intent(db, approved) == ["Close report", "Judging"]


def test_a_section_deleted_from_the_body_is_still_promotable(db, approved):
    """The case this exists for: intent was agreed, work never happened, and the heading
    quietly disappeared. Reading the living body would lose it entirely."""
    prd_svc.update_prd(db, approved.id, body=BODY.split("## Judging")[0])

    assert "Judging" in prd_svc.dropped_intent(db, approved)


def test_promoting_delivered_work_is_refused(db, approved):
    """The guard the whole feature rests on. A lineage record saying "Baseline was
    dropped" when it shipped is worse than no lineage at all."""
    with pytest.raises(prd_svc.NothingDropped, match="Baseline"):
        prd_svc.promote_to_item(db, approved, "Baseline")


def test_promoting_a_section_the_baseline_never_had_is_refused(db, approved):
    with pytest.raises(ValueError, match="not in the governing baseline"):
        prd_svc.promote_to_prd(db, approved, ["Invented"])


def test_a_prd_with_no_baseline_has_nothing_to_promote(db):
    """No agreed intent means nothing can have been dropped from it."""
    prd = prd_svc.create_prd(db, title="Draft", project_id="core", body=BODY)

    assert prd_svc.dropped_intent(db, prd) == []
    with pytest.raises(ValueError, match="no baseline"):
        prd_svc.promote_to_item(db, prd, "Judging")


# ---- promotion to an item -------------------------------------------------------------
def test_promoting_to_an_item_carries_the_baselines_text(db, approved):
    """Seeded from the baseline, not the living body — the point is to carry forward what
    was AGREED, and the body may not contain the section any more."""
    prd_svc.update_prd(db, approved.id, body=BODY.split("## Judging")[0])
    item = prd_svc.promote_to_item(db, approved, "Judging")

    assert item.status == "backlog" and item.prd_section == "Judging"
    assert "Classify each completed item against the goal." in item.description
    assert approved.key in item.description


def test_after_promotion_the_section_reads_as_planned_not_absent(db, approved):
    """The honest new state. It has not shipped, but it is no longer unaccounted for, and
    collapsing those two would make the promotion invisible."""
    assert "Judging" in prd_svc.completeness(db, approved)["absent"]

    prd_svc.promote_to_item(db, approved, "Judging")
    out = prd_svc.completeness(db, approved)

    assert "Judging" not in out["absent"] and "Judging" in out["undelivered"]


# ---- promotion to a successor PRD -----------------------------------------------------
def test_a_successor_carries_a_lineage_link_and_the_sections_it_inherited(db, approved):
    successor = prd_svc.promote_to_prd(db, approved, ["Judging", "Close report"])

    assert successor.supersedes_prd_id == approved.id
    assert successor.promoted_sections == ["Judging", "Close report"]
    assert "## Judging" in successor.body and "## Close report" in successor.body


def test_a_successor_starts_as_a_draft_and_must_earn_its_own_approval(db, approved):
    """Inheriting approval would let a rebaseline that cannot add sections launder them in
    through a successor instead — the same scope growth GRPH-318 refuses, one hop away."""
    successor = prd_svc.promote_to_prd(db, approved, ["Judging"])

    assert successor.status == "draft"
    assert prd_svc.baseline_of(db, successor.id) is None


def test_the_successor_inherits_the_baselines_words_not_the_drifted_body(db, approved):
    prd_svc.update_prd(db, approved.id,
                       body=BODY.replace("Classify each completed item against the goal.",
                                         "Rewritten after approval."))
    successor = prd_svc.promote_to_prd(db, approved, ["Judging"])

    assert "Classify each completed item against the goal." in successor.body
    assert "Rewritten after approval." not in successor.body


def test_promotion_does_not_alter_the_predecessors_baseline(db, approved):
    """Promotion records where intent went; it never rewrites what was agreed. If it did,
    drift against the original would quietly shrink every time scope was moved out."""
    before = prd_svc.baseline_of(db, approved.id)
    body, version = before.body, before.version

    prd_svc.promote_to_prd(db, approved, ["Judging"])
    after = prd_svc.baseline_of(db, approved.id)

    assert (after.body, after.version) == (body, version)
    assert "Judging" in prd_svc.dropped_intent(db, approved)


# ---- the chain is walkable ------------------------------------------------------------
def test_lineage_walks_both_directions(db, approved):
    successor = prd_svc.promote_to_prd(db, approved, ["Judging"])

    forward = prd_svc.lineage(db, approved)
    assert [s["id"] for s in forward["successors"]] == [successor.key]
    assert forward["successors"][0]["promoted_sections"] == ["Judging"]

    back = prd_svc.lineage(db, successor)
    assert [a["id"] for a in back["ancestors"]] == [approved.key]
    assert back["ancestors"][0]["promoted_sections"] == ["Judging"]


def test_lineage_walks_a_chain_more_than_one_hop_deep(db, approved):
    """"Walkable" means the whole chain, not just the nearest link — otherwise intent that
    moved twice is untraceable to where it started."""
    second = prd_svc.promote_to_prd(db, approved, ["Judging"])
    _approve(db, second)
    third = prd_svc.promote_to_prd(db, second, ["Judging"])

    assert [a["id"] for a in prd_svc.lineage(db, third)["ancestors"]] == [second.key,
                                                                         approved.key]


def test_a_lineage_cycle_terminates_instead_of_hanging(db, approved):
    """Only reachable through an import or a hand-edited row. A server that hangs on one
    is a worse failure than a truncated chain, so the walk is guarded rather than trusting
    the data to be acyclic."""
    successor = prd_svc.promote_to_prd(db, approved, ["Judging"])
    approved.supersedes_prd_id = successor.id  # the cycle
    db.commit()

    assert [a["id"] for a in prd_svc.lineage(db, approved)["ancestors"]] == [successor.key]


# ---- api ------------------------------------------------------------------------------
def test_promotion_is_reachable_over_the_api(client, auth, db, approved):
    out = client.post(f"/api/prds/{approved.id}/promote",
                      json={"sections": ["Judging"], "target": "prd"}, headers=auth)
    assert out.status_code == 200 and out.json()["promoted_sections"] == ["Judging"]

    lin = client.get(f"/api/prds/{approved.id}/lineage", headers=auth).json()
    assert lin["successors"][0]["id"] == out.json()["id"]


def test_the_api_refuses_to_promote_delivered_work(client, auth, db, approved):
    out = client.post(f"/api/prds/{approved.id}/promote",
                      json={"sections": ["Baseline"], "target": "item"}, headers=auth)

    assert out.status_code == 422 and "Baseline" in out.json()["detail"]
