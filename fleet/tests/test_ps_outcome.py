"""`ps` says what a child's work came to, not only whether its process is alive (GRPH-812).

Reported from super-arc: a child that had completed work and one that had done nothing were
identical — `running: false, stopped_because: null`. That identity actively misled the
operator into reporting two children had done nothing when one had already signed off, which
is the most expensive kind of wrong because it is confidently specific.

Three answers, and every way of collapsing them is a bug in one direction:

* `did` — held items, at least one reached review or done
* `nothing` — held items and none moved, or held none
* `unknown` — the ledger could not be asked

Reading `unknown` as `nothing` is the original defect with a new cause: a supervisor whose
client may not read items would report the whole fleet as idle.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gbfleet.mcp import _outcome


class _Child:
    def __init__(self, held=(), assigned=None):
        self.held_items = list(held)
        self.assigned = assigned


def _statuses(**kw) -> dict[str, dict]:
    return {k: {"status": v} for k, v in kw.items()}


# ---- the three answers -----------------------------------------------------------------------

def test_a_child_whose_item_reached_review_did_the_work():
    got = _outcome(_Child(["SA-417"]), _statuses(**{"SA-417": "review"}))

    assert got["verdict"] == "did"
    assert "SA-417" in got["detail"]


def test_done_counts_too():
    assert _outcome(_Child(["SA-417"]), _statuses(**{"SA-417": "done"}))["verdict"] == "did"


def test_a_child_whose_item_never_moved_did_nothing():
    got = _outcome(_Child(["SA-417"]), _statuses(**{"SA-417": "in_progress"}))

    assert got["verdict"] == "nothing"
    assert "SA-417" in got["detail"], "said nothing about which item it was holding"


def test_a_child_that_held_nothing_says_so():
    got = _outcome(_Child([]), _statuses())

    assert got["verdict"] == "nothing"
    assert got["items"] == []
    assert "held no item" in got["detail"]


def test_an_unreadable_ledger_is_unknown_not_nothing():
    """THE ONE THAT MATTERS. A supervisor whose client may not read items would otherwise
    report every child as idle — the original bug, arrived at differently."""
    got = _outcome(_Child(["SA-417"]), {})

    assert got["verdict"] == "unknown"
    assert got["items"] == ["SA-417"]
    assert "not known here" in got["detail"]


def test_an_item_the_ledger_did_not_return_is_not_treated_as_finished():
    """A row missing from the sweep is an item this client could not see. Reading absence as
    completion is how a child gets credited for work nobody can point at."""
    got = _outcome(_Child(["SA-417"]), _statuses(**{"SA-999": "done"}))

    assert got["verdict"] == "nothing"
    assert got["items"][0]["status"] == "unknown"


# ---- where the item comes from -----------------------------------------------------------------

def test_a_bound_seats_item_counts_even_before_holdings_arrive():
    """PRD-36: a bound seat's child holds its item from registration, and `assigned` carries
    it. A child killed early has `assigned` and no holdings, and it still held something."""
    got = _outcome(_Child([], assigned={"item": "SA-420", "state": "claimed"}),
                   _statuses(**{"SA-420": "review"}))

    assert got["verdict"] == "did"
    assert [r["id"] for r in got["items"]] == ["SA-420"]


def test_holdings_win_over_assigned():
    """`held_items` is what the roster actually reported; `assigned` is what the seat asked
    for. When both exist the observed one is the truth."""
    got = _outcome(_Child(["SA-1"], assigned={"item": "SA-2"}), _statuses(**{"SA-1": "done"}))

    assert [r["id"] for r in got["items"]] == ["SA-1"]


def test_one_finished_item_is_enough():
    got = _outcome(_Child(["SA-1", "SA-2"]),
                   _statuses(**{"SA-1": "backlog", "SA-2": "done"}))

    assert got["verdict"] == "did"
    assert "SA-2" in got["detail"] and "SA-1" not in got["detail"].split("reached")[0]
