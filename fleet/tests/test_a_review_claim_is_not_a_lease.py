"""gbfleet must not act on a review claim as if it were a build lease (GRPH-1001 bounce).

The server started reporting both kinds of hold on the roster, tagged `hold: build | review`,
because a review claim stops an item as surely as a lease does and a supervisor could not see
it. But every consumer of `holdings` in this package was written when the list could only be
build leases, and acts accordingly: it files a reaped child's salvage branch against them,
releases them, and counts them as work in progress.

Applied to a review claim each of those is wrong, and the middle one is destructive — a reaped
reviewer's salvage branch would be filed under the BUILDER's item. One predicate, three call
sites, and a test on each call site rather than only on the predicate.
"""
import time
from pathlib import Path

from gbfleet import spawn as spawn_mod
from gbfleet.spawn import build_holdings
from gbfleet.supervisor import _holdings
from gbfleet.until import _any_holdings


def _agent(agent_id="A-1", holdings=None):
    return {"id": agent_id, "holdings": holdings or []}


def _hold(item_id, hold=None, phase="building"):
    h = {"id": item_id, "phase": phase}
    if hold is not None:
        h["hold"] = hold
    return h


class _Roster:
    """Minimal stand-in for the supervisor client `_any_holdings` calls."""

    def __init__(self, agents):
        self._agents = agents

    def call(self, _tool, **_kw):
        return {"agents": self._agents}


# ── the predicate ──────────────────────────────────────────────────────────────────────

def test_a_review_claim_is_not_a_build_hold():
    agent = _agent(holdings=[_hold("GB-1", "build"), _hold("GB-2", "review")])
    assert [h["id"] for h in build_holdings(agent)] == ["GB-1"]


def test_an_unlabelled_hold_is_a_build_hold():
    """An older server sends no `hold`. Before the field existed every holding WAS a lease,
    so defaulting to review would make a current supervisor stop salvaging against an older
    deployment — a silent regression on exactly the pairing nobody tests."""
    agent = _agent(holdings=[_hold("GB-1")])
    assert [h["id"] for h in build_holdings(agent)] == ["GB-1"]


def test_an_agent_holding_only_a_review_claim_has_no_build_holds():
    agent = _agent(holdings=[_hold("GB-2", "review")])
    assert build_holdings(agent) == []


# ── call site 1: the salvage subject ───────────────────────────────────────────────────

def test_the_salvage_subject_excludes_a_review_claim():
    """`_holdings` feeds partition.held and from there `child.held_items`, which is what a
    reaped child's branch is salvaged against. This is the destructive one: a reviewer reaped
    mid-review would have its WIP branch filed against the item it was reviewing — work
    attributed to the builder, by the reviewer's own reap."""
    roster = {"agents": [_agent("A-rev", [_hold("GB-2", "review")]),
                         _agent("A-build", [_hold("GB-1", "build")])]}

    held = _holdings(roster)

    assert held["A-rev"] == [], held
    assert held["A-build"] == ["GB-1"], held


# ── call site 2: "is anyone still working?" ────────────────────────────────────────────

def test_a_lone_review_claim_is_not_work_in_progress():
    """`until` pairs `_any_holdings` with liveness to decide whether to keep waiting. A
    reviewer holding an item is not a builder working, and counting it kept the loop spinning
    on a wave whose only activity was a review."""
    assert _any_holdings(_Roster([_agent("A-rev", [_hold("GB-2", "review")])])) is False


def test_a_build_lease_is_still_work_in_progress():
    """The control. A filter that excluded everything would satisfy every assertion above."""
    assert _any_holdings(_Roster([_agent("A-build", [_hold("GB-1", "build")])])) is True


def test_a_stale_build_lease_is_still_not_work_in_progress():
    """The pre-existing rule must survive the new one: a hold older than the presence TTL is
    a dead agent's, and counting it kept a wave waiting on a dead process forever."""
    assert _any_holdings(
        _Roster([_agent("A-dead", [_hold("GB-1", "build", phase="stale")])])
    ) is False


# ── call site 3: what a registering child records as its own ───────────────────────────

class _Proc:
    returncode = None

    def poll(self):
        return None


def _child(worktree: str) -> spawn_mod.Child:
    return spawn_mod.Child(
        adapter="fake", worktree=Path(worktree), branch="gb/w-1", base="main",
        seat_path=Path("/tmp/seat"), process=_Proc(), started_at=time.monotonic(),
        log_dir=Path("/tmp/logs"),
    )


def test_a_registering_child_records_only_its_build_leases(tmp_path):
    """`await_registration` seeds `child.held_items` from the roster row, and that list is
    the salvage subject when the child is later reaped. A reviewer registering while it holds
    a review claim must not adopt that item as something its branch can be filed against."""
    wt = tmp_path / "w"
    wt.mkdir()
    child = _child(str(wt))
    roster = {"agents": [{
        "id": "GRPH-A9", "worktree": str(wt),
        "holdings": [_hold("GB-1", "build"), _hold("GB-2", "review")],
    }]}

    spawn_mod.await_registration(child, lambda: roster, window=5, poll=0, sleep=lambda _: None)

    assert child.held_items == ["GB-1"], child.held_items


def test_a_child_holding_only_a_review_claim_records_nothing_to_salvage_against(tmp_path):
    wt = tmp_path / "w"
    wt.mkdir()
    child = _child(str(wt))
    roster = {"agents": [{
        "id": "GRPH-A9", "worktree": str(wt),
        "holdings": [_hold("GB-2", "review")],
    }]}

    spawn_mod.await_registration(child, lambda: roster, window=5, poll=0, sleep=lambda _: None)

    assert child.held_items == [], child.held_items
