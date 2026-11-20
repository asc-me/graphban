"""GRPH-1753. A spawn exception in `until` is per item.

The process returns. The delegation that spawn opened is withdrawn, so the next
planner is not refused for it. The same failure on every pass ends the run
`spawn_failed` instead of an unhandled traceback.

Sabotage: drop the `withdraw=True` call in `until._loop`'s `_withdraw_spawn`.
The second-run test then fails with the open-delegation refusal.
"""
from __future__ import annotations

from pathlib import Path

from gbfleet.client import ToolFailed
from gbfleet.seat import UnrenderableSeat
from gbfleet.supervisor import Limits
from gbfleet.until import run

from tests.test_supervisor import KEY, _factory
from tests.test_until import _clients

MSG = "oauth token cannot be rendered"


def _raising(seat, tree, instruction_file, debug_file=None):
    raise UnrenderableSeat(MSG)


def _track(planner):
    """A delegate ledger the stock fake does not have: an open row, and a withdraw that closes it.

    A second planner is refused while the first planner's row is open, in the words the
    server uses. `withdraw: true` from the owner closes it and opens nothing.
    """
    real = planner.call
    ids = iter(["GRPH-P-A", "GRPH-P-B", "GRPH-P-C"])
    open_by_item: dict[str, dict] = {}
    opened: list[dict] = []
    refused: list[str] = []

    def call(tool, /, **arguments):
        if tool == "register_agent":
            payload = real(tool, **arguments)
            payload = dict(payload)
            payload["agent_id"] = next(ids)
            return payload
        if tool == "delegate":
            item = str(arguments.get("id"))
            caller = str(arguments.get("agent_id") or "")
            row = open_by_item.get(item)
            if arguments.get("withdraw"):
                if row and row["by"] != caller:
                    message = (
                        f"{item} already has an open delegation ({row['id']}) from "
                        f"{row['by']}, 10s old"
                    )
                    refused.append(message)
                    raise ToolFailed(tool, "conflict", message)
                if row and row["by"] == caller:
                    open_by_item.pop(item, None)
                    return {"delegation_id": row["id"], "state": "closed",
                            "withdrew": row["id"], "enrolment_code": None,
                            "brief": {}, "bound_only": False}
                return {"delegation_id": "", "state": "absent", "withdrew": None,
                        "enrolment_code": None, "brief": {}, "bound_only": False}
            if row and row["by"] != caller:
                message = (
                    f"{item} already has an open delegation ({row['id']}) from "
                    f"{row['by']}, 10s old"
                )
                refused.append(message)
                raise ToolFailed(tool, "conflict", message)
            reply = real(tool, **arguments)
            open_by_item[item] = {"id": reply.get("delegation_id"), "by": caller}
            opened.append({"item": item, "by": caller, "id": reply.get("delegation_id")})
            return reply
        return real(tool, **arguments)

    planner.call = call
    return open_by_item, opened, refused


def _go(git_repo, tmp_path, state, factory, planner, supervisor, workspace, *,
        empty_ticks=1, limits=None):
    return run(
        git_repo, factory, planner, supervisor, api_key=KEY, server="http://gb.invalid",
        adapter="fake", state=state, workspace=workspace, poll=0, sleep=lambda _: None,
        empty_ticks=empty_ticks, limits=limits or Limits(max_workers=1),
    )


def test_a_raising_spawn_returns_and_withdraws_the_delegation(
    git_repo: Path, tmp_path: Path, state: Path,
):
    """The process does not die. The fake server shows no open delegation, and the
    summary carries spawn_failed with the exception class and message."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], sticky_clusters=True)
    open_by_item, opened, refused = _track(planner)
    result = _go(git_repo, tmp_path, state, _raising, planner, supervisor, workspace,
                 empty_ticks=1)
    summary = result.as_json()
    assert result.reason == "spawn_failed", summary
    assert open_by_item == {}, open_by_item
    assert refused == []
    assert opened and opened[0]["by"] == "GRPH-P-A"
    text = " ".join(summary["spawn_failed"])
    assert "UnrenderableSeat" in text and MSG in text, summary["spawn_failed"]


def test_a_second_planner_is_not_refused_for_the_withdrawn_delegation(
    git_repo: Path, tmp_path: Path, state: Path,
):
    """Sabotage: remove the withdraw in `_withdraw_spawn`. This fails with
    'already has an open delegation'."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], sticky_clusters=True)
    open_by_item, opened, refused = _track(planner)
    first = _go(git_repo, tmp_path, state, _raising, planner, supervisor, workspace,
                empty_ticks=1)
    assert first.reason == "spawn_failed"
    second = _go(git_repo, tmp_path, state, _raising, planner, supervisor,
                 tmp_path / "ws2", empty_ticks=1)
    # This is the assertion the sabotage hits: the second planner is refused
    # while the first planner's row is still open.
    assert refused == [], refused
    assert any(row["by"] == "GRPH-P-B" for row in opened), opened
    assert open_by_item == {}
    assert second.reason == "spawn_failed"
    assert "UnrenderableSeat" in " ".join(second.as_json()["spawn_failed"])


def test_a_spawn_that_keeps_failing_ends_spawn_failed(
    git_repo: Path, tmp_path: Path, state: Path,
):
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], sticky_clusters=True)
    _open, opened, refused = _track(planner)
    result = _go(git_repo, tmp_path, state, _raising, planner, supervisor, workspace,
                 empty_ticks=3)
    assert result.reason == "spawn_failed", result.as_json()
    assert result.exit == 1
    assert refused == []
    # One opening delegation per pass, each withdrawn. Not an unbounded restart.
    assert len(opened) == 3, opened
    assert all("UnrenderableSeat" in line for line in result.as_json()["spawn_failed"])


def test_a_later_item_still_spawns_when_an_earlier_one_raises(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """The failure is per item. The next item in the cluster gets a child."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-A", "GRPH-B"]], sticky_clusters=True)
    open_by_item, opened, refused = _track(planner)
    real = _factory(scripts, "works_then_exits")
    # The first launch is the first item in the cluster. An unbound seat does not
    # carry that id, so the factory cannot read it; the order is the signal.
    launched = {"n": 0}

    def factory(seat, tree, instruction_file, debug_file=None):
        launched["n"] += 1
        if launched["n"] == 1:
            raise UnrenderableSeat(MSG)
        return real(seat, tree, instruction_file, debug_file)

    result = _go(
        git_repo, tmp_path, state, factory, planner, supervisor, workspace,
        empty_ticks=2, limits=Limits(max_workers=1, max_children=1),
    )
    assert refused == []
    assert any(row["item"] == "GRPH-B" for row in opened), opened
    assert result.spawned >= 1, result.as_json()
    assert "GRPH-A" not in open_by_item
    assert "spawn_failed" in result.as_json()
    assert any("UnrenderableSeat" in line for line in result.as_json()["spawn_failed"])
