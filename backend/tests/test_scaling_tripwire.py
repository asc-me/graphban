"""A deferred finding that can fire (GRPH-540).

GRPH-55 parks real scaling work behind a precise, measurable condition — *first project over
~5k items* — and **nothing evaluated it**. The item sat in `blocked` and the trigger fired only
if somebody re-measured on a hunch, which is the same weakness as the prose it was meant to
improve on. A condition nobody checks is a condition that is always false.

The deferral itself is sound: measured 2026-08-27 across every project, the largest held 420
items, 8.4% of the trigger. What was missing is what happens on the day that stops being true.

**The ticket names three ways to build this and have it do nothing**, and each has a test here:

* Exercise it only against the test database, which holds a handful of rows — the check never
  fires and "no warning was emitted" passes for entirely the wrong reason. So the threshold is
  injectable and the tests drive it from both sides.
* Warn unconditionally, which satisfies "it fires when crossed" and is worthless.
* Hardcode the number beside the item's own copy, so the two can drift with nothing to notice.
"""
from __future__ import annotations

import logging

import pytest

from app.models import Item
from app.scaling import (
    SCALING_TRIGGER_ITEM,
    SCALING_TRIGGER_ITEMS,
    check_scaling_triggers,
)


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _count(db, project_id: str) -> int:
    """The seeded dataset already puts items in `core`, so every assertion below has to be
    written against the REAL count rather than the number this file added. Getting that wrong
    is how a threshold test passes for the wrong reason."""
    from sqlalchemy import func, select

    return db.scalar(select(func.count(Item.id)).where(Item.project_id == project_id)) or 0


def _items(db, project_id: str, n: int, start: int = 10_000):
    """Real rows, because the check counts rows. `number` is per-project and unique."""
    for i in range(n):
        db.add(Item(id=f"{project_id}-tripwire-{start + i}", project_id=project_id,
                    number=start + i, title=f"row {i}", status="backlog"))
    db.commit()


# ── both directions, or neither ───────────────────────────────────────────────

def test_it_fires_when_a_project_crosses(db, caplog):
    _items(db, "core", 4)
    with caplog.at_level(logging.WARNING, logger="graphban.scaling"):
        crossed = check_scaling_triggers(db, threshold=_count(db, "core") - 1)
    assert [p for p, _ in crossed] == ["core"]
    assert SCALING_TRIGGER_ITEM in caplog.text, (
        "the warning must name the item whose analysis this reconnects to — a bare count is "
        "not an instruction")


def test_it_is_silent_below_the_line(db, caplog):
    """The control the ticket asks for by name. Warning unconditionally satisfies the test
    above and makes the check worthless."""
    _items(db, "core", 2)
    with caplog.at_level(logging.WARNING, logger="graphban.scaling"):
        assert check_scaling_triggers(db, threshold=_count(db, "core") + 1) == []
    assert "graphban.scaling" not in caplog.text
    assert SCALING_TRIGGER_ITEM not in caplog.text


def test_exactly_at_the_threshold_is_not_over_it(db, caplog):
    """`> threshold`, not `>=`. The trigger reads "over ~5k", and an off-by-one here fires a
    boot warning on a deployment that has not crossed anything."""
    _items(db, "core", 3)
    with caplog.at_level(logging.WARNING, logger="graphban.scaling"):
        assert check_scaling_triggers(db, threshold=_count(db, "core")) == []


# ── the test database is the trap, so drive it deliberately ───────────────────

def test_the_real_threshold_finds_nothing_here(db, caplog):
    """Named so the next reader knows this proves almost nothing on its own.

    The suite's database holds a handful of rows, so a check exercised ONLY at the real
    threshold can never fire — and a test asserting silence would pass for the wrong reason.
    That is why every test above injects a small threshold instead. This one exists to say
    the default is wired through, not to demonstrate the behaviour.
    """
    with caplog.at_level(logging.WARNING, logger="graphban.scaling"):
        assert check_scaling_triggers(db) == []


# ── one number, in one place ──────────────────────────────────────────────────

def test_the_threshold_is_the_one_the_item_named():
    assert SCALING_TRIGGER_ITEMS == 5_000


def test_the_warning_quotes_the_threshold_it_used(db, caplog):
    """Not a hardcoded string. If the constant moves and the message does not, GRPH-55's text
    and the tripwire disagree with nothing to notice — which is the drift the ticket warns
    about, one level down."""
    _items(db, "core", 4)
    n = _count(db, "core") - 1
    with caplog.at_level(logging.WARNING, logger="graphban.scaling"):
        check_scaling_triggers(db, threshold=n)
    assert f"{n:,}-item trigger" in caplog.text


def test_the_warning_names_the_project_and_the_count(db, caplog):
    """An operator's next question is "which one, and how far over"."""
    _items(db, "core", 6)
    n = _count(db, "core")
    with caplog.at_level(logging.WARNING, logger="graphban.scaling"):
        check_scaling_triggers(db, threshold=n - 1)
    assert "core" in caplog.text and f"{n:,} items" in caplog.text


def test_several_crossers_are_all_reported_worst_first(db, caplog):
    """A deployment that crosses on two projects has two problems, and the bigger one is
    where the scan cost lands first."""
    from app.models import Project

    db.add(Project(id="second", name="Second", tag="SEC"))
    db.commit()
    _items(db, "second", _count(db, "core") + 5)
    with caplog.at_level(logging.WARNING, logger="graphban.scaling"):
        crossed = check_scaling_triggers(db, threshold=_count(db, "core") - 1)
    assert [p for p, _ in crossed] == ["second", "core"], "worst first"
    assert "2 projects" in caplog.text


# ── it must not be able to keep the app down ──────────────────────────────────

def test_boot_actually_evaluates_the_scaling_trigger():
    """THE CALL. Every behavioural test drives check_scaling_triggers directly, so removing
    it from lifespan restores the ticket's own defect: a condition nobody evaluates is
    always false (GRPH-540 bounce).
    """
    import ast
    import inspect

    from app import main

    tree = ast.parse(inspect.getsource(main.lifespan))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (
            fn.attr if isinstance(fn, ast.Attribute) else "")
        if name == "check_scaling_triggers":
            found = True
    assert found, (
        "lifespan no longer calls check_scaling_triggers — the trigger is a function "
        "nobody evaluates, which is the defect this ticket exists to close"
    )


def test_a_broken_tripwire_does_not_stop_the_app(monkeypatch, _clean_database):
    """A tripwire is not load-bearing. Asserted by making it RAISE, not by GET /health
    on an already-booted client — that is 200 whether the call is swallowed, missing,
    or never raises (GRPH-540 bounce).
    """
    from fastapi.testclient import TestClient

    from app import main, scaling

    def boom(*a, **k):
        raise RuntimeError("tripwire exploded")

    monkeypatch.setattr(scaling, "check_scaling_triggers", boom)

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200, (
            "a raising tripwire escaped lifespan and took the app down"
        )
