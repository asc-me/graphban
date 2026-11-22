"""GRPH-1849. A `next` member seeds before a `backlog` member.

The server lists a cluster's items in ascending id. `_seed_ready` accepts both
statuses, so the first ready member by id used to win even when a later member
was `next`. Ranking is only among clean ready candidates: a bounce remainder
(GRPH-1024) stays a candidate or an occupier exactly as it did, and is then
ordered with the rest.

Sabotage: in `_delegate_next`, stop sorting `clean` / clusters by `_ready_rank`
(offer `row["clean"]` in server order, and do not reorder `prepared`). The
first test fails: the delegation is A, not C.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet.until import NEXT_BEFORE_BACKLOG, _delegate_next


class _Planner:
    def __init__(self, clusters, details):
        self.clusters = clusters
        self.details = details
        self.delegated: list[str] = []

    def call(self, tool, **kw):
        if tool == "collision_clusters":
            return {"clusters": self.clusters}
        if tool == "get_item_details":
            return dict(self.details[kw["id"]])
        if tool == "related_work":
            return {"results": []}
        if tool == "delegate":
            self.delegated.append(kw["id"])
            return {"enrolment_code": "WORKER-X", "bound_only": True}
        raise AssertionError(tool)


def _brief():
    return {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}


def _item(item_id: str, status: str) -> dict:
    return {"id": item_id, "status": status, "brief": _brief(), "blocker": ""}


def _seed(tmp_path: Path, clusters, details, **kw):
    planner = _Planner(clusters, details)
    flags: dict = {}
    seed, code, _want = _delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=tmp_path, base="",
        bound_out=flags, **kw,
    )
    return seed, code, planner, flags


def test_a_next_member_seeds_ahead_of_earlier_backlog_members(tmp_path: Path):
    """A cluster [A backlog, B backlog, C next], all ready: the first delegation is C.

    Sabotage: drop the `_ready_rank` sort. This fails: seed is A.
    """
    details = {
        "A": _item("A", "backlog"),
        "B": _item("B", "backlog"),
        "C": _item("C", "next"),
    }
    seed, code, planner, flags = _seed(
        tmp_path, [{"items": ["A", "B", "C"]}], details,
    )
    assert seed == "C" and code == "WORKER-X"
    assert planner.delegated == ["C"]
    assert flags["delegated_reason"] == NEXT_BEFORE_BACKLOG


def test_a_next_seeded_cluster_takes_the_free_slot(tmp_path: Path):
    """Two clusters. The first's seed is backlog, the second's is next. One call is
    one free slot: the next item is delegated, and the backlog cluster is not."""
    details = {
        "A": _item("A", "backlog"),
        "B": _item("B", "backlog"),
        "C": _item("C", "next"),
    }
    seed, _code, planner, flags = _seed(
        tmp_path,
        [{"items": ["A", "B"]}, {"items": ["C"]}],
        details,
    )
    assert seed == "C"
    assert planner.delegated == ["C"]
    assert flags["delegated_reason"] == NEXT_BEFORE_BACKLOG


def test_with_no_next_member_the_first_ready_by_id_still_seeds(tmp_path: Path):
    """No `next` member: the order is the server's, first ready id in the first cluster."""
    details = {
        "A": _item("A", "backlog"),
        "B": _item("B", "backlog"),
        "C": _item("C", "backlog"),
        "D": _item("D", "backlog"),
    }
    seed, _code, planner, flags = _seed(
        tmp_path, [{"items": ["A", "B", "C"]}], details,
    )
    assert seed == "A"
    assert planner.delegated == ["A"]
    assert "delegated_reason" not in flags

    seed, _code, planner, flags = _seed(
        tmp_path,
        [{"items": ["A", "B"]}, {"items": ["D"]}],
        details,
    )
    assert seed == "A"
    assert planner.delegated == ["A"]
    assert "delegated_reason" not in flags


def test_within_one_status_the_server_order_is_kept(tmp_path: Path):
    """Two `next` members keep ascending-id order. The earlier backlog does not seed,
    and the later `next` does not jump the earlier `next`."""
    details = {
        "A": _item("A", "backlog"),
        "B": _item("B", "next"),
        "C": _item("C", "next"),
    }
    seed, _code, planner, flags = _seed(
        tmp_path, [{"items": ["A", "B", "C"]}], details,
    )
    assert seed == "B"
    assert planner.delegated == ["B"]
    assert flags["delegated_reason"] == NEXT_BEFORE_BACKLOG


def test_the_delegated_event_names_the_jump(git_repo, tmp_path, scripts, state, monkeypatch):
    """THE CALL. `_delegate_next` records the reason; `failover` is what emits `delegated`
    after the child claims. Dropping `reason` from that emit leaves the ranking tests
    green and this one red.

    Sabotage: emit `delegated` without `flags["delegated_reason"]`. This fails: the
    event has no reason.
    """
    from gbfleet.supervisor import Limits
    from gbfleet.until import run
    from tests.test_supervisor import KEY, _factory
    from tests.test_until import _clients

    events: list[tuple] = []
    monkeypatch.setattr(
        "gbfleet.until.observe.emit", lambda ev, **kw: events.append((ev, kw)),
    )
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace,
        clusters=1,
        cluster_items=[["A", "B", "C"]],
        bound_seats=True,
        item_details={
            "A": {"id": "A", "status": "backlog"},
            "B": {"id": "B", "status": "backlog"},
            "C": {"id": "C", "status": "next"},
        },
        roster_assigned={
            "item": "C", "state": "claimed", "reason": None, "held_by": None,
        },
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        limits=Limits(max_workers=1),
    )
    assert result.spawned == 1, result.detail
    delegated = [kw for ev, kw in events if ev == "delegated"]
    assert len(delegated) == 1, events
    assert delegated[0]["item"] == "C"
    assert delegated[0]["reason"] == NEXT_BEFORE_BACKLOG
