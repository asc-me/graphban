"""GRPH-1258 (Capacity S1↔S3): the wave declares the entitlements an adapter runs on and
reports spawn_start / registered / limit_hit / spawn_failed for them."""
from __future__ import annotations

from pathlib import Path

from gbfleet.client import ToolFailed
from gbfleet.seat import Seat, instruction_for
from gbfleet.spawn import LaunchFailed, VendorLimit
from gbfleet.supervisor import Limits
from gbfleet.until import run

from tests.test_supervisor import KEY, _factory
from tests.test_until import _clients

SAID = "You've hit your limit · resets 12:20pm (America/New_York)"


def _limited(seat, tree, instruction_file, debug_file=None):
    raise VendorLimit("out of quota", adapter="fake", said=SAID)


def _broken(seat, tree, instruction_file, debug_file=None):
    raise LaunchFailed("binary missing")


def _tap(planner, *, admitted=True, refuse_with=None):
    """Record the register_agent declaration and every ledger event the wave sends."""
    events: list[dict] = []
    registered: list[dict] = []
    real = planner.call

    def call(tool, /, **arguments):
        if tool == "register_agent":
            registered.append(dict(arguments))
            arguments.pop("entitlements", None) if refuse_with else None
            if refuse_with and registered[-1].get("entitlements"):
                raise ToolFailed(tool, "quota_exceeded", "capacity routing needs team")
        if tool == "report_entitlement_event":
            events.append(dict(arguments))
            if refuse_with:
                raise ToolFailed(tool, "quota_exceeded", "capacity routing needs team")
            return {"admitted": admitted, "reason": "" if admitted else "entitlement is exhausted"}
        return real(tool, **arguments)

    planner.call = call
    return events, registered


def _go(git_repo, tmp_path, state, factory, planner, supervisor, workspace, ents):
    return run(
        git_repo, factory, planner, supervisor, api_key=KEY, server="http://gb.invalid",
        adapter="fake", state=state, workspace=workspace, poll=0, sleep=lambda _: None,
        empty_ticks=1, limits=Limits(max_workers=1), entitlements=ents,
    )


def test_a_good_spawn_declares_then_reports_start_and_registered(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Sabotage: drop the `registered` report in `_start` and the second event is missing."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, cluster_items=[["GRPH-7"]], workers=0)
    events, registered = _tap(planner)
    _go(git_repo, tmp_path, state, _factory(scripts, "works_then_exits"), planner, supervisor,
        workspace, {"fake": ["ent-1"]})
    assert registered[0]["entitlements"] == ["ent-1"]
    assert [e["event"] for e in events][:2] == ["spawn_start", "registered"]
    assert all(e["id"] == "ent-1" for e in events)


def test_a_vendor_limit_reports_limit_hit_with_the_vendors_words(
    git_repo: Path, tmp_path: Path, state: Path,
):
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, cluster_items=[["GRPH-7"]])
    events, _ = _tap(planner)
    _go(git_repo, tmp_path, state, _limited, planner, supervisor, workspace, {"fake": ["ent-1"]})
    hit = [e for e in events if e["event"] == "limit_hit"]
    assert hit and hit[0]["vendor_text"] == SAID


def test_a_failed_launch_reports_spawn_failed(
    git_repo: Path, tmp_path: Path, state: Path,
):
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, cluster_items=[["GRPH-7"]])
    events, _ = _tap(planner)
    _go(git_repo, tmp_path, state, _broken, planner, supervisor, workspace, {"fake": ["ent-1"]})
    failed = [e for e in events if e["event"] == "spawn_failed"]
    assert failed and "binary missing" in failed[0]["vendor_text"]


def test_admitted_false_launches_nothing(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Sabotage: ignore `refused` in `_start` and a child is spawned despite the refusal."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, cluster_items=[["GRPH-7"]])
    events, _ = _tap(planner, admitted=False)
    result = _go(git_repo, tmp_path, state, _factory(scripts, "works_then_exits"), planner,
                 supervisor, workspace, {"fake": ["ent-1"]})
    assert result.spawned == 0
    assert [e["event"] for e in events] == ["spawn_start"]
    assert result.reason == "capacity"


def test_off_a_capacity_routing_plan_the_wave_runs_as_before(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, cluster_items=[["GRPH-7"]], workers=0)
    events, registered = _tap(planner, refuse_with=True)
    result = _go(git_repo, tmp_path, state, _factory(scripts, "works_then_exits"), planner,
                 supervisor, workspace, {"fake": ["ent-1"]})
    assert len(registered) == 2 and "entitlements" not in registered[1]
    assert events == []
    assert result.spawned >= 1


def test_the_child_instruction_names_the_entitlements():
    seat = Seat(code="W-1", server_url="http://x", api_key="k", entitlements=("ent-1",),
                declare={"vendor": "anthropic"})
    text = instruction_for(seat, Path("/w"), "gb/x-1")
    assert 'entitlements=["ent-1"]' in text
    assert "capabilities=" in text
    bare = Seat(code="W-1", server_url="http://x", api_key="k")
    assert "entitlements" not in instruction_for(bare, Path("/w"), "gb/x-1")
