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

    proc.send_signal(signal.SIGKILL)
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
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)


# --- the state directory ----------------------------------------------------------


def test_the_state_directory_is_private_to_this_user():
    """On Linux /tmp is shared. Not a security boundary (D-k), but two accounts on one
    host must not silently contend for the same lock."""
    root = state_root()
    assert root.name == f"gbfleet-{os.getuid()}"
    assert root.stat().st_mode & 0o777 == 0o700


def test_the_lock_file_is_not_world_readable(git_repo: Path, state: Path):
    with hold(git_repo, state) as acquired:
        assert acquired.path.stat().st_mode & 0o777 == 0o600


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
    assert path.stat().st_mode & 0o777 == 0o644

    with hold(git_repo, state) as acquired:
        assert acquired.path.stat().st_mode & 0o777 == 0o600
