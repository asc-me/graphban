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
from gbfleet.spawn import Child, Launch, Reason
from gbfleet.supervisor import (
    AllocationRead, Limits, Wave, WaveBase, resolve_wave_base, up,
)
from gbfleet.worktree import BaseBranchNotFound, Disposition, SEAT_FILES, Worktree, create, is_seat_file, orphans, reap, salvage_message
from gbfleet.hostos import is_owner_only  # noqa: E402

from conftest import telemetry_ack  # noqa: E402

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

        ack = telemetry_ack(request)

        if ack is not None:

            return ack

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
    assert "release_item" not in ALLOWED_TOOLS
    assert "search_items" in SPAWN_READS
    assert "release_item" in SPAWN_READS


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


def test_a_seats_line_can_bind_an_item_and_name_a_role(tmp_path: Path):
    """`until` builds Seat(item=..., role=...) in-process (PRD-36 D7). A seats file could
    say neither, so a bound seat handed to `up` produced a child that held its item AND
    was told to claim_cluster on top of it. S6: reviewer merged into worker, so the
    unified instruction teaches both claim_review and claim_cluster."""
    from gbfleet.seat import instruction_for

    path = tmp_path / "seats.txt"
    path.write_text(
        "WORKER-AAA item=GRPH-755\nREVIEWER-BBB role=reviewer\nWORKER-CCC\n", encoding="utf-8",
    )
    seats = read_seats(str(path), "http://gb.invalid", KEY)
    assert [(s.code, s.role, s.item) for s in seats] == [
        ("WORKER-AAA", "worker", "GRPH-755"),
        ("REVIEWER-BBB", "reviewer", None),
        ("WORKER-CCC", "worker", None),
    ]
    assert "BOUND to GRPH-755" in instruction_for(seats[0], tmp_path, "gb/x")
    # S6: unified instruction for all unbound seats
    assert "claim_review" in instruction_for(seats[1], tmp_path, "gb/x")
    assert "claim_cluster" in instruction_for(seats[2], tmp_path, "gb/x")


def test_up_accepts_project_because_main_reads_it():
    """GRPH-718 added `--project` to doctor, mcp and until, and `main()` passes
    `args.project` for `up` too — but `up`'s parser never got the flag, so every `up`
    since died with AttributeError after reading its seats and before any spawn. Found
    running GRPH-P39's S2 on a bound seat."""
    from gbfleet.cli import build_parser

    base = ["up", "--server", "http://gb.invalid", "--seats-file", "s.txt", "--adapter", "claude"]
    assert build_parser().parse_args(base).project == ""
    assert build_parser().parse_args(base + ["--project", "agentledger"]).project == "agentledger"


def test_main_up_prints_the_wave_report_after_a_wave(tmp_path: Path, monkeypatch, capsys):
    """`main()` bound `report = doctor.run(...)` in the doctor branch, which made `report` a
    local of the whole function, so `up`'s `report(wave)` raised UnboundLocalError AFTER the
    wave had run, reaped and pushed. Only the report was lost — the quietest possible
    failure of a supervisor — and it hid behind the missing `--project` for as long as
    that came first. This drives `main(["up", ...])` to the report line with the wave itself
    stubbed, so the parser, the env, the seats file and the tail all run for real."""
    import subprocess

    from gbfleet import cli
    from gbfleet.supervisor import Wave

    # A repository, because the build-server grant is read from its root (GRPH-998).
    subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True, check=True)
    seats = tmp_path / "seats.txt"
    seats.write_text("WORKER-AAA\n", encoding="utf-8")
    monkeypatch.setenv(cli.API_KEY_ENV, KEY)
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: (lambda *a, **k: None))

    class _Client:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli, "Graphban", _Client)
    monkeypatch.setattr(cli, "up", lambda *a, **k: Wave(reason="idle"))

    rc = cli.main(["up", "--repo", str(tmp_path), "--server", "http://gb.invalid",
                   "--seats-file", str(seats), "--adapter", "claude"])
    assert rc == 0
    assert capsys.readouterr().out.strip(), "the wave report was not printed"


def test_a_mistyped_seats_line_is_refused_at_read(tmp_path: Path):
    """Refused before any worktree exists, and the refusal quotes the token, so `itm=`
    is not read as an unbound seat that then claims whatever the divvy hands it."""
    path = tmp_path / "seats.txt"
    path.write_text("WORKER-AAA itm=GRPH-755\n", encoding="utf-8")
    with pytest.raises(ValueError, match="itm=GRPH-755"):
        read_seats(str(path), "http://gb.invalid", KEY)
    path.write_text("WORKER-AAA role=planner\n", encoding="utf-8")
    with pytest.raises(ValueError, match="planner"):
        read_seats(str(path), "http://gb.invalid", KEY)


# --- the machine is a limit too (GRPH-842) ----------------------------------------


def test_a_full_machine_stops_the_wave_and_says_which_limit_it_was(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """The gate at the launch loop, not the unit under it.

    A wave killed by the harness for memory pressure left `unused_seats` looking exactly
    like a cap, a crash or an adapter fault. Here the seats and the worker cap are both
    generous and the MACHINE is what runs out — one child fits, the rest are refused, and
    the wave says so in a field of its own.
    """
    workspace = tmp_path / "ws"
    from gbfleet import hostos, headroom

    monkeypatch.setattr(hostos, "available_memory", lambda: headroom.RESERVE)
    wave = up(
        git_repo,
        _seats(4),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=4),
        state=state,
        workspace=workspace,
    )

    assert len(wave.spawned) == 1, "the first child is never gated; the rest had no room"
    assert wave.unused_seats == 3
    assert wave.gated and "no room" in wave.gated[0]
    assert not wave.failures, "a full machine is not an adapter fault and must not read as one"
    assert wave.headroom_at_start == headroom.RESERVE


def test_an_unmeasurable_machine_spawns_exactly_as_before(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """None must not read as full. This is the whole wave the old behaviour ran."""
    workspace = tmp_path / "ws"
    from gbfleet import hostos

    monkeypatch.setattr(hostos, "available_memory", lambda: None)
    wave = up(
        git_repo,
        _seats(2),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=4),
        state=state,
        workspace=workspace,
    )
    assert len(wave.spawned) == 2
    assert wave.gated == []
    assert wave.headroom_at_start is None, (
        "and the report has to be able to tell this apart from a roomy machine"
    )


def test_a_roomy_machine_does_not_gate_anything(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    workspace = tmp_path / "ws"
    from gbfleet import hostos, headroom

    monkeypatch.setattr(
        hostos, "available_memory",
        lambda: headroom.RESERVE + 20 * headroom.DEFAULT_CHILD_MEMORY,
    )
    wave = up(
        git_repo,
        _seats(3),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=4),
        state=state,
        workspace=workspace,
    )
    assert len(wave.spawned) == 3
    assert wave.gated == []


def test_the_gate_reports_the_refusal_on_the_wave_summary(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """`3 seat(s) never redeemed` with no reason is what sent an operator hunting a bug."""
    workspace = tmp_path / "ws"
    from gbfleet import hostos, headroom

    monkeypatch.setattr(hostos, "available_memory", lambda: headroom.RESERVE)
    wave = up(
        git_repo,
        _seats(3),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=4),
        state=state,
        workspace=workspace,
    )
    out = io.StringIO()
    report(wave, out=out)
    printed = out.getvalue()
    assert "never redeemed" in printed
    assert "NO ROOM" in printed
    assert "FAILED" not in printed


def test_children_are_charged_before_the_kernel_can_see_them(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """The reading never moves. Only the charge stops the loop.

    A machine reporting room for exactly two more children, forever — which is what a real
    one does for the first seconds after a spawn, because a process that has just started
    has not allocated anything yet. Without charging each spawn against the baseline, the
    loop starts every seat it has on the strength of one stale reading, which is precisely
    how three children ended up on a box with room for one.
    """
    workspace = tmp_path / "ws"
    from gbfleet import hostos, headroom

    monkeypatch.setattr(
        hostos, "available_memory",
        lambda: headroom.RESERVE + 2 * headroom.DEFAULT_CHILD_MEMORY,
    )
    wave = up(
        git_repo,
        _seats(4),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=4),
        state=state,
        workspace=workspace,
    )
    assert len(wave.spawned) == 2
    assert wave.unused_seats == 2
    assert wave.gated


@pytest.mark.real_memory
def test_the_gate_binds_on_a_real_kernel_reading(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The whole chain against the actual kernel, not a lambda.

    Every other test here pins the reader, which proves the wiring and not the reading.
    This one lets `hostos.available_memory` run for real and makes the machine look full by
    charging an absurd amount per child — so a broken platform branch, a parser that
    returns None on this host, or a limit that never reaches the gate all show up as a
    wave that spawned four children instead of one.
    """
    from gbfleet import hostos

    if hostos.available_memory() is None:
        pytest.skip("this host cannot be asked; the gate does not bind and says so")
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(4),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=4, child_memory=1024**5),  # a petabyte per child
        state=state,
        workspace=workspace,
    )
    assert len(wave.spawned) == 1
    assert wave.gated and "no room" in wave.gated[0]
    assert wave.headroom_at_start and wave.headroom_at_start > 0


# --- GRPH-870: vendor worktrees must run [setup] ---------------------------------


def test_start_one_runs_setup_commands_for_every_adapter(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """`prepare` is called in the gbfleet spawn path, not only inside gbagent.

    A fresh `git worktree` has no generated client, no node_modules, no .venv.
    Vendor children that start in an unbuilt tree and report green tests are the
    worse outcome. The CALL — `prepare(tree.path)` in `start_one` — is what closes
    the gap. Sabotage: remove that line; this test fails because the marker file
    the setup command creates never appears.
    """
    import subprocess
    import sys
    from conftest import make_stub_script, stub_command
    from gbfleet.supervisor import Limits, Partition, start_one, _tree_for
    from gbfleet.spawn import Launch

    workspace = tmp_path / "ws"
    workspace.mkdir()

    def _git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
        ).stdout.strip()

    # A setup stub that creates a marker file.
    setup_script = make_stub_script(git_repo / "setup_mark.py", touch=(".setup-ran",))
    setup_cmd = stub_command(setup_script)

    # Commit a `.gbagent.toml` whose [setup] runs the stub.
    toml = (
        f"[tests]\ncommand = '{stub_command(make_stub_script(git_repo / 'test_stub.py'))}'\n"
        f"\n[setup]\ncommands = ['{setup_cmd}']\n"
    )
    (git_repo / ".gbagent.toml").write_text(toml, encoding="utf-8")
    _git(git_repo, "add", ".gbagent.toml", "setup_mark.py", "test_stub.py")
    _git(git_repo, "commit", "-qm", "add setup config")

    tree = _tree_for(git_repo, workspace, "wave-setup", "1")
    client = _server(workspace)

    child = start_one(
        tree,
        _seats(1)[0],
        lambda s, t, i, d: Launch(
            adapter="fake",
            argv=[str(scripts["python"]), str(scripts["exits_immediately"])],
            seat_path=t.path / SEAT_FILES[0],
            config=s.mcp_config(),
            instruction="",
        ),
        client, Limits(registration_window=30.0), Partition(),
        workspace=workspace, wave_name="wave-setup", slot="1",
    )
    # The marker file was created by `prepare`, not by the child process (which
    # exits immediately). If `prepare` is not called, this file does not exist.
    assert (tree.path / ".setup-ran").exists(), (
        "setup commands were not run in the worktree — prepare() is missing "
        "from the gbfleet spawn path"
    )


def test_a_failing_setup_refuses_the_wave_slot(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """A setup that fails stops the wave, exactly like a broken adapter.

    `SetupFailed` ⊂ `ConfigRefused`. The `_start` except clause catches it alongside
    `LaunchFailed`, reaps the empty worktree, and records the failure. A wave that
    continued past a failed setup would spawn children into unbuilt trees — the
    exact outcome GRPH-870 exists to prevent.
    """
    import subprocess
    import sys
    from conftest import make_stub_script, stub_command
    from gbfleet.supervisor import Limits, up

    workspace = tmp_path / "ws"

    def _git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
        ).stdout.strip()

    # A setup stub that always fails.
    fail_script = make_stub_script(git_repo / "setup_fail.py", exit_code=1)
    fail_cmd = stub_command(fail_script)
    test_script = make_stub_script(git_repo / "test_stub.py")
    test_cmd = stub_command(test_script)

    toml = (
        f"[tests]\ncommand = '{test_cmd}'\n"
        f"\n[setup]\ncommands = ['{fail_cmd}']\n"
    )
    (git_repo / ".gbagent.toml").write_text(toml, encoding="utf-8")
    _git(git_repo, "add", ".gbagent.toml", "setup_fail.py", "test_stub.py")
    _git(git_repo, "commit", "-qm", "add failing setup")

    wave = up(
        git_repo,
        _seats(2),
        _factory(scripts, "exits_immediately"),
        _server(workspace),
        limits=Limits(max_workers=4),
        state=state,
        workspace=workspace,
    )
    assert wave.failures, "a failing setup should record a failure"
    assert any("setup" in f.lower() or "exited" in f.lower() for f in wave.failures), (
        f"failure should name the setup: {wave.failures}"
    )
    assert len(wave.spawned) == 0, "no child should have started past a failed setup"


# --- GRPH-1012: the base a child is cut from is FETCHED, never inherited from HEAD -------


def _g(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True).stdout.strip()


def _cloned_repo(tmp_path: Path, *, extra_branch: str = "") -> Path:
    """A CLONE of a real remote, left checked out on a feature branch.

    Cloned rather than `init` + `remote add`, because `default_ref` reads the
    `refs/remotes/origin/HEAD` symref and only a clone writes one. A fixture built the other
    way has no default ref at all, so it exercises the no-remote fallback while looking like
    it exercises the third rule — a test that passes for the wrong reason and would keep
    passing if the rule broke.

    The checkout is left standing on `feature`, one commit past `origin/main`, so "cut from
    HEAD" and "cut from the fetched base" are different SHAs and an assertion can tell them
    apart. `tmp_path / "seed"` is the other clone, for tests that move the remote AFTER this
    one last fetched.
    """
    import subprocess

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                   check=True, capture_output=True, text=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _g(seed, "init", "-q", "-b", "main")
    _g(seed, "config", "user.email", "t@t.t")
    _g(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("x\n", encoding="utf-8")
    _g(seed, "add", "-A")
    _g(seed, "commit", "-qm", "first")
    _g(seed, "remote", "add", "origin", str(bare))
    _g(seed, "push", "-q", "origin", "main")
    if extra_branch:
        _g(seed, "checkout", "-q", "-b", extra_branch)
        (seed / f"{extra_branch}.txt").write_text("stacked\n", encoding="utf-8")
        _g(seed, "add", "-A")
        _g(seed, "commit", "-qm", f"{extra_branch} base")
        _g(seed, "push", "-q", "origin", extra_branch)
        _g(seed, "checkout", "-q", "main")

    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(repo)],
                   check=True, capture_output=True, text=True)
    _g(repo, "config", "user.email", "t@t.t")
    _g(repo, "config", "user.name", "t")
    _g(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("wip\n", encoding="utf-8")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "feature work nobody merged")
    return repo


class _Ledger:
    """Answers only `get_context`, the one call step 2 of the three rules makes.

    Duck-typed deliberately: `resolve_wave_base` takes any holder that can ask, and WHICH
    holders may ask is the allowlists' business, pinned by name below.
    """

    def __init__(self, gitops: dict | None = None, raises: Exception | None = None):
        self.gitops = gitops if gitops is not None else {}
        self.raises = raises
        self.asked: list[str] = []

    def call(self, tool: str, **_kw) -> dict:
        self.asked.append(tool)
        if self.raises is not None:
            raise self.raises
        return {"project_id": "core", "gitops": self.gitops}


def test_the_supervisors_own_table_stays_two_reads_and_get_context_goes_on_the_wider_sets():
    """The GRPH-1012 read is on `PLANNER_TOOLS` and `SPAWN_READS`, and NOT on the third.

    Dropping it from `SPAWN_READS` is the quiet failure and the reason this asserts all
    three sets: `resolve_wave_base` treats a refused call as an unreadable gitops, so every
    project's measured trunk would silently read as unmeasured and every wave would fall
    through to the remote default — a clean-looking report of a rule that never ran.
    """
    from gbfleet.cli import SPAWN_READS
    from gbfleet.client import ALLOWED_TOOLS
    from gbfleet.until import PLANNER_TOOLS

    assert ALLOWED_TOOLS == frozenset({"fleet_status", "propose_allocation"}), (
        "PRD-22 §4: the supervisor decides how many children of an authorised kind to run, "
        "and reads nothing else"
    )
    assert "get_context" in SPAWN_READS
    assert "get_context" in PLANNER_TOOLS
    assert ALLOWED_TOOLS < SPAWN_READS


def test_a_child_is_cut_from_the_fetched_default_ref_and_not_from_the_checkout_head(
    tmp_path: Path, scripts, state: Path,
):
    """THE DEFECT, end to end through `up`. This checkout is on `feature`; the base is not.

    `up` used to hand `_tree_for` an empty base, which is `wt_mod.create`'s HEAD default, so
    every child inherited whatever branch the supervisor happened to be standing on. Measured
    as seven `gbfleet mcp` processes on one repository, the lock holder's checkout four
    commits ahead of the trunk on a feature branch, and a second clone whose `origin/main`
    had never been fetched and so looked current to itself.

    Sabotage: drop `wave_base`/`base_branch` from `up`'s call to `resolve_wave_base`, or
    hand `_tree_for` no base → the child's base becomes the feature commit and this fails.
    """
    repo = _cloned_repo(tmp_path)
    workspace = tmp_path / "ws"
    main_sha = _g(repo, "rev-parse", "origin/main")
    head_sha = _g(repo, "rev-parse", "HEAD")
    assert main_sha != head_sha, "the fixture has to make the two answers different"

    wave = up(
        repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
        limits=Limits(max_workers=1), state=state, workspace=workspace,
    )
    assert wave.spawned, wave.failures
    assert wave.spawned[0].base == main_sha, (
        f"child was cut from {wave.spawned[0].base[:12]}, origin/main is {main_sha[:12]} "
        f"and this checkout's HEAD is {head_sha[:12]}"
    )
    assert wave.spawned[0].base != head_sha
    assert wave.base is not None
    assert wave.base.source == "remote_default"
    assert wave.base.ref == "origin/main"


def test_an_explicit_base_beats_a_measured_project_rule(tmp_path: Path):
    """Rule 1 before rule 2: the operator's `--base` is the loudest voice in the room."""
    repo = _cloned_repo(tmp_path, extra_branch="integration")
    client = _Ledger({"base_branch": {"value": "integration", "source": "project"}})

    got = resolve_wave_base(repo, client, base_branch="main")

    assert got.source == "flag"
    assert got.ref == "origin/main"
    assert "--base main" in got.note
    assert client.asked == [], "rule 1 answered, so the ledger was never asked"


def test_a_measured_project_rule_beats_the_remotes_default_ref(tmp_path: Path):
    """Rule 2. A project that has stated its trunk is stating where its work lands."""
    repo = _cloned_repo(tmp_path, extra_branch="integration")
    client = _Ledger({"base_branch": {"value": "integration", "source": "org"}})

    got = resolve_wave_base(repo, client)

    assert got.source == "gitops"
    assert got.ref == "origin/integration"
    assert _g(repo, "rev-parse", got.ref) == _g(repo, "rev-parse", "origin/integration")
    assert "source org" in got.note, "the note has to say whose rule it was"


def test_an_unmeasured_project_rule_reads_as_unmeasured_and_not_as_main(tmp_path: Path):
    """Rule 3, and the absence rule that makes it honest.

    `unmeasured` means the project stated no base. That is not `main`, and a wave summary
    that said "cut from origin/main" without saying nobody chose it would report a decision
    nobody made. Three states, three notes: an empty gitops, an explicit unmeasured field,
    and no client at all to ask.
    """
    repo = _cloned_repo(tmp_path)
    main_sha = _g(repo, "rev-parse", "origin/main")

    for client in (_Ledger(), _Ledger({"base_branch": {"value": None,
                                                      "source": "unmeasured"}})):
        got = resolve_wave_base(repo, client)
        assert got.source == "remote_default"
        assert _g(repo, "rev-parse", got.ref) == main_sha
        assert "unmeasured" in got.note

    nothing = resolve_wave_base(repo, None)
    assert nothing.source == "remote_default"
    assert "not asked" in nothing.note, "no client is its own state, not an unmeasured one"


def test_an_unreadable_get_context_reads_as_unreadable_and_not_as_unmeasured(tmp_path: Path):
    """The other half of the absence rule: could not ask ≠ nobody answered.

    Both fall through to the remote's default ref, so the wave runs either way — and that is
    exactly why the note has to keep them apart. An operator reading "unmeasured" goes and
    measures the project's git process; one reading "unreadable" goes and looks at the
    server. The wrong word sends them to the wrong place.
    """
    repo = _cloned_repo(tmp_path)

    got = resolve_wave_base(repo, _Ledger(raises=RuntimeError("server on fire")))

    assert got.ref == "origin/main"
    assert got.source == "remote_default"
    assert "unreadable" in got.note
    assert "server on fire" in got.note
    assert "unmeasured" not in got.note


def test_the_default_ref_is_fetched_and_not_just_read(tmp_path: Path):
    """A remote-tracking ref is only as fresh as the last fetch.

    This clone fetched at clone time and the remote moved since. Resolving without fetching
    would cut every child from a commit that is no longer the trunk, and the reviewer's diff
    would be against a world that does not exist any more.

    Sabotage: delete the `refresh_ref` call in `resolve_wave_base` → the ref stays at the
    stale sha and this fails.
    """
    repo = _cloned_repo(tmp_path)
    seed = tmp_path / "seed"
    (seed / "later.txt").write_text("landed\n", encoding="utf-8")
    _g(seed, "add", "-A")
    _g(seed, "commit", "-qm", "landed on main after the clone fetched")
    _g(seed, "push", "-q", "origin", "main")
    stale = _g(repo, "rev-parse", "origin/main")

    got = resolve_wave_base(repo, _Ledger())

    assert _g(repo, "rev-parse", got.ref) == _g(seed, "rev-parse", "main") != stale
    assert "fetched" in got.note


def test_a_fetch_that_failed_is_reported_as_stale_and_not_as_current(tmp_path: Path,
                                                                    monkeypatch):
    """`refresh_ref` answers False when the remote cannot be reached. That is a fact.

    Reporting the ref anyway is right — a wave offline still has to cut from something — but
    calling it current would be the absence reading as clean, in the one check built to stop
    it.
    """
    from gbfleet import worktree as wt_mod

    repo = _cloned_repo(tmp_path)
    monkeypatch.setattr(wt_mod, "refresh_ref", lambda *a, **k: False)

    got = resolve_wave_base(repo, _Ledger())

    assert got.ref == "origin/main"
    assert "FETCH FAILED" in got.note


def test_a_measured_base_the_remote_does_not_have_refuses_instead_of_falling_back(
    tmp_path: Path,
):
    """A project rule naming a branch this remote does not have is a misconfiguration.

    Falling past it to the default ref would put every child of the wave on a base the
    project never chose, and the wave summary would read as a normal wave. Refusing names
    the branch, says whose rule it was, and points at `--base` as the way past.
    """
    repo = _cloned_repo(tmp_path)

    with pytest.raises(BaseBranchNotFound) as excinfo:
        resolve_wave_base(repo, _Ledger({"base_branch": {"value": "release-9",
                                                         "source": "project"}}))

    text = str(excinfo.value)
    assert "release-9" in text
    assert "source project" in text
    assert "--base" in text


def test_a_repository_with_no_remote_says_it_is_cutting_from_head(git_repo: Path):
    """The one case where HEAD is the answer, and it is reported as a fact about the repo.

    Not an error and not a silent default: `source == "head"` is what tells an operator that
    this wave's children are built on whatever the checkout was standing on. The ledger is
    not even asked — with no remote there is nothing for a measured base to resolve against.
    """
    client = _Ledger({"base_branch": {"value": "integration", "source": "project"}})

    got = resolve_wave_base(git_repo, client)

    assert got.ref == ""
    assert got.source == "head"
    assert "no remote" in got.note
    assert client.asked == []


def test_mcp_spawn_resolves_a_base_before_it_cuts_a_worktree():
    """THE CALL, not only the callee (GRPH-1012).

    `resolve_wave_base` can be perfectly correct and `spawn` can still hand `_tree_for`
    nothing, which is what it did before: the resolver did not exist, and the call site is
    the half that decides what a child is built on. Pinned as source because the behavioural
    spawn harness lives in `test_mcp.py` and this assertion is about the wiring, not about
    registration.

    Sabotage: drop `base=fleet.base.ref` from the `_tree_for` call, or delete the lazy
    resolve → one of these two assertions fails.
    """
    import inspect

    from gbfleet import mcp as mcp_mod

    src = inspect.getsource(mcp_mod.call_tool)
    assert "fleet.base = resolve_wave_base(fleet.repo, fleet.client)" in src, (
        "spawn must resolve the base itself when startup did not"
    )
    assert "_tree_for(fleet.repo, fleet.workspace, wave, slot, base=fleet.base.ref)" in src, (
        "spawn must cut the worktree from the resolved base, not from _tree_for's HEAD default"
    )
    assert 'described["base"]' in src, "the reply has to say which ref and which rule"


def test_the_wave_report_names_the_base_and_which_rule_chose_it():
    """The summary line. `origin/main` alone cannot say whether the project chose it."""
    out = io.StringIO()
    report(Wave(base=WaveBase(
        ref="origin/main", source="remote_default",
        note="gitops.base_branch unmeasured (source unmeasured), so this cuts from "
             "origin's default ref origin/main, fetched")), out=out)

    text = out.getvalue()
    assert "BASE origin/main" in text
    assert "[remote_default]" in text
    assert "unmeasured" in text, "the note has to keep saying nobody measured a base"


def test_a_wave_that_cut_children_without_a_recorded_base_says_so(tmp_path: Path):
    """`wave.base is None` on a wave that spawned is a wave nobody can explain afterwards.

    And the quiet half: a wave that spawned nothing never cut a worktree, so it prints
    nothing. Claiming an unresolved base for a wave that built no children is the same
    defect wearing the other clothes — a line an operator learns to skip.
    """
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        child = Child(adapter="fake", worktree=tmp_path, branch="gb/w-1", base="deadbeef",
                      seat_path=tmp_path / "seat", process=proc, started_at=time.time(),
                      log_dir=tmp_path)
        out = io.StringIO()
        report(Wave(spawned=[child]), out=out)
        assert "BASE unrecorded" in out.getvalue()
    finally:
        proc.wait(timeout=30)

    quiet = io.StringIO()
    report(Wave(), out=quiet)
    assert "BASE" not in quiet.getvalue()

