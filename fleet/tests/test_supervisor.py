"""One wave, deterministically, end to end.

PRD-22 S1. Real worktrees, real processes, a mocked Graphban. The things worth
asserting here are mostly about what the wave REPORTS, because a supervisor that runs
four children and tells you nothing useful about them is the failure this whole design
is trying to avoid — spending money while nobody watches.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import httpx
import pytest

from gbfleet.cli import make_launch_factory, read_seats, report
from gbfleet.client import Graphban
from gbfleet.lock import RepoLocked, hold
from gbfleet.seat import Seat
from gbfleet.spawn import Launch, Reason
from gbfleet.supervisor import AllocationRead, Limits, up
from gbfleet.worktree import Disposition, SEAT_FILES, Worktree, create, is_seat_file, orphans, reap, salvage_message
from gbfleet.hostos import is_owner_only  # noqa: E402

CODE = "WORKER-7F3K"
KEY = "gbk_test"


def _seats(n: int, server: str = "http://gb.invalid") -> list[Seat]:
    return [Seat(code=f"{CODE}-{i}", server_url=server, api_key=KEY) for i in range(n)]


def _server(
    workspace: Path,
    allocation: dict | None = None,
    unreachable: bool = False,
    blind: bool = False,
    holdings: list | None = None,
    items: list | None = None,
) -> Graphban:
    """A Graphban that reports every worktree under `workspace` as a registered agent.

    Registration is matched on the worktree (see spawn.await_registration), so the
    roster has to reflect what actually got created — which the handler discovers by
    looking, rather than by being told.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if unreachable:
            raise httpx.ConnectError("no route to host")

        body = json.loads(request.content)
        tool = body["params"]["name"]
        if tool == "propose_allocation":
            payload = allocation or {
                "workers": 0,
                "reviewers": 0,
                "mapping": [],
                "rationale": "no agents online — nothing to allocate",
            }
        elif tool == "search_items":
            payload = {"results": list(items or [])}
        else:
            # `blind` is a server that is up and answering, and simply never sees the
            # child — which is what a broken adapter looks like from here, and is a
            # different thing from being unreachable.
            trees = [] if blind else sorted(
                p for p in workspace.glob("*") if p.is_dir() and p.name != "logs"
            )
            payload = {
                "agents": [
                    {
                        "id": f"GRPH-A{i + 1}",
                        "worktree": str(p),
                        "state": "idle",
                        # The roster has carried this since GRPH-451; a fixture that
                        # omits it lets anything reading it look like it works.
                        "enrolled": True,
                        "enrolment_id": f"seat-{i + 1}",
                        "holdings": list(holdings or []),
                    }
                    for i, p in enumerate(trees)
                ]
            }
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload,
                },
            },
        )

    from gbfleet.client import ALLOWED_TOOLS
    return Graphban(
        "http://gb.invalid", KEY,
        allowed=ALLOWED_TOOLS | frozenset({"search_items"}),
        transport=httpx.MockTransport(handler),
    )


def _factory(scripts, which: str, adapter: str = "fake"):
    template = [str(scripts["python"]), str(scripts[which])]
    return make_launch_factory(adapter, template)


# --- a wave that works ------------------------------------------------------------


def test_a_wave_spawns_a_child_per_seat_and_reaps_them(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(2),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=4),
        state=state,
        workspace=workspace,
    )

    assert wave.ok, wave.failures
    assert len(wave.spawned) == 2
    assert all(c.agent_id for c in wave.spawned)
    assert all(c.registration_latency is not None for c in wave.spawned)

    assert len(wave.reaped) == 2
    assert {r.disposition for r in wave.reaped} == {Disposition.SALVAGED}
    assert all(r.removed for r in wave.reaped)
    assert not any(c.worktree.exists() for c in wave.spawned)

    # The work survives as branches, which is the whole point of salvaging.
    branches = {o.branch for o in orphans(git_repo)}
    assert branches == {r.branch for r in wave.reaped}


def test_the_next_child_of_an_open_item_starts_on_the_salvage_branch(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """P30 D9. Cutting from HEAD redoes the work. Sabotage: ignore `items`; this
    child lands on a new gb/wave-* from HEAD and wip.py is missing.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dead = create(git_repo, workspace / "dead", "wave", "1")
    (dead.path / "wip.py").write_text("keep-me\n", encoding="utf-8")
    reap(dead, message=salvage_message("fake", ["GRPH-1"]))

    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"),
        _server(workspace), limits=Limits(max_workers=1),
        state=state, workspace=workspace,
        items={"GRPH-1": {"status": "next", "claimed_by": ""}},
    )
    assert wave.resumed == [dead.branch], wave.resume_misses
    assert wave.spawned[0].branch == dead.branch
    # The salvage file was in the tree the child started with.
    assert any(r.branch == dead.branch for r in wave.reaped)


def test_up_resumes_a_salvage_orphan_without_injected_items(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """P30 D9 bounce. Production `gbfleet up` never passed `items=`; choose_resume
    then always saw {} and cut from HEAD. Salvage keys come from last holdings, and
    `up` fetches item status itself.
    """
    from gbfleet.client import ALLOWED_TOOLS
    from gbfleet.cli import SPAWN_READS

    workspace = tmp_path / "ws"
    first = up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"),
        _server(
            workspace,
            holdings=[{"id": "GRPH-1"}],
            items=[{"id": "GRPH-1", "status": "next", "claimed_by": ""}],
        ),
        limits=Limits(max_workers=1),
        state=state, workspace=workspace,
    )
    assert first.reaped, first.failures
    assert first.spawned[0].held_items == ["GRPH-1"], first.spawned[0].held_items
    found = orphans(git_repo)
    assert any(o.item_keys == ("GRPH-1",) for o in found), [o.subject for o in found]

    second = up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"),
        _server(
            workspace,
            items=[{"id": "GRPH-1", "status": "next", "claimed_by": ""}],
        ),
        limits=Limits(max_workers=1),
        state=state, workspace=workspace,
    )
    assert second.resumed, (
        f"did not resume; misses={second.resume_misses} spawned="
        f"{[c.branch for c in second.spawned]}"
    )
    assert ALLOWED_TOOLS == frozenset({"fleet_status", "propose_allocation"})
    assert "search_items" not in ALLOWED_TOOLS
    assert "search_items" in SPAWN_READS


def test_remember_holdings_keeps_the_last_non_empty():
    """Live roster after release_item is empty; reap must not read that."""
    import subprocess
    import sys
    from gbfleet.spawn import Child
    from gbfleet.supervisor import Partition, _remember_holdings

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    child = Child(
        adapter="fake", worktree=Path("/wt"), branch="gb/w-1", base="",
        seat_path=Path("/s"), process=proc, started_at=0.0, log_dir=Path("/l"),
        agent_id="GRPH-A1", held_items=["GRPH-1"],
    )
    part = Partition()
    part.held = {"GRPH-A1": []}
    _remember_holdings([child], part)
    assert child.held_items == ["GRPH-1"]
    part.held = {"GRPH-A1": ["GRPH-2"]}
    _remember_holdings([child], part)
    assert child.held_items == ["GRPH-2"]
    import inspect
    from gbfleet import supervisor as sup
    assert "child.held_items" in inspect.getsource(sup._reap_all)


def test_item_status_is_empty_when_search_items_is_not_permitted():
    """A supervisor-only client must not crash; resume then sees {} and cuts from HEAD."""
    from gbfleet.client import ALLOWED_TOOLS
    from gbfleet.supervisor import item_status

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("search_items reached the network")

    client = Graphban(
        "http://gb.invalid", KEY,
        allowed=ALLOWED_TOOLS,
        transport=httpx.MockTransport(never),
    )
    assert item_status(client) == {}


def test_cli_up_does_not_inject_items_and_uses_spawn_reads():
    """THE CALL. `up()` used to need `items=`; the CLI never passed it."""
    import inspect
    from gbfleet import cli

    src = inspect.getsource(cli.main)
    assert "items=" not in src
    assert "allowed=SPAWN_READS" in src
    src_mcp = inspect.getsource(cli._serve_stdio)
    assert "allowed=SPAWN_READS" in src_mcp


def test_the_work_a_child_did_is_recoverable_and_carries_no_credential(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(1),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        state=state,
        workspace=workspace,
    )
    branch = wave.reaped[0].branch

    import subprocess

    kept = subprocess.run(
        ["git", "show", f"{branch}:feature.py"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert kept == "print(1)\n"

    everything = subprocess.run(
        ["git", "log", "-p", "--all"], cwd=git_repo, capture_output=True, text=True, check=True
    ).stdout
    assert CODE not in everything, "the enrolment code reached a commit"
    assert KEY not in everything, "the API key reached a commit"
    for seat_file in SEAT_FILES:
        assert seat_file not in everything


def test_nothing_carrying_a_credential_is_passed_as_an_argument(
    git_repo: Path, tmp_path: Path, scripts
):
    """argv is readable by every process on the machine.

    D-k declines to sandbox, which is a different thing from publishing a live seat to
    `ps`. Both secrets travel as 0600 files and only PATHS go on the command line.
    """
    from gbfleet.worktree import create

    tree = create(git_repo, tmp_path / "w1", "wave", "1")
    instruction = tmp_path / "instr"
    instruction.write_text("...", encoding="utf-8")

    factory = make_launch_factory("claude", [
        "claude", "--mcp-config", "{seat_file}", "-p", "{instruction_file}", "--cwd", "{worktree}"
    ])
    launch = factory(_seats(1)[0], tree, instruction)

    joined = " ".join(launch.argv)
    assert f"{CODE}-0" not in joined
    assert KEY not in joined
    assert str(tree.path) in joined
    assert str(instruction) in joined


def test_the_instruction_file_is_private_while_it_exists(git_repo: Path, tmp_path: Path):
    """The half the reap test's name claimed and did not check.

    `up` runs to completion before returning, so by then the file is gone and its mode
    is unobservable — a sabotage setting it 0644 passed. The mode matters while a child
    is running, because the file carries the enrolment code, so it is checked where it
    is written.
    """
    from gbfleet.supervisor import _instruction_file
    from gbfleet.worktree import create

    tree = create(git_repo, tmp_path / "w1", "wave", "1")
    seat = _seats(1)[0]
    path = _instruction_file(tree, seat, "wave")

    assert is_owner_only(path)
    assert seat.code in path.read_text(encoding="utf-8")
    assert is_seat_file(path, tree.path), (
        "the instruction file carries a live seat and lives in the worktree, so salvage "
        "must know to exclude it"
    )


def test_the_instruction_file_is_gone_after_reap(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(1),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        state=state,
        workspace=workspace,
    )
    assert wave.ok, wave.failures
    assert not (wave.spawned[0].worktree / ".gbfleet-instruction").exists()
    assert not wave.spawned[0].seat_path.exists()


# --- what the wave says about itself ----------------------------------------------


def test_a_proposal_of_zero_over_an_empty_roster_is_marked_uninformative(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """`workers: 0` has two opposite meanings and this is the one that must not read as
    "nothing to do" — it means the server had nobody to allocate over, which is exactly
    the state a supervisor is in at the moment it needs the answer."""
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(1),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        state=state,
        workspace=workspace,
    )
    assert wave.before is not None
    assert wave.before.workers == 0
    assert wave.before.uninformative is True

    # And having run a child, the server is now in a position to answer.
    assert wave.after is not None


def test_a_proposal_of_zero_over_a_live_roster_is_a_real_answer():
    real = AllocationRead.of(
        {
            "workers": 0,
            "reviewers": 2,
            "mapping": [{"agent": "GRPH-A1", "role": "reviewer", "cluster": []}],
            "rationale": "0 free cluster(s) for 2 agent(s)",
        }
    )
    assert real.uninformative is False


def test_seats_beyond_the_cap_are_reported_not_silently_dropped(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(5),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=2),
        state=state,
        workspace=workspace,
    )
    assert len(wave.spawned) == 2
    assert wave.unused_seats == 3, (
        "a caller would have to infer this from len(spawned), and a short list reads as "
        "'nothing went wrong'"
    )


# --- the ways a wave goes wrong ---------------------------------------------------


def test_an_unreachable_server_spawns_nothing(git_repo: Path, tmp_path: Path, scripts, state: Path):
    """D-i. A child that cannot register has no identity, no consumed seat and no claim
    — spawning one spends money to produce a process nobody can account for."""
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(3),
        _factory(scripts, "works_then_exits"),
        _server(workspace, unreachable=True),
        state=state,
        workspace=workspace,
    )
    assert wave.offline is True
    assert wave.ok is False
    assert wave.spawned == []
    assert wave.unused_seats == 3
    assert not workspace.exists() or not any(workspace.glob("wave-*"))


def test_a_launch_failure_stops_the_wave_rather_than_repeating_it(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The failures S2 describes are adapter-shaped and identical for every seat.

    Spawning three more children into three more worktrees to watch them fail the same
    way costs three more salvage branches and tells nobody anything new.
    """
    workspace = tmp_path / "ws"
    missing = tmp_path / "not-a-binary"
    wave = up(
        git_repo,
        _seats(3),
        make_launch_factory("codex", [str(missing)]),
        _server(workspace),
        state=state,
        workspace=workspace,
    )

    assert wave.ok is False
    assert wave.spawned == []
    assert len(wave.failures) == 1
    assert "codex" in wave.failures[0]
    assert wave.unused_seats == 3
    # The worktree it got as far as creating is reaped, not left behind.
    assert not any(p.is_dir() for p in workspace.glob("wave-*"))


def test_a_child_that_overruns_is_stopped_and_said_so(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The child sleeps for five minutes, so if the cap does not fire nothing ends it.

    `bounded` turns that into a failure instead of a hang, which is not decoration: a
    sabotage pass removing the wall-clock check hung for two minutes and killed the
    harness rather than reporting anything. A hanging test is not a failing test.
    """
    workspace = tmp_path / "ws"
    polls: list[float] = []

    def bounded(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) > 40:
            raise AssertionError(
                "the supervisor waited 40 polls without stopping an overrunning child — "
                "the wall-clock limit is not being enforced"
            )
        time.sleep(seconds)

    wave = up(
        git_repo,
        _seats(1),
        _factory(scripts, "sleeper"),
        _server(workspace),
        limits=Limits(child_wall_clock=0.0),
        state=state,
        workspace=workspace,
        poll=0.05,
        sleep=bounded,
    )
    assert wave.ok is False
    assert wave.spawned[0].stopped_because is Reason.WALL_CLOCK
    assert any("stopped" in f for f in wave.failures)
    # Killing cleans up nothing, so the reap that follows is what removes the worktree.
    assert wave.reaped and wave.reaped[0].removed


def test_a_child_that_cannot_write_its_handoff_fails_the_wave(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """P30 D6. Exit 70 is handoff-failed: the item is still claimed. That is a
    supervisor failure, not idle.
    """
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "exits_handoff_failed"),
        _server(workspace), limits=Limits(max_workers=1),
        state=state, workspace=workspace,
    )
    assert wave.ok is False
    assert wave.reason != "idle"
    assert any("handoff-failed" in f and "70" in f for f in wave.failures)


def test_a_stuck_give_up_is_not_a_supervisor_failure(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """P30 D6. Exit 75 is a completed give-up: evidence written, item released.
    Visible in the report, not a supervisor failure by itself.
    """
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "exits_stuck"),
        _server(workspace), limits=Limits(max_workers=1),
        state=state, workspace=workspace,
    )
    assert wave.ok is True
    assert wave.reason == "idle"
    assert wave.give_ups and any("75" in g for g in wave.give_ups)
    assert not any("75" in f for f in wave.failures)


def test_a_second_supervisor_on_the_same_repo_is_refused(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """D-h, reaching `up`: this is what makes --max-workers correct rather than
    approximate, because two supervisors would exceed it between them."""
    workspace = tmp_path / "ws"
    with hold(git_repo, state):
        with pytest.raises(RepoLocked):
            up(
                git_repo,
                _seats(1),
                _factory(scripts, "works_then_exits"),
                _server(workspace),
                state=state,
                workspace=workspace,
            )


# --- the report -------------------------------------------------------------------


def test_the_report_says_when_a_zero_meant_nothing():
    wave_out = io.StringIO()
    from gbfleet.supervisor import Wave

    wave = Wave(
        before=AllocationRead(0, 0, "no agents online — nothing to allocate", uninformative=True)
    )
    report(wave, out=wave_out)
    assert "ignorance" in wave_out.getvalue()


def test_the_report_shouts_about_a_credential_in_branch_history():
    from gbfleet.supervisor import Wave
    from gbfleet.worktree import Reaped, Salvage

    out = io.StringIO()
    wave = Wave(
        reaped=[
            Reaped(
                disposition=Disposition.SALVAGED,
                branch="gb/wave-1",
                salvage=Salvage(
                    committed=True, commit="abc", credential_in_history=[".cursor/mcp.json"]
                ),
                removed=True,
            )
        ]
    )
    report(wave, out=out)
    assert "!!" in out.getvalue()
    assert ".cursor/mcp.json" in out.getvalue()


def test_seats_are_read_from_a_file_ignoring_blanks_and_comments(tmp_path: Path):
    path = tmp_path / "seats.txt"
    path.write_text("# a wave\nWORKER-AAA\n\n  REVIEWER-BBB  \n", encoding="utf-8")
    seats = read_seats(str(path), "http://gb.invalid", KEY)
    assert [s.code for s in seats] == ["WORKER-AAA", "REVIEWER-BBB"]
    assert all(s.api_key == KEY for s in seats)
