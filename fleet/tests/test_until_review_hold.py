"""A bounced item is not held by review (GRPH-1832).

Seen on the GRPH-P95 drain: a reviewer bounced GRPH-1828 to `next`, nobody held a review
claim, and `until` logged `contention ... GRPH-1828 held by no live reviewer` for 49 cycles
without delegating the rebuild. The supervisor took the server's `held_by: ["review"]` as
the answer every cycle and never re-read the holding item. A hold no live agent owns is
not a hold.

Sabotage: make `_stale_review_hold` return ("", "") unconditionally (keep the review hold
whatever the item's status now is) → the first test fails: nothing is delegated and a
`contention` line is logged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gbfleet import until

HOLDER = "GRPH-1828"


def _brief():
    return {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}


class _Server:
    """`collision_clusters` keeps reporting the review hold, as on the P95 drain."""

    def __init__(self, status: str = "review", reviewer: str = "GRPH-A9"):
        self.status = status
        self.reviewer = reviewer
        self.delegated: list[str] = []

    def bounce(self) -> None:
        self.status, self.reviewer = "next", ""

    def call(self, tool, **kw):
        if tool == "collision_clusters":
            return {"clusters": [{
                "items": [HOLDER],
                "held_by": ["review"],
                "held_because": [{
                    "reserved": "review", "by": HOLDER,
                    "holds": ["fleet/src/gbfleet/until.py"],
                    "live_holder": self.reviewer or None,
                    "lapse": None if self.reviewer else "age",
                    "lapses_in": 240 if self.reviewer else None,
                }],
            }], "total": 1}
        if tool == "get_item_details":
            assert kw["id"] == HOLDER
            return {"id": HOLDER, "status": self.status, "brief": _brief(),
                    "blocker": "", "review_claimed_by": self.reviewer,
                    "claimed_by": "", "touchpoints": ["fleet/src/gbfleet/until.py"]}
        if tool == "related_work":
            return {"results": []}
        if tool == "delegate":
            self.delegated.append(kw["id"])
            return {"enrolment_code": "WORKER-X", "bound_only": True}
        if tool == "fleet_status":
            return {"agents": []}
        raise AssertionError(tool)


@pytest.fixture
def events(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(until.observe, "emit", lambda name, **f: seen.append((name, f)))
    until._contention.clear()
    until._stale_holds.clear()
    yield seen
    until._contention.clear()
    until._stale_holds.clear()


def _cycle(server: _Server, tmp_path: Path):
    need = until._wanted_workers(server, server, live_n=0, max_workers=2)
    seed = None
    if need > 0:
        seed, _code, _want = until._delegate_next(
            server, "GRPH-A1", "w", set(), None, repo=tmp_path, base="", bound_out={})
    return need, seed


def test_an_item_bounced_out_of_review_is_delegated_on_the_next_cycle(tmp_path, events):
    """THE REGRESSION. In review under a live reviewer: held, contention, nothing spawned.
    Bounced to `next` with no review claim, while the server still reports the hold: the
    next cycle delegates it and logs no contention for it."""
    server = _Server()
    need, seed = _cycle(server, tmp_path)
    assert (need, seed) == (0, None)
    assert [n for n, _ in events].count("contention") == 1

    server.bounce()
    events.clear()
    need, seed = _cycle(server, tmp_path)

    assert need == 1
    assert seed == HOLDER and server.delegated == [HOLDER]
    assert not [f for n, f in events if n == "contention"]
    stale = [f["detail"] for n, f in events if n == "review_hold_stale"]
    assert stale and HOLDER in stale[0] and "next" in stale[0]


def test_a_review_hold_with_no_live_reviewer_and_no_claim_is_not_contention(events):
    """Held by review on the server, no live reviewer, no review claim: not contention.
    It spawns no builder either — the row is still in review, and the review branch is
    what staffs it."""
    server = _Server(status="review", reviewer="")

    need = until._wanted_workers(server, server, live_n=0, max_workers=2)

    assert need == 0
    assert not [f for n, f in events if n == "contention"]
    stale = [f["detail"] for n, f in events if n == "review_hold_stale"]
    assert stale and "no live reviewer and no review claim" in stale[0]


def test_a_live_review_hold_is_still_contention(events):
    """The other arm: a reviewer is on it, so the cluster is held and the wait is reported."""
    server = _Server(status="review", reviewer="GRPH-A9")

    assert until._wanted_workers(server, server, live_n=0, max_workers=2) == 0
    assert [n for n, _ in events].count("contention") == 1
    assert not [n for n, _ in events if n == "review_hold_stale"]


def test_an_unreadable_holder_keeps_the_hold(events):
    """Unknown is not free: a holder whose row cannot be read keeps the cluster held."""
    class _Unreadable(_Server):
        def call(self, tool, **kw):
            if tool == "get_item_details":
                raise until.ToolFailed("get_item_details", "error", "boom")
            return super().call(tool, **kw)

    server = _Unreadable(status="next", reviewer="")
    got = until._lift_stale_review_holds(server, server.call("collision_clusters"))
    assert got["clusters"][0]["held_by"] == ["review"]


def test_a_reservation_hold_is_left_as_sent(events):
    server = _Server(status="next", reviewer="")
    sent = {"clusters": [{"items": ["X"], "held_by": ["GRPH-A2"], "free_in": 30}]}
    assert until._lift_stale_review_holds(server, sent) is sent
