"""Effort field validation — negative effort must be refused on both write paths.

**Found by doing it.** `create_item` coaxed effort through `int(effort or 0)` and
`update_item` wrote it raw via `setattr`, so an agent could ships `effort=-5` and
the item would carry it. That value falls below `ADVERSARIAL_EFFORT_THRESHOLD`,
so the adversarial-evidence gate never fires and the item signs off without a
required sabotage receipt. A field that decides whether a gate should fire
must not accept a value with no meaning.
"""
from __future__ import annotations

import pytest

from app.services import items as items_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ---- create_item ----------------------------------------------------------------------


def test_create_item_with_negative_effort_raises_valueerror(db):
    """A negative effort argument to `create_item` raises ValueError."""
    with pytest.raises(ValueError, match="negative effort"):
        items_svc.create_item(db, title="bad effort", project_id="core", effort=-1)


def test_create_item_with_large_negative_effort_raises_valueerror(db):
    """Even a large negative effort is refused."""
    with pytest.raises(ValueError, match="negative effort"):
        items_svc.create_item(db, title="very bad effort", project_id="core", effort=-999)


def test_create_item_with_zero_effort_succeeds(db):
    """Zero effort is legal — it means 'not estimated'."""
    item = items_svc.create_item(db, title="zero effort", project_id="core", effort=0)
    assert item.effort == 0


def test_create_item_with_null_effort_defaults_to_zero(db):
    """None effort falls back to 0 without error."""
    item = items_svc.create_item(db, title="null effort", project_id="core", effort=None)
    assert item.effort == 0


def test_create_item_with_string_effort_succeeds(db):
    """A valid string effort is coerced."""
    item = items_svc.create_item(db, title="string effort", project_id="core", effort="3")
    assert item.effort == 3


# ---- update_item ----------------------------------------------------------------------


def test_update_item_with_negative_effort_raises_valueerror(db):
    """Updating an item's effort to a negative value raises ValueError."""
    item = items_svc.create_item(db, title="good effort", project_id="core", effort=5)
    with pytest.raises(ValueError, match="negative effort"):
        items_svc.update_item(db, item.id, effort=-1)


def test_update_item_with_string_negative_effort_raises_valueerror(db):
    """A string that converts to negative effort is also refused."""
    item = items_svc.create_item(db, title="good effort", project_id="core", effort=5)
    with pytest.raises(ValueError, match="negative effort"):
        items_svc.update_item(db, item.id, effort="-1")


def test_update_item_with_zero_effort_succeeds(db):
    """Zero effort is legal on update — it means 'not estimated'."""
    item = items_svc.create_item(db, title="good effort", project_id="core", effort=5)
    items_svc.update_item(db, item.id, effort=0)
    db.refresh(item)
    assert item.effort == 0


def test_update_item_with_null_effort_is_a_noop(db):
    """Passing None does not change the effort (it is equivalent to not providing the field)."""
    item = items_svc.create_item(db, title="good effort", project_id="core", effort=5)
    items_svc.update_item(db, item.id, effort=None)
    db.refresh(item)
    assert item.effort == 5
