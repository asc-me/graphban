"""GRPH-1830 — the review queue stops re-issuing an item no agent review can move on.

GRPH-1825 sat in `review` on one unchanged head while ~52 reviewer seats were handed it. Every
one wrote NOT YET ("needs a person"), the 8th also set `blocker`, and `claim_review` kept giving
it out. Two holes, both pinned here:

1. an item in `review` with a `blocker` is withheld (`needs_person`), and the reason names it;
2. two independent NOT YET verdicts on one head and one acceptance withhold it
   (`not_yet_unchanged`) until a new head, an acceptance edit, or a person's
   `review requeued` note.
"""
from __future__ import annotations

from app.services import fleet as fleet_svc
from app.services import items as items_svc
from tests.test_delegation import (  # noqa: F401 — fixtures
    _agent, _mcp, _ok, _stored, db, key, proj)
from tests.test_pr_unlinked_review import _reviewer, _to_review

HEAD = "a" * 40
NEW_HEAD = "b" * 40


def _ready(client, key, db) -> tuple[str, str]:
    planner = _agent(client, key, "planner", capabilities={"vendor": "anthropic"})
    item = _to_review(client, key, planner, title="post-release check", branch="gb/p95")
    row = _stored(db, item)
    row.github_url = "https://github.com/o/r/pull/9"
    row.head_commit = HEAD
    row.description = "## Acceptance\n\n- a live check after the release"
    db.commit()
    return planner, item


def _not_yet(client, key, item, reviewer) -> None:
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert got["claimed"] and got["item"]["id"] == item, got
    _ok(_mcp(client, key, "release_item", {
        "id": item, "agent_id": reviewer,
        "reason": "the live clause needs a person after release"}))


def _rules(db, reviewer, proj) -> list[tuple[str, str]]:
    return [(w["item"], w["rule"])
            for w in fleet_svc.review_withheld(db, agent_id=reviewer, project_id=proj)]


def test_a_review_row_with_a_blocker_is_withheld_and_named(client, key, proj, db):
    """Sabotage: drop the `blocker` check in `review_needs_person`. The reviewer is handed it."""
    planner, item = _ready(client, key, db)
    row = _stored(db, item)
    row.blocker = "Needs a person: verify the release on the live box"
    db.commit()

    reviewer = _reviewer(client, key, proj, db, planner, "reviewer")
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert not got["claimed"], got
    withheld = fleet_svc.review_withheld(db, agent_id=reviewer, project_id=proj)
    assert [(w["item"], w["rule"]) for w in withheld] == [
        (item, fleet_svc.WITHHELD_NEEDS_PERSON)]
    assert withheld[0]["reason"].startswith(
        "review_skipped: blocked by a person-only step: Needs a person")
    # The row says so too, so `until` can start no reviewer for it.
    assert items_svc.item_dict(_stored(db, item))["review_needs_person"].startswith(
        "review_skipped: blocked by a person-only step")

    # Clearing it is what releases the row.
    row = _stored(db, item)
    row.blocker = ""
    db.commit()
    assert "review_needs_person" not in items_svc.item_dict(_stored(db, item))
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": reviewer}))
    assert got["claimed"] and got["item"]["id"] == item, got


def test_two_not_yets_on_one_head_park_it_until_the_head_moves(client, key, proj, db):
    """Sabotage: return None from `review_needs_person` past the blocker. The third is handed it."""
    planner, item = _ready(client, key, db)
    r1 = _reviewer(client, key, proj, db, planner, "reviewer-1")
    _not_yet(client, key, item, r1)
    note = _stored(db, item).evidence[-1]["detail"]
    assert note.startswith(f"NOT YET ({r1}): ")
    assert f"[on head {HEAD}, revision " in note

    # One NOT YET is not enough: the second reviewer still gets it.
    r2 = _reviewer(client, key, proj, db, planner, "reviewer-2")
    _not_yet(client, key, item, r2)

    r3 = _reviewer(client, key, proj, db, planner, "reviewer-3")
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": r3}))
    assert not got["claimed"], got
    assert _rules(db, r3, proj) == [(item, fleet_svc.WITHHELD_NOT_YET)]
    why = items_svc.item_dict(_stored(db, item))["review_needs_person"]
    assert "2 reviewers" in why and HEAD[:12] in why and "needs a person" in why

    # A new head is a new thing to review.
    row = _stored(db, item)
    row.head_commit = NEW_HEAD
    db.commit()
    got = _ok(_mcp(client, key, "claim_review", {"agent_id": r3}))
    assert got["claimed"] and got["item"]["id"] == item, got


def test_the_same_reviewer_twice_is_one_verdict(client, key, proj, db):
    planner, item = _ready(client, key, db)
    r1 = _reviewer(client, key, proj, db, planner, "reviewer-1")
    _not_yet(client, key, item, r1)
    _not_yet(client, key, item, r1)
    assert fleet_svc.review_needs_person(_stored(db, item)) is None


def test_an_acceptance_edit_or_a_requeue_note_releases_it(client, key, proj, db):
    planner, item = _ready(client, key, db)
    for n in (1, 2):
        _not_yet(client, key, item, _reviewer(client, key, proj, db, planner, f"r-{n}"))
    r3 = _reviewer(client, key, proj, db, planner, "reviewer-3")
    assert _rules(db, r3, proj) == [(item, fleet_svc.WITHHELD_NOT_YET)]

    # The planner removes the clause nobody could check: reviewed against what it says now.
    _ok(_mcp(client, key, "update_item", {
        "id": item, "description": "## Acceptance\n\n- a backend test"}))
    assert _rules(db, r3, proj) == []

    # Parked again on the new acceptance, then a person puts it back.
    for n in (4, 5):
        _not_yet(client, key, item, _reviewer(client, key, proj, db, planner, f"r-{n}"))
    assert _rules(db, r3, proj) == [(item, fleet_svc.WITHHELD_NOT_YET)]
    row = _stored(db, item)
    row.evidence = items_svc.append_evidence(row.evidence, [
        {"kind": "note", "detail": "review requeued: checked by hand, review the diff"}])
    db.commit()
    assert _rules(db, r3, proj) == []


def test_a_note_from_before_the_fix_names_no_head_and_does_not_count(db, client, key, proj):
    planner, item = _ready(client, key, db)
    row = _stored(db, item)
    row.evidence = items_svc.append_evidence(row.evidence, [
        {"kind": "note", "detail": f"NOT YET (GRPH-A{n}): needs a person"} for n in (1, 2, 3)])
    db.commit()
    assert fleet_svc.review_needs_person(_stored(db, item)) is None
