"""One supervisor per repository, and telling a crash from a clean exit.

PRD-22 D-h. Every test here is about a way the lock could appear to work while not
binding: contending on the wrong key, refusing forever after a reboot, or acquiring
after a crash so smoothly that nobody notices there are orphaned children.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gbfleet.lock import Holder, RepoLocked, hold
from gbfleet.state import NotARepository, lock_path, repo_key, repo_root, state_root
from gbfleet.hostos import is_owner_only, user_tag

HOLD_SCRIPT = """
import sys, time
from gbfleet.lock import hold
with hold(sys.argv[1], sys.argv[2]) as acquired:
    print(acquired.holder.pid, flush=True)
    time.sleep(120)
"""


def _spawn_holder(repo: Path, state: Path) -> subprocess.Popen:
    """A real second supervisor, in a real second process."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD_SCRIPT, str(repo), str(state)],
        stdout=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline().strip()
    assert line, "child never reported acquiring the lock"
    proc._reported_pid = int(line)  # type: ignore[attr-defined]
    return proc


# --- the cap is per repository ----------------------------------------------------


def test_a_second_supervisor_on_the_same_repo_refuses_and_names_the_holder(
    git_repo: Path, state: Path
):
    with hold(git_repo, state) as first:
        with pytest.raises(RepoLocked) as exc:
            with hold(git_repo, state):
                pass

    assert exc.value.holder is not None
    assert exc.value.holder.pid == first.holder.pid == os.getpid()
    # The PRD asks for the holder's pid by name, because the alternative is a person
    # deleting a lock file they cannot attribute.
    assert str(first.holder.pid) in str(exc.value)


def test_supervisors_on_different_repos_never_contend(
    git_repo: Path, other_repo: Path, state: Path
):
    with hold(git_repo, state) as a, hold(other_repo, state) as b:
        assert a.path != b.path
        assert a.path.exists() and b.path.exists()


def test_a_linked_worktree_is_the_same_repository(
    git_repo: Path, linked_worktree: Path, state: Path
):
    """The hole D-h exists to close.

    The supervisor's whole job is creating linked worktrees, so a second supervisor
    started from inside one is the likely accident rather than an exotic one. Keyed on
    `--show-toplevel` it would get its own lock, run alongside the first, and
    `--max-workers` would silently become a per-worktree cap.
    """
    assert repo_root(linked_worktree) == repo_root(git_repo)
    assert lock_path(repo_root(linked_worktree), state) == lock_path(
        repo_root(git_repo), state
    )

    with hold(git_repo, state):
        with pytest.raises(RepoLocked):
            with hold(linked_worktree, state):
                pass


def test_the_key_survives_the_ways_one_path_can_be_written(git_repo: Path, tmp_path: Path):
    link = tmp_path / "symlinked"
    link.symlink_to(git_repo)
    assert repo_key(git_repo) == repo_key(Path(str(git_repo) + "/"))
    assert repo_key(git_repo) == repo_key(link)


def test_two_repos_with_the_same_directory_name_get_different_keys(tmp_path: Path):
    """The readable half of the key is the directory name, and checkouts of the same
    project under different parents are the normal case, not a contrived one."""
    a = tmp_path / "one" / "graphban"
    b = tmp_path / "two" / "graphban"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert repo_key(a) != repo_key(b)
    assert repo_key(a).startswith("graphban-")


def test_somewhere_that_is_not_a_repository_says_so(tmp_path: Path):
    with pytest.raises(NotARepository):
        repo_root(tmp_path)


# --- telling a crash from a clean exit --------------------------------------------


def test_a_clean_release_leaves_nothing_to_take_over(git_repo: Path, state: Path):
    with hold(git_repo, state) as first:
        path = first.path
    assert path.read_text(encoding="utf-8") == ""

    with hold(git_repo, state) as second:
        assert second.takeover is None


def test_a_killed_supervisor_leaves_a_lock_the_next_one_can_take(
    git_repo: Path, state: Path
):
    """The load-bearing test for 'liveness, not presence'.

    A supervisor is SIGKILLed — no cleanup, no atexit, no chance to release. The next
    supervisor must be able to start (or a reboot leaves the repo locked forever) AND
    must be told it is taking over (or it starts blind beside orphaned children).
    """
    proc = _spawn_holder(git_repo, state)
    dead_pid = proc._reported_pid  # type: ignore[attr-defined]

    with pytest.raises(RepoLocked):
        with hold(git_repo, state):
            pass  # pragma: no cover - the point is that we do not get here

    proc.kill()  # portable: SIGKILL here, TerminateProcess on Windows
    proc.wait(timeout=30)

    with hold(git_repo, state) as taken:
        assert taken.takeover is not None, (
            "acquired after a crash without noticing — a new supervisor would start "
            "blind beside children that are still running"
        )
        assert taken.takeover.holder is not None
        assert taken.takeover.holder.pid == dead_pid
        assert str(dead_pid) in taken.takeover.describe()


def test_an_unreadable_record_still_counts_as_a_takeover(git_repo: Path, state: Path):
    """A partial write is exactly what a crash mid-write leaves behind.

    'We cannot tell who held this' must not collapse into 'nobody held this'. The
    question adoption turns on is whether a fleet may be running unsupervised, and an
    unreadable record answers that yes.
    """
    path = lock_path(repo_root(git_repo), state)
    path.write_text('{"pid": 42, "repo": "/some/pl', encoding="utf-8")

    with hold(git_repo, state) as taken:
        assert taken.takeover is not None
        assert taken.takeover.holder is None
        assert "unreadable" in taken.takeover.describe()


def test_the_holder_record_on_disk_is_the_running_supervisor(git_repo: Path, state: Path):
    with hold(git_repo, state) as acquired:
        record = json.loads(acquired.path.read_text(encoding="utf-8"))
    assert record["pid"] == os.getpid()
    assert record["repo"] == str(repo_root(git_repo))
    assert Holder.parse(json.dumps(record)) is not None


def test_a_lock_held_but_not_yet_written_is_not_reported_as_free(
    git_repo: Path, state: Path
):
    """The window between flock and the record being written is small and real.

    Reporting 'held by nobody' there invites the reader to delete a live lock.
    """
    proc = _spawn_holder(git_repo, state)
    try:
        lock_path(repo_root(git_repo), state).write_text("", encoding="utf-8")
        with pytest.raises(RepoLocked) as exc:
            with hold(git_repo, state):
                pass
        assert exc.value.holder is None
        assert "has not yet written its record" in str(exc.value)
    finally:
        proc.kill()  # portable: SIGKILL here, TerminateProcess on Windows
        proc.wait(timeout=30)


# --- the state directory ----------------------------------------------------------


def test_the_state_directory_is_private_to_this_user(tmp_path: Path, monkeypatch):
    """On Linux /tmp is shared. Not a security boundary (D-k), but two accounts on one
    host must not silently contend for the same lock.

    The temp directory is redirected so the directory is CREATED by this test. Without
    that it asserted against `tempfile.gettempdir()`, a path that survives between runs
    — so once any earlier run had made it private, `mkdir(exist_ok=True)` left it alone
    and the assertion passed whether or not the code still restricted anything.
    Sabotage caught it: removing the restriction entirely kept every test green.

    `user_tag()` rather than `os.getuid()`, which does not exist on Windows — the same
    reason the mode assertions became `is_owner_only`.
    """
    import tempfile as tempfile_mod

    monkeypatch.setattr(tempfile_mod, "gettempdir", lambda: str(tmp_path))

    root = state_root()
    assert root.parent == tmp_path, "the redirect did not take; this proves nothing"
    assert root.name == f"gbfleet-{user_tag()}"
    assert is_owner_only(root)


def test_hold_opens_a_lock_inside_the_restricted_state_directory(
    git_repo: Path, tmp_path: Path, monkeypatch,
):
    """THE CALL. `test_a_restricted_directory_is_still_usable_by_its_owner` drives
    `restrict_to_owner` directly. Every lock test uses an unrestricted `tmp_path`
    fixture. `_OWNER_ONLY_DIR = 0o600` left those green and `hold()` still
    PermissionError'd (GRPH-600 bounce).
    """
    import tempfile as tempfile_mod

    monkeypatch.setattr(tempfile_mod, "gettempdir", lambda: str(tmp_path))
    root = state_root()

    with hold(git_repo, root) as acquired:
        assert acquired.path.parent == root
        assert acquired.path.exists()
        acquired.path.read_text(encoding="utf-8")


def test_the_lock_file_is_not_world_readable(git_repo: Path, state: Path):
    with hold(git_repo, state) as acquired:
        assert is_owner_only(acquired.path)


def test_being_refused_does_not_disturb_the_holder(git_repo: Path, state: Path):
    """Found by the SIGKILL test above, which is the only place the symptom shows.

    An earlier version emptied the lock file on every exit path, including the one
    where it had just been refused. The refusal still worked, so the lock looked
    correct — but the live holder's record was gone, nothing could name it afterwards,
    and when it eventually crashed its successor found an empty file and reported no
    takeover. Absence reading clean, one process removed from where it was caused.
    """
    with hold(git_repo, state) as first:
        before = first.path.read_text(encoding="utf-8")
        for _ in range(3):
            with pytest.raises(RepoLocked):
                with hold(git_repo, state):
                    pass  # pragma: no cover
        assert first.path.read_text(encoding="utf-8") == before
        assert json.loads(before)["pid"] == os.getpid()


def test_a_pre_existing_lock_file_gets_tightened(git_repo: Path, state: Path):
    """The only case the explicit chmod covers, and the sabotage pass found it untested.

    `os.open(..., 0o600)` already creates at 0600, so on a fresh file the chmod is
    redundant and a test that only ever creates fresh files cannot tell whether it is
    there. It matters when the file already exists — written by an older gbfleet, by a
    different umask, or by hand — because O_CREAT does not change the mode of a file it
    did not create, and a lock file holds a repository path someone may not want
    readable.
    """
    path = lock_path(repo_root(git_repo), state)
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)
    assert not is_owner_only(path), (
        "the precondition did not take: this test needs a file that starts "
        "readable by others, and asserting a POSIX mode literal here is what "
        "made it fail on Windows for a reason unrelated to the lock"
    )

    with hold(git_repo, state) as acquired:
        assert is_owner_only(acquired.path)


# --- GRPH-881: gbfleet mcp attaches read-only when the lock is held -----------------


MCP_ATTACH_SCRIPT = """
import json, subprocess, sys, os

# Send initialize to gbfleet mcp via stdin, read the reply from stdout.
proc = subprocess.Popen(
    [sys.executable, "-m", "gbfleet.cli", "mcp",
     "--repo", sys.argv[1], "--server", "http://127.0.0.1:1"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, env={**os.environ, "GBFLEET_API_KEY": "test-key",
                    # GRPH-1011: `mcp` sweeps the state directory at startup now, and a
                    # conftest monkeypatch cannot reach a subprocess. An empty TMPDIR is
                    # what keeps this from reaping the developer's real abandoned
                    # worktrees — nothing on a CI runner, 1.4 GB on the machine the item
                    # was measured on. All three names because Windows reads TMP/TEMP.
                    "TMPDIR": sys.argv[2], "TMP": sys.argv[2], "TEMP": sys.argv[2]},
)
init_msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\\n"
stdout, stderr = proc.communicate(init_msg, timeout=10)
# Write the exit code and stdout for the test to read.
print(json.dumps({"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr}))
"""


def test_mcp_initialize_succeeds_while_another_supervisor_holds_the_repo(
    git_repo: Path, state: Path
):
    """THE load-bearing test (GRPH-881). With a live holder, send MCP `initialize` on
    stdin and assert a JSON-RPC result (protocolVersion + serverInfo) on stdout, exit 0
    after stdin closes. If initialize still exits 3 under a held lock, this test fails.
    """
    holder = _spawn_holder(git_repo, state)
    subprocess_state = state.parent / "subprocess-state"
    subprocess_state.mkdir()
    try:
        result = subprocess.run(
            [sys.executable, "-c", MCP_ATTACH_SCRIPT, str(git_repo),
             str(subprocess_state)],
            capture_output=True, text=True, timeout=15,
            cwd=str(git_repo),
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        assert data["exit_code"] == 0, (
            f"gbfleet mcp exited {data['exit_code']}: {data['stderr']}")
        # Parse the JSON-RPC reply from stdout.
        lines = [l for l in data["stdout"].strip().splitlines() if l.strip()]
        assert lines, "no JSON-RPC reply on stdout — initialize was never answered"
        reply = json.loads(lines[0])
        assert reply.get("result", {}).get("protocolVersion"), (
            f"no protocolVersion in reply: {reply}")
        assert reply["result"]["serverInfo"]["name"] == "gbfleet"
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_a_second_drain_on_the_same_repo_still_exits_3(
    git_repo: Path, state: Path
):
    """Two `gbfleet up`/`until` on the same clone still refuse at startup.
    Do not weaken drain-vs-drain (PRD-22 D-h).
    """
    with hold(git_repo, state):
        with pytest.raises(RepoLocked) as exc:
            with hold(git_repo, state):
                pass
    assert exc.value.holder is not None


# --- GRPH-1011: holding the lock for the length of a tidy-up pass ---------------------


def test_sweep_hold_answers_free_locked_and_unknown_separately(
    git_repo: Path, state: Path, tmp_path: Path
):
    """`free` and `locked` are the kernel's two answers. `unknown` is the third, and it
    must not collapse into `free`: a sweep that read "I could not ask" as "nobody is
    here" would delete a live supervisor's worktrees, while collapsing it into `locked`
    would leave the report unable to say which of the two happened.

    Sabotage: yield `LockState("free")` from `sweep_hold`'s except branch; the third
    assertion fails.
    """
    from gbfleet.lock import LockState, sweep_hold

    guard = lock_path(repo_root(git_repo), state)

    # No lock file at all is the ordinary free case, and it is free because the kernel
    # released the flock when the last holder exited — not because the file is missing a
    # pid we could have misread as live.
    with sweep_hold(guard) as free:
        assert free.state == "free" and free.free, free
        assert "no supervisor" in free.describe()

    with hold(git_repo, state) as acquired:
        with sweep_hold(guard) as held:
            assert held.state == "locked" and not held.free, held
            assert held.holder is not None and held.holder.pid == acquired.holder.pid
            assert str(acquired.holder.pid) in held.describe()

    # A lock file that cannot be opened at all. A directory is how that happens without
    # depending on a permission the suite cannot rely on.
    wedged = tmp_path / "wedged.lock"
    wedged.mkdir()
    with sweep_hold(wedged) as unknown:
        assert unknown.state == "unknown" and not unknown.free, unknown
        assert unknown.why, "an unanswerable question still has to say it was asked"

    # Held, with a record we cannot name — the partial write a crash leaves behind. Said
    # without "nobody" in it, because a report reading "nobody holds this" is an
    # invitation to delete a live lock (the same reason `RepoLocked` refuses to guess).
    # Constructed rather than provoked: `hold` overwrites the record as it acquires, so
    # nothing reachable from a test is both held and unreadable for longer than the
    # write itself.
    nameless = LockState("locked")
    assert not nameless.free
    assert "nobody" not in nameless.describe()
    assert "not finished writing its record" in nameless.describe(), nameless.describe()


def test_a_dead_pid_in_the_lock_file_is_not_a_held_lock(git_repo: Path, state: Path):
    """The measurement `gc` rests on (GRPH-1011). The pid in the file is for the error
    message, not for the decision: the flock lives on an open descriptor and the kernel
    dropped it when that process exited, however it exited. A sweep that read a leftover
    pid as a live holder would skip exactly the repositories it exists to clean.

    Sabotage: make `sweep_hold` answer `locked` when the file names a pid; this fails.
    """
    from gbfleet.lock import sweep_hold

    path = lock_path(repo_root(git_repo), state)
    path.write_text(json.dumps({
        "pid": 999_999_999, "repo": str(repo_root(git_repo)),
        "acquired_at": "2026-09-27T00:00:00+00:00", "version": "test",
    }), encoding="utf-8")
    try:
        with sweep_hold(path) as answer:
            assert answer.state == "free", answer
            assert answer.free
    finally:
        path.unlink(missing_ok=True)


def test_sweep_hold_writes_no_holder_record_of_its_own(git_repo: Path, state: Path):
    """A sweep is not a supervisor, so it must not look like one in the lock file.

    What is written there is how the NEXT supervisor tells a crash from a clean shutdown,
    and a crash is what makes it adopt rather than start blind beside a fleet (GRPH-599).
    A pass that wrote itself in — or merely truncated on the way out, which is what `hold`
    does — would erase that, and every symptom of the loss is a lock that appears to work.

    Sabotage: truncate and write a `Holder` in `sweep_hold` the way `hold` does; the
    record asserted on here is gone and the takeover below is never reported.
    """
    from gbfleet.lock import sweep_hold

    path = lock_path(repo_root(git_repo), state)
    crashed = Holder(
        pid=424242, repo=str(repo_root(git_repo)),
        acquired_at="2026-09-27T00:00:00+00:00", version="test")
    path.write_text(crashed.as_json(), encoding="utf-8")

    with sweep_hold(path) as answer:
        assert answer.free, answer
        assert path.read_text(encoding="utf-8") == crashed.as_json()
        # What a supervisor starting during the sweep sees and hears. The refusal is the
        # existing one, and it names the record the file actually holds rather than
        # inventing a holder — see `sweep_hold` for why that is the honest answer.
        with pytest.raises(RepoLocked) as exc:
            with hold(git_repo, state):
                pass
        assert exc.value.holder is not None
        assert exc.value.holder.pid == crashed.pid

    assert path.read_text(encoding="utf-8") == crashed.as_json(), (
        "the pass left something of itself in the lock file")
    with hold(git_repo, state) as acquired:
        assert acquired.takeover is not None, "the crash was erased by a tidy-up pass"
        assert acquired.takeover.holder is not None
        assert acquired.takeover.holder.pid == crashed.pid
