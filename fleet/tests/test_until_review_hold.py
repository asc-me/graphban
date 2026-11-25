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


# --- GRPH-1947: the freed cluster gets back the siblings the hold stripped -----------------
#
# `collision.py` writes `holds` (the reviewer's touch-areas) AND `held` (the sibling item keys
# the hold took out of the cluster). Only `held` restores those siblings. The fake server
# above emits only `holds` on a one-item cluster, so the restoration never ran under test.
#
# Sabotage: in `_lift_stale_review_holds`, iterate `[h.get("by")]` instead of
# `[h.get("by"), *(h.get("held") or [])]` (restore only the holder's own key) → both tests
# below fail: the siblings are missing from `items` and nothing is delegated.

RUNNING = "GRPH-1900"   # left in the cluster by the server; in progress, not a seed
SIBLING = "GRPH-1901"   # stripped by the hold; ready
SIBLING2 = "GRPH-1902"  # stripped by the hold; ready


class _HeldServer:
    """A two-item cluster held by a review whose `held` names the siblings it stripped.
    The holder has since left review (signed off), so the hold is gone."""

    STATUS = {HOLDER: "done", RUNNING: "in_progress", SIBLING: "next", SIBLING2: "next"}

    def __init__(self, items=(RUNNING, SIBLING2)):
        self.items = list(items)
        self.delegated: list[str] = []

    def call(self, tool, **kw):
        if tool == "collision_clusters":
            return {"clusters": [{
                "items": list(self.items),
                "held_by": ["review"],
                "free_in": 240,
                "held_because": [{
                    "reserved": "review", "by": HOLDER,
                    "holds": ["fleet/src/gbfleet/until.py"],
                    # By default SIBLING2 is already in `items`: restored once, not twice.
                    "held": [SIBLING, SIBLING2],
                    "live_holder": None, "lapse": "age", "lapses_in": None,
                }],
            }], "total": 1}
        if tool == "get_item_details":
            status = self.STATUS[kw["id"]]
            return {"id": kw["id"], "status": status, "brief": _brief(),
                    "blocker": "", "review_claimed_by": "", "claimed_by": "",
                    "touchpoints": [f"fleet/{kw['id']}.py"]}
        if tool == "related_work":
            return {"results": []}
        if tool == "delegate":
            self.delegated.append(kw["id"])
            return {"enrolment_code": "WORKER-X", "bound_only": True}
        if tool == "fleet_status":
            return {"agents": []}
        raise AssertionError(tool)


def test_a_freed_cluster_restores_the_siblings_its_review_hold_stripped(events):
    server = _HeldServer()

    got = until._lift_stale_review_holds(server, server.call("collision_clusters"))

    (freed,) = got["clusters"]
    assert "held_by" not in freed and "free_in" not in freed
    assert freed["items"] == [RUNNING, SIBLING2, HOLDER, SIBLING]
    assert freed["held_because"] == []


def test_delegate_next_seeds_the_freed_cluster_from_a_restored_sibling(tmp_path, events):
    """The siblings are what make the freed cluster buildable: the server's own member is in
    progress and the holder is done, so without `held` there is nothing to seed."""
    server = _HeldServer(items=[RUNNING])

    seed, _code, _want = until._delegate_next(
        server, "GRPH-A1", "w", set(), None, repo=tmp_path, base="", bound_out={})

    assert seed in (SIBLING, SIBLING2)
    assert server.delegated and server.delegated[0] == seed
