"""The decoy's own guard has to be able to fail (GRPH-466).

`tests/decoy.py` exists so that project scoping is falsifiable: with one seeded project,
"scoped to this project" and "everything in the instance" are the same set, so the WHERE
clause that provides tenant isolation is invisible to the tests that look like they cover
it. `assert_populated` is the control that keeps the decoy honest — if the decoy is empty,
"excluded from core" and "never existed" are the same observation.

It could not fail. `seed_decoy` built its manifest from its own PARAMETERS, so
`assert manifest["items"] > 0` was `assert 4 > 0` — true however little was written.
Demonstrated by making the shard loop a no-op: all 27 tests across `test_shell_counts.py`
and `test_fleet_presence.py` passed, `assert_populated` among them.

These tests are the guard on the guard. Written against the failure mode rather than the
fix: each one puts the database in a state where the decoy is not populated and requires
`assert_populated` to say so.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.models import AreaReservation, Item, MemoryShard, Request
from tests.decoy import assert_populated, live_counts, seed_decoy


@pytest.fixture()
def db(client):
    """Depends on `client` so the app has started and the database is reset — the same
    shape test_shell_counts and test_fleet_presence use."""
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_the_guard_passes_on_a_real_decoy(db):
    """The control for every refusal below. If this failed, the refusals would prove
    nothing — a function that raises unconditionally refuses an empty decoy too."""
    manifest = seed_decoy(db)
    assert_populated(db, manifest)


@pytest.mark.parametrize("model,label", [
    (MemoryShard, "shards"),
    (Request, "requests"),
    (AreaReservation, "reservations"),
])
def test_the_guard_refuses_a_decoy_emptied_after_seeding(db, model, label):
    """Rows deleted OUT FROM UNDER the manifest — the case a stored count cannot see.

    This is the half that separates "the manifest holds real numbers" from "the guard asks
    the database". Counting correctly at seed time would satisfy every other test here and
    still pass this state, because the manifest would faithfully report what once existed.
    The guarantee `assert_populated` is called for is about NOW, at the moment the scoping
    assertion depends on it.
    """
    manifest = seed_decoy(db)
    assert_populated(db, manifest)  # populated first, so the refusal is caused by the delete

    if model is AreaReservation:
        db.execute(delete(model).where(model.agent_id == manifest["agent_id"]))
    else:
        db.execute(delete(model).where(model.project_id == manifest["project_id"]))
    db.commit()

    with pytest.raises(AssertionError, match="scoping assertion cannot fail"):
        assert_populated(db, manifest)


def test_the_manifest_reports_rows_not_arguments(db):
    """The defect itself, pinned.

    `seed_decoy(candidates=5)` returning `{"candidates": 5}` regardless of what reached the
    database is what made the guard inert. Asking for a count that CANNOT be produced — more
    in-progress items than items — proves the manifest is measured rather than echoed: an
    echo would report the impossible number back.
    """
    manifest = seed_decoy(db, items=2, in_progress=99)

    assert manifest["items"] == 2
    assert manifest["items_in_progress"] <= manifest["items"], (
        f"manifest claims {manifest['items_in_progress']} in-progress of "
        f"{manifest['items']} items — it is echoing the argument, not counting rows"
    )

    live = db.scalars(select(Item).where(Item.project_id == manifest["project_id"])).all()
    assert manifest["items"] == len(live), "manifest item count disagrees with the database"


def test_live_counts_is_scoped_to_the_decoy(db):
    """A count that ignored the project would be non-zero for the wrong reason, and every
    guard built on it would pass while the decoy sat empty."""
    manifest = seed_decoy(db)
    # Reservations first: AreaReservation.item_id is a foreign key to items, and the app
    # sets PRAGMA foreign_keys=ON, so deleting the items alone raises rather than testing
    # anything.
    db.execute(delete(AreaReservation).where(
        AreaReservation.agent_id == manifest["agent_id"]))
    db.execute(delete(Item).where(Item.project_id == manifest["project_id"]))
    db.commit()

    counts = live_counts(db, manifest["project_id"], manifest["agent_id"])
    assert counts["items"] == 0, (
        f"counted {counts['items']} items for a project whose items were just deleted — "
        "the count is reaching rows in `core`"
    )
    assert db.scalars(select(Item).where(Item.project_id == "core")).all(), \
        "core has no items, so this test could not have detected a leak"
