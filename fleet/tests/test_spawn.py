"""Starting a child on a server-issued seat, and ending it.

Real processes throughout. The failures that matter here — a full pipe wedging a child,
a kill that leaves the vendor's helper processes running, a child that runs forever
without ever registering — are all things a mock would happily pretend did not exist.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from gbfleet import seat as seat_mod
from gbfleet.seat import INSTRUCTION, Seat, WouldDeclareParentage, instruction_for
from gbfleet.spawn import (
    Child,
    Launch,
    LaunchFailed,
    Reason,
    await_registration,
    spawn,
    stop,
)
from gbfleet.worktree import create
from gbfleet.hostos import is_owner_only  # noqa: E402

SEAT = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="gbk_test")


def _launch(scripts: dict, which: str, seat_path: Path, adapter: str = "fake") -> Launch:
    return Launch(
        adapter=adapter,
        argv=[str(scripts["python"]), str(scripts[which])],
        seat_path=seat_path,
        config=SEAT.mcp_config(),
        instruction=instruction_for(SEAT, Path("/tmp/wt"), "gb/w-1"),
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _roster(*agents: dict):
    return lambda: {"agents": list(agents)}


# --- the seat -------------------------------------------------------------------


def test_the_seat_config_is_written_private(tmp_path: Path):
    path = seat_mod.write(tmp_path / "mcp.json", SEAT.mcp_config())
    assert is_owner_only(path)

    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["graphban"]["headers"]["X-API-Key"] == "gbk_test"
    assert config["mcpServers"]["graphban"]["url"] == "https://gb.invalid/api/mcp"


def test_an_existing_seat_file_is_tightened_not_inherited(tmp_path: Path):
    path = tmp_path / "mcp.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    seat_mod.write(path, SEAT.mcp_config())
    assert is_owner_only(path)


@pytest.mark.parametrize(
    "config",
    [
        {"parent_agent_id": "GRPH-A1"},
        {"mcpServers": {"graphban": {"parent_agent_id": "GRPH-A1"}}},
        {"a": [{"b": {"parentAgentId": "GRPH-A1"}}]},
    ],
)
def test_a_config_that_declares_parentage_is_refused_at_any_depth(tmp_path: Path, config):
    """D-b. Nothing the supervisor hands a child may make it claim a parent — a spawned
    child is a separate process, and `independent()` treats siblings under one parent as
    one call tree."""
    with pytest.raises(WouldDeclareParentage):
        seat_mod.write(tmp_path / "mcp.json", config)
    assert not (tmp_path / "mcp.json").exists()


def test_the_child_is_told_it_has_no_parent():
    """The supervisor's half of D-b, and it has to be explicit.

    `register_agent`'s own schema describes `parent_agent_id` as "Set if you are a
    SUBAGENT: who spawned you" — and a child launched by a supervisor has an obvious,
    wrong answer to that. Saying nothing would leave the schema's invitation standing.
    """
    text = instruction_for(SEAT, Path("/repo/wt-1"), "gb/wave-1-grph-a1")
    assert "parent_agent_id" in text and "Do NOT set" in text
    assert "WORKER-7F3K" in text
    assert "/repo/wt-1" in text and "gb/wave-1-grph-a1" in text
    # D-c: exit on empty is the normal end of a run.
    assert "wait_seconds=0" in text and "EXIT" in text


def test_removing_a_seat_says_whether_there_was_one(tmp_path: Path):
    path = tmp_path / "mcp.json"
    assert seat_mod.remove(path) is False
    seat_mod.write(path, SEAT.mcp_config())
    assert seat_mod.remove(path) is True
    assert not path.exists()


# --- launching ------------------------------------------------------------------


def _await_lines(log_dir: Path, count: int, timeout: float = 20.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = (log_dir / "stdout.log").read_text(errors="replace").splitlines()
        if len(lines) >= count:
            return lines
        time.sleep(0.05)
    raise AssertionError(f"child produced {count} lines of stdout in time")  # pragma: no cover


def test_spawn_starts_the_child_in_its_own_worktree_with_its_own_environment(
    git_repo: Path, tmp_path: Path, scripts, log_dir: Path
):
    """The child reports its own cwd rather than the test reading /proc.

    D-k claims exactly one confinement and no more: the child's cwd is its own
    worktree, so a bad worker cannot reach another's files. That is the claim, so it is
    the child's answer that has to be checked — and /proc does not exist on macOS,
    where the obvious version compares a value against itself and cannot fail.
    """
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    launch = _launch(scripts, "says_where_it_is", wt.path / ".cursor" / "mcp.json")
    launch = Launch(**{**launch.__dict__, "env": {"GBFLEET_PROBE": "handed-through"}})
    child = spawn(launch, wt.path, wt.branch, log_dir)
    try:
        cwd, probe = _await_lines(log_dir, 2)[:2]
        assert Path(cwd).resolve() == wt.path.resolve()
        assert probe == "handed-through"
        assert child.seat_path.exists()
        assert child.worktree == wt.path
    finally:
        stop(child, Reason.SHUTDOWN)


def test_a_binary_that_does_not_exist_names_the_adapter(tmp_path: Path, log_dir: Path):
    launch = Launch(
        adapter="cursor-agent",
        argv=[str(tmp_path / "not-a-thing")],
        seat_path=tmp_path / "mcp.json",
        config=SEAT.mcp_config(),
        instruction="",
    )
    with pytest.raises(LaunchFailed) as exc:
        spawn(launch, tmp_path, "gb/w", log_dir)
    assert "cursor-agent" in str(exc.value)


def test_a_chatty_child_does_not_wedge_on_a_full_pipe(tmp_path: Path, scripts, log_dir: Path):
    """A pipe nobody drains blocks the writer at ~64KB.

    The child here writes a megabyte to stderr and then keeps going. On pipes it would
    stop at the buffer and sit there forever, looking exactly like a child thinking hard
    about something — and `await_registration` would eventually kill it and blame the
    adapter for a fault the supervisor caused.
    """
    child = spawn(_launch(scripts, "very_chatty", tmp_path / "mcp.json"), tmp_path, "gb/w", log_dir)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if "still here" in (log_dir / "stdout.log").read_text(errors="replace"):
                break
            time.sleep(0.1)
        else:  # pragma: no cover
            pytest.fail("child never got past writing to stderr — it wedged")

        assert child.running
        assert (log_dir / "stderr.log").stat().st_size >= 1_000_000
    finally:
        stop(child, Reason.SHUTDOWN)


# --- registration ---------------------------------------------------------------


def test_registration_is_matched_on_the_worktree_and_timed(
    git_repo: Path, tmp_path: Path, scripts, log_dir: Path
):
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    child = spawn(_launch(scripts, "sleeper", tmp_path / "mcp.json"), wt.path, wt.branch, log_dir)
    try:
        agent = await_registration(
            child,
            _roster(
                {"id": "GRPH-A9", "worktree": "/somebody/else"},
                {"id": "GRPH-A1", "worktree": str(wt.path)},
            ),
        )
        assert agent["id"] == "GRPH-A1"
        assert child.agent_id == "GRPH-A1"
        assert child.registration_latency is not None and child.registration_latency >= 0
    finally:
        stop(child, Reason.SHUTDOWN)


def test_a_child_that_dies_before_registering_names_the_adapter_and_its_stderr(
    tmp_path: Path, scripts, log_dir: Path
):
    """D-a: the planner must be able to tell *your adapter is broken* from *the
    supervisor is gone*, so the failure carries the adapter, the exit code and what the
    child actually said."""
    child = spawn(
        _launch(scripts, "exits_badly", tmp_path / "mcp.json", adapter="codex"),
        tmp_path,
        "gb/w",
        log_dir,
    )
    with pytest.raises(LaunchFailed) as exc:
        await_registration(child, _roster(), window=10, poll=0.05)

    message = str(exc.value)
    assert "codex" in message
    assert "3" in message
    assert "adapter blew up" in message


def test_a_child_that_never_registers_is_killed_inside_the_window(
    tmp_path: Path, scripts, log_dir: Path
):
    """The silent drop. A process that runs, burns money and produces nothing, while the
    roster simply shows one agent fewer than expected."""
    child = spawn(
        _launch(scripts, "sleeper", tmp_path / "mcp.json", adapter="grok"), tmp_path, "gb/w", log_dir
    )
    slept: list[float] = []

    def bounded(seconds: float) -> None:
        """Turn "never gives up" into a failure instead of a hang.

        Sleeps for real, because the deadline is wall-clock: a no-op sleep spins fast
        enough that two hundred iterations pass in about a millisecond, and the bound
        would fire long before the window it is meant to be testing. Then bounds the
        count, because deleting the deadline check otherwise leaves an infinite loop —
        and a hanging test is not a failing test. A sabotage pass spent five minutes
        finding that out.
        """
        slept.append(seconds)
        if len(slept) > 100:
            raise AssertionError(
                "await_registration polled 100 times without giving up — "
                "the registration deadline is not being enforced"
            )
        time.sleep(seconds)

    with pytest.raises(LaunchFailed) as exc:
        await_registration(child, _roster(), window=0.2, poll=0.05, sleep=bounded)

    assert "grok" in str(exc.value)
    assert child.stopped_because is Reason.NEVER_REGISTERED
    assert not child.running
    assert slept, "never actually polled"


def test_the_window_is_seconds_not_the_seats_thirty_minutes():
    from gbfleet.spawn import REGISTRATION_WINDOW

    assert REGISTRATION_WINDOW <= 300, (
        "a registration window near the seat's 30-minute TTL cannot tell a broken "
        "adapter from a slow start, which is the one thing it exists to do"
    )


# --- stopping -------------------------------------------------------------------


def test_stop_is_gentle_first(tmp_path: Path, scripts, log_dir: Path):
    """The child has to be able to TELL which signal it got.

    A sabotage that sent SIGKILL first survived the earlier version of this test,
    because a plain sleeper dies to both signals and `escalated` stays False either
    way. The assertion was about the supervisor's bookkeeping when it needed to be
    about what the child received — a vendor CLI that is killed outright loses whatever
    it was flushing.
    """
    marker = tmp_path / "signal-seen"
    launch = Launch(
        adapter="fake",
        argv=[str(scripts["python"]), str(scripts["notes_sigterm"]), str(marker)],
        seat_path=tmp_path / "mcp.json",
        config=SEAT.mcp_config(),
        instruction="",
    )
    child = spawn(launch, tmp_path, "gb/w", log_dir)
    _await_lines(log_dir, 1)

    result = stop(child, Reason.ASKED, grace=10)

    assert result.escalated is False
    assert result.reason is Reason.ASKED
    assert not child.running
    assert result.exit_code == 0, "the child did not get to exit on its own terms"
    assert marker.read_text(encoding="utf-8") == "sigterm", (
        "the child was killed outright rather than asked to stop"
    )


def test_a_child_that_ignores_sigterm_is_killed(tmp_path: Path, scripts, log_dir: Path):
    child = spawn(_launch(scripts, "ignores_sigterm", tmp_path / "mcp.json"), tmp_path, "gb/w", log_dir)
    deadline = time.monotonic() + 20
    while "ready" not in (log_dir / "stdout.log").read_text(errors="replace"):
        assert time.monotonic() < deadline, "child never started"  # pragma: no cover
        time.sleep(0.05)

    result = stop(child, Reason.SCALED_DOWN, grace=1)
    assert result.escalated is True
    assert not child.running


def test_stopping_a_child_takes_its_helpers_with_it(tmp_path: Path, scripts, log_dir: Path):
    """Signals go to the process GROUP.

    A vendor CLI spawning its own helpers is the normal case, and killing only the
    leader leaves them running — still holding a seat, still doing work nobody is
    watching, and invisible to a supervisor that thinks it stopped them.
    """
    child = spawn(_launch(scripts, "spawns_a_helper", tmp_path / "mcp.json"), tmp_path, "gb/w", log_dir)
    deadline = time.monotonic() + 20
    helper = ""
    while not helper:
        assert time.monotonic() < deadline, "helper pid never appeared"  # pragma: no cover
        helper = (log_dir / "stdout.log").read_text(errors="replace").strip()
        time.sleep(0.05)
    helper_pid = int(helper.splitlines()[0])
    assert _alive(helper_pid)

    stop(child, Reason.SHUTDOWN, grace=2)

    deadline = time.monotonic() + 10
    while _alive(helper_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(helper_pid), f"helper {helper_pid} survived its parent being stopped"


def test_stopping_cleans_up_nothing(git_repo: Path, tmp_path: Path, scripts, log_dir: Path):
    """Every path into `stop` is a path where something already went sideways, and
    tidying at that moment is how uncommitted work disappears. Salvage happens at reap."""
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    (wt.path / "unfinished.py").write_text("half a thought\n", encoding="utf-8")
    seat_path = wt.path / ".cursor" / "mcp.json"
    child = spawn(_launch(scripts, "sleeper", seat_path), wt.path, wt.branch, log_dir)

    stop(child, Reason.LEASE_LAPSED)

    assert seat_path.exists(), "stop removed the seat file"
    assert wt.path.exists(), "stop removed the worktree"
    assert (wt.path / "unfinished.py").read_text(encoding="utf-8") == "half a thought\n"


def test_stopping_something_already_gone_is_not_an_error(tmp_path: Path, scripts, log_dir: Path):
    """A worker exiting on empty is the normal end of its life (D-c), so the supervisor
    will routinely try to stop something that already left."""
    child = spawn(_launch(scripts, "exits_badly", tmp_path / "mcp.json"), tmp_path, "gb/w", log_dir)
    child.process.wait(timeout=20)

    result = stop(child, Reason.SHUTDOWN)
    assert result.exit_code == 3
    assert result.escalated is False


def test_fleet_idle_is_not_a_reason_to_kill():
    """D-d lists exactly four. The worker exits itself on empty per D-c, and two things
    owning one transition is how they come to disagree about it."""
    values = {r.value for r in Reason}
    assert "fleet_idle" not in values
    assert {"asked", "lease_lapsed", "scaled_down", "shutdown"} <= values


SUPERVISOR = """
import sys, time
from pathlib import Path
from gbfleet.spawn import Launch, Reason, spawn, stop

worktree, log_dir, script, python = (Path(p) for p in sys.argv[1:5])
child = spawn(
    Launch(
        adapter="fake",
        argv=[str(python), str(script)],
        seat_path=worktree / "mcp.json",
        config={"mcpServers": {}},
        instruction="",
    ),
    worktree,
    "gb/w",
    log_dir,
)
result = stop(child, Reason.SHUTDOWN, grace=5)
print(f"supervisor survived; child exit={result.exit_code}", flush=True)
"""


def test_stopping_a_child_does_not_kill_the_supervisor(tmp_path: Path, scripts, log_dir: Path):
    """`stop` signals a process GROUP, so which group the child is in decides who dies.

    Without `start_new_session=True` the child inherits the supervisor's process group,
    and `killpg` reaches the supervisor, its other children, and whatever launched it.
    Found the hard way: two sabotage runs died with no output at all before I worked out
    that the mutation had killed the harness running it.

    The supervisor under test gets its own session so THIS test is never in the blast
    radius — the point is to observe a suicide, not to share in one.
    """
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(SUPERVISOR, encoding="utf-8")

    result = subprocess.run(
        [
            str(scripts["python"]),
            str(supervisor),
            str(tmp_path),
            str(log_dir),
            str(scripts["sleeper"]),
            str(scripts["python"]),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,
    )

    assert result.returncode == 0, (
        "the supervisor did not survive stopping its own child "
        f"(returncode {result.returncode}, negative means it was signalled)\n{result.stderr}"
    )
    assert "supervisor survived" in result.stdout


def test_a_child_that_registered_and_exited_at_once_is_not_called_broken(
    git_repo: Path, tmp_path: Path, scripts, log_dir: Path
):
    """The defect the acceptance walk found on its third step.

    D-c says exiting on empty is the NORMAL end of a worker's life: it claims with
    `wait_seconds=0`, works what it got, and leaves. A fast one — register, find nothing,
    exit — can be gone before the supervisor's first poll. Checking liveness before the
    roster reported that child as `exited 0 before registering`, which sends the operator
    to look at the vendor for a fault that never happened.

    No mock caught it: mocked children sleep, and mocked rosters always answer.
    """
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    child = spawn(
        _launch(scripts, "exits_immediately", tmp_path / "mcp.json"), wt.path, wt.branch, log_dir
    )
    child.process.wait(timeout=20)
    assert not child.running, "the child has to be gone for this to test anything"

    agent = await_registration(
        child,
        _roster({"id": "GRPH-A1", "worktree": str(wt.path), "enrolment_id": "seat-1"}),
        window=5,
        poll=0.05,
    )

    assert agent["id"] == "GRPH-A1"
    assert child.agent_id == "GRPH-A1"
    assert child.registration_latency is not None
    assert child.stopped_because is None, "a worker that did its job was not stopped"


def test_a_child_that_exited_without_registering_is_still_called_broken(
    git_repo: Path, tmp_path: Path, scripts, log_dir: Path
):
    """The control. Asking the roster first must not turn the silent drop into a pass —
    that is the failure S2 exists to make loud."""
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    child = spawn(
        _launch(scripts, "exits_badly", tmp_path / "mcp.json", adapter="codex"),
        wt.path, wt.branch, log_dir,
    )
    child.process.wait(timeout=20)

    with pytest.raises(LaunchFailed) as exc:
        await_registration(child, _roster(), window=5, poll=0.05)
    assert "codex" in str(exc.value) and "3" in str(exc.value)
