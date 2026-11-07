"""GRPH-1395. The branch-pointer guard names the item's real status and only refuses
while a reviewer holds the item."""
from __future__ import annotations

import pytest

from app.services import items as items_svc


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _make(db, status, branch="gb/old", holder=None):
    it = items_svc.create_item(db, title=f"retarget {status}", status="next")
    row = items_svc.get_item(db, it.id if hasattr(it, "id") else it["id"])
    row.status = status
    row.branch = branch
    row.review_claimed_by = holder
    db.commit()
    return row


def test_in_progress_item_can_move_to_review_on_a_new_branch(db):
    """Sabotage: revert to `item.status == "review"` — the move into review trips it."""
    row = _make(db, "in_progress")
    items_svc.update_item(db, row.id, status="review", branch="gb/finished",
                          evidence=[{"kind": "url", "url": "https://example.test/pr/1"}])
    db.refresh(row)
    assert row.branch == "gb/finished"
    assert row.status == "review"


def test_unheld_review_item_can_be_retargeted(db):
    row = _make(db, "review")
    items_svc.update_item(db, row.id, branch="gb/new")
    db.refresh(row)
    assert row.branch == "gb/new"


def test_held_review_item_refuses_and_states_status(db):
    row = _make(db, "review", holder="GRPH-A-reviewer")
    with pytest.raises(ValueError, match="review on `gb/old`"):
        items_svc.update_item(db, row.id, branch="gb/new")
    db.refresh(row)
    assert row.branch == "gb/old"
