"""GRPH-576: the platform differences, and what a POSIX host can honestly check.

**Read this before trusting a green run here.** Most of `hostos` has two branches and
this machine can only execute one of them. A passing suite on macOS or Linux says
nothing whatever about the Windows path — that was verified separately, on the box,
with `scripts/verify_hostos_windows.py`, and its output is recorded on GRPH-576.

What IS checked here, and is worth checking:

* the POSIX branch still does what it did before the port (the rest of the suite covers
  this too — 572 tests that never knew `hostos` existed);
* the decisions that are expressible as data rather than as code paths, above all
  `JOB_LIMIT_FLAGS == 0`, which is untestable off Windows and destructive if wrong;
* that the two branches agree about their own shape, so a Windows host cannot silently
  take a POSIX-shaped path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet import hostos  # noqa: E402
from gbfleet.hostos import (  # noqa: E402
    AlreadyLocked,
    ProcessTree,
    lock_exclusive,
    read_at,
    spawn_kwargs,
    user_tag,
    write_at,
)

posix_only = pytest.mark.skipif(hostos.WINDOWS, reason="POSIX branch")


# --- the decision that cannot be tested where it applies ---------------------------

def test_the_job_object_does_not_kill_children_when_the_supervisor_dies():
    """`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is 0x2000 and works — it was measured. It is
    not used, and this is the only place that can say so from a POSIX host.

    With it set, a supervisor crash kills every child instantly. `lock.py`'s `Takeover`
    exists so a new supervisor can ADOPT those children instead, and salvage recovers
    their uncommitted work at reap. The flag would delete a worker's work on a crash,
    on Windows only, and no test on any platform would go red.
    """
    assert hostos.JOB_LIMIT_FLAGS == 0, (
        "a job limit flag was set on children's job objects. If it is "
        "KILL_ON_JOB_CLOSE (0x2000), a supervisor crash now destroys every child's "
        "uncommitted work on Windows, and the adoption path in lock.py is dead code."
    )


def test_the_lock_byte_is_past_the_holder_record():
    """`lock.py` reads 4096 bytes from offset 0 to find out who holds the lock. Windows
    locking is MANDATORY, so if the lock sat inside that range the refused process could
    not read it, and `RepoLocked` would stop being able to name anyone — on Windows
    only, while the POSIX tests stayed green."""
    assert hostos._LOCK_BYTE >= 4096


# --- positional IO -----------------------------------------------------------------

def test_read_at_and_write_at_round_trip(tmp_path: Path):
    path = tmp_path / "f"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        write_at(fd, b"holder-record", 0)
        assert read_at(fd, 4096, 0).rstrip(b"\x00") == b"holder-record"
    finally:
        os.close(fd)


def test_write_at_does_not_depend_on_the_file_offset(tmp_path: Path):
    """The Windows implementation seeks, so a caller that had moved the offset would
    otherwise write somewhere else entirely."""
    path = tmp_path / "f"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        write_at(fd, b"AAAA", 0)
        os.lseek(fd, 3000, os.SEEK_SET)
        write_at(fd, b"BBBB", 0)
        assert read_at(fd, 4, 0) == b"BBBB"
    finally:
        os.close(fd)


# --- the lock ----------------------------------------------------------------------

def test_a_second_holder_is_refused(tmp_path: Path):
    path = tmp_path / "lock"
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        lock_exclusive(first)
        with pytest.raises(AlreadyLocked):
            lock_exclusive(second)
    finally:
        os.close(first)
        os.close(second)


def test_the_record_is_readable_while_the_lock_is_held(tmp_path: Path):
    """The property that makes `RepoLocked`'s message possible. Trivially true under
    advisory POSIX locking; the reason `_LOCK_BYTE` exists at all under mandatory
    Windows locking."""
    path = tmp_path / "lock"
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        write_at(first, b'{"pid": 42}', 0)
        lock_exclusive(first)
        assert b'"pid": 42' in read_at(second, 4096, 0)
    finally:
        os.close(first)
        os.close(second)


def test_the_lock_is_released_when_the_holder_dies(tmp_path: Path):
    """The kernel liveness check the whole design rests on: no stale-lock state, no
    PID-reuse window. Verified by killing a real process rather than asserting it."""
    path = tmp_path / "lock"
    src = (
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(Path(hostos.__file__).parent.parent)!r})\n"
        "from gbfleet.hostos import lock_exclusive\n"
        f"fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "lock_exclusive(fd)\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        mine = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with pytest.raises(AlreadyLocked):
                lock_exclusive(mine)
        finally:
            os.close(mine)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    deadline = time.monotonic() + 10
    while True:
        mine = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_exclusive(mine)
            return
        except AlreadyLocked:
            if time.monotonic() > deadline:
                pytest.fail("the lock outlived the process holding it")
            time.sleep(0.1)
        finally:
            os.close(mine)


# --- identity and spawn shape ------------------------------------------------------

def test_user_tag_is_usable_in_a_path():
    tag = user_tag()
    assert tag and "/" not in tag and "\\" not in tag and " " not in tag


def test_spawn_kwargs_match_the_platform():
    kwargs = spawn_kwargs()
    if hostos.WINDOWS:
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert "start_new_session" not in kwargs, (
            "start_new_session is a POSIX concept and Popen ignores it on Windows — a "
            "child left in the supervisor's own group takes the supervisor's CTRL_BREAK"
        )
    else:
        assert kwargs == {"start_new_session": True}


# --- the process tree --------------------------------------------------------------

_SPAWNS_A_HELPER = (
    "import subprocess, sys, time\n"
    "h = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "open(sys.argv[1], 'w').write(str(h.pid))\n"
    "time.sleep(300)\n"
)


def _alive(pid: int) -> bool:
    if hostos.WINDOWS:  # pragma: no cover - the box, not here
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_killing_the_tree_takes_the_helpers_with_it(tmp_path: Path):
    """The whole reason `ProcessTree` exists. A vendor CLI's helpers are its children,
    and a supervisor that kills only the leader leaves them running and working — which
    costs money and produces conflicting edits in a worktree nobody is watching."""
    marker = tmp_path / "helper.pid"
    top = subprocess.Popen(
        [sys.executable, "-c", _SPAWNS_A_HELPER, str(marker)], **spawn_kwargs()
    )
    tree = ProcessTree(top)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists():
            if time.monotonic() > deadline:
                pytest.fail("the helper never started")
            time.sleep(0.1)
        helper = int(marker.read_text().strip())
        assert _alive(top.pid) and _alive(helper)

        # Checked BEFORE killing anything. If the child is not its own group leader the
        # kill below would reach pytest, and this test's failure mode would be the whole
        # run dying by signal with no output — which is exactly what happened while it
        # was being written, twice, and cost far more time than the check costs.
        if not hostos.WINDOWS:
            assert os.getpgid(top.pid) != os.getpgid(0), (
                "the child shares this process's group, so killing its tree would kill "
                "the test runner. Refusing to run the kill."
            )

        tree.kill()
        top.wait(timeout=20)

        deadline = time.monotonic() + 20
        while _alive(helper):
            if time.monotonic() > deadline:
                pytest.fail(
                    f"helper {helper} survived its leader being killed — the supervisor "
                    "would leave it running and paying"
                )
            time.sleep(0.1)
    finally:
        tree.close()
        if top.poll() is None:
            top.kill()
        if marker.exists():
            pid = int(marker.read_text().strip())
            if _alive(pid):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass


def test_closing_the_tree_handle_leaves_the_children_running(tmp_path: Path):
    """`close()` releases the handle; it must not be a kill.

    This is `JOB_LIMIT_FLAGS == 0` observed from the outside rather than asserted as a
    number, and it is the behaviour adoption depends on: a supervisor that exits leaves
    its children for the next one, which is why `lock.py` bothers to report a takeover.
    """
    top = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                           **spawn_kwargs())
    tree = ProcessTree(top)
    try:
        tree.close()
        time.sleep(1)
        # `poll()`, not `_alive()`. `top` is OUR child, so once it dies it becomes a
        # zombie until reaped — and `os.kill(pid, 0)` succeeds on a zombie. Sabotage
        # caught this: making `close()` kill the tree left the test green, because the
        # corpse still answered. `poll()` reads the exit status and cannot be fooled.
        assert top.poll() is None, (
            "closing the tree handle killed the child. On Windows that means "
            "KILL_ON_JOB_CLOSE is set, and a supervisor crash now destroys every "
            "child's uncommitted work instead of leaving it to be adopted."
        )
    finally:
        top.kill()
        top.wait(timeout=10)


@posix_only
def test_the_posix_branch_still_uses_a_session_and_killpg(tmp_path: Path):
    """Named so the port cannot quietly change behaviour on the platform that already
    worked. `start_new_session` is what makes the child a group leader, and `killpg`
    only reaches the helpers because of it."""
    assert spawn_kwargs() == {"start_new_session": True}
    top = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                           **spawn_kwargs())
    try:
        assert os.getpgid(top.pid) == top.pid, "the child is not its own group leader"
    finally:
        top.kill()
        top.wait(timeout=10)


@posix_only
def test_signalling_a_group_we_are_in_is_refused(tmp_path: Path):
    """The guard that turns a catastrophe into a complaint.

    A child that never got its own session is in the supervisor's group, and `killpg`
    would then reach the supervisor, every sibling worker, and the operator's shell. The
    symptom is the entire session dying by signal with no output — unattributable, and
    found here only because it happened twice while building this.
    """
    from gbfleet.hostos import WouldSignalOurselves, _signal_group_posix

    # Deliberately NOT given its own session, which is the regression being guarded.
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert os.getpgid(sibling.pid) == os.getpgid(0), "test setup is not reproducing it"
        with pytest.raises(WouldSignalOurselves):
            _signal_group_posix(sibling.pid, 9)
        assert _alive(sibling.pid), "the guard raised but signalled anyway"
    finally:
        sibling.kill()
        sibling.wait(timeout=10)


def test_spawn_attaches_a_process_tree(tmp_path: Path, git_repo: Path):
    """`stop()` falls back to building a `ProcessTree` when the child has none, and on
    POSIX that fallback is indistinguishable from the real thing — which is why removing
    the attachment in `spawn()` survived sabotage here.

    On Windows it is emphatically not the same. The job object is what owns the tree,
    and one created at stop-time cannot contain helpers the child spawned minutes
    earlier: they are outside the job and survive the kill. So the attachment is
    asserted directly, on every platform, rather than inferred from behaviour on the one
    platform where its absence does no harm.
    """
    from gbfleet.spawn import Launch, spawn

    script = tmp_path / "s.py"
    script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    launch = Launch(
        adapter="fake",
        argv=[sys.executable, str(script)],
        seat_path=tmp_path / "seat.json",
        config={"mcpServers": {}},
        instruction="",
    )
    child = spawn(launch, tmp_path, "gb/x", tmp_path / "logs")
    try:
        assert child.tree is not None, (
            "spawn() did not attach a ProcessTree. On Windows the job object would then "
            "be created at stop() time, too late to contain the helpers the child had "
            "already spawned — they would survive the kill and keep working."
        )
        assert child.tree.pid == child.pid
    finally:
        child.process.kill()
        child.process.wait(timeout=10)
