"""A reused wave name is a new run (GRPH-1088).

Two failures arrived together. The roster still listed the previous child on the same
worktree, the supervisor matched the first row, and the watchdog stopped the new process
because that old id was gone. Separately, the next cut reused a branch name the remote
already had at a merged commit, and the push fast-forwarded it.

Sabotage for the first test: in `await_registration`, go back to taking the first roster
row whose worktree matches. This test must fail. The remote tests fail if `create` cuts
a name without asking `ls-remote`, or if a failed `ls-remote` is read as "the name is free".
"""

from __future__ import annotations

import io
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gbfleet.cli import report
from gbfleet.spawn import Child, LaunchFailed, await_registration
from gbfleet.supervisor import Limits, Wave, _catch_the_disowned, up
from gbfleet.worktree import (
    GitError, RemoteBranchTaken, branch_name, create,
)

from test_supervisor import _factory, _seats, _server


class _Running:
    pid = 424242
    returncode = None

    def poll(self):
        return None


class _Exited:
    pid = 424242
    returncode = 0

    def poll(self):
        return 0


def _child(worktree: Path, process=None) -> Child:
    return Child(
        adapter="fake", worktree=worktree, branch="gb/wave-1", base="main",
        seat_path=worktree / "seat", process=process or _Running(),
        started_at=time.monotonic(), log_dir=worktree,
    )


def _row(agent_id: str, worktree: Path, **over) -> dict:
    row = {
        "id": agent_id,
        "worktree": str(worktree),
        "state": "working",
        "enrolment_id": f"seat-{agent_id}",
    }
    row.update(over)
    return row


def _iso(when: datetime) -> str:
    return when.isoformat()


def test_a_stale_roster_row_on_the_same_worktree_is_not_this_child(tmp_path: Path):
    """The previous run is listed first, still `working`, on this directory.

    The child must bind to its own row. The watchdog then sees that id still
    working and does not stop it — the old id is the one that disappeared.

    Sabotage: match the first worktree row again. The assertion on `agent_id`
    fails before anything is signalled.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    child = _child(wt)
    now = datetime.now(timezone.utc)
    old = _row(
        "GRPH-A3345", wt, enrolment_id="seat-old",
        registered_at=_iso(now - timedelta(hours=1)),
    )
    new = _row(
        "GRPH-A3349", wt, enrolment_id="seat-new",
        registered_at=_iso(now),
    )

    await_registration(
        child, lambda: {"agents": [old, new]},
        window=5, poll=0, sleep=lambda _: None,
    )

    assert child.agent_id == "GRPH-A3349"
    assert child.seat_id == "seat-new"

    wave = Wave()
    _catch_the_disowned(
        wave, [child], {"agents": [new]}, Limits(disowned_after=0),
    )
    assert child.stopped_because is None
    assert wave.failures == []


def test_a_row_from_before_the_child_started_is_not_adopted_alone(tmp_path: Path):
    """Only the previous run is listed. That is "not registered yet", not a match."""
    wt = tmp_path / "wt"
    wt.mkdir()
    child = _child(wt, _Exited())
    old = _row(
        "GRPH-A3345", wt,
        registered_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
    )

    with pytest.raises(LaunchFailed):
        await_registration(
            child, lambda: {"agents": [old]},
            window=5, poll=0, sleep=lambda _: None,
        )

    assert child.agent_id is None


@pytest.mark.parametrize("over", [
    {"state": "offline"},
    {"state": "quarantined"},
    {"retired": True},
    {"dismissed": True},
])
def test_an_absent_row_is_not_this_child_even_when_it_just_registered(tmp_path: Path, over):
    wt = tmp_path / "wt"
    wt.mkdir()
    child = _child(wt, _Exited())
    row = _row(
        "GRPH-A3345", wt,
        registered_at=_iso(datetime.now(timezone.utc)),
        **over,
    )

    with pytest.raises(LaunchFailed):
        await_registration(
            child, lambda: {"agents": [row]},
            window=5, poll=0, sleep=lambda _: None,
        )

    assert child.agent_id is None


def test_a_known_seat_beats_a_later_row_on_a_different_seat(tmp_path: Path):
    """`child.seat_id` is the stronger key when this process already has it."""
    wt = tmp_path / "wt"
    wt.mkdir()
    child = _child(wt)
    child.seat_id = "seat-new"
    now = datetime.now(timezone.utc)
    other = _row(
        "GRPH-A3345", wt, enrolment_id="seat-old", state="working",
        registered_at=_iso(now + timedelta(seconds=30)),
    )
    mine = _row(
        "GRPH-A3349", wt, enrolment_id="seat-new", state="working",
        registered_at=_iso(now),
    )

    await_registration(
        child, lambda: {"agents": [other, mine]},
        window=5, poll=0, sleep=lambda _: None,
    )

    assert child.agent_id == "GRPH-A3349"


def test_one_unstamped_row_on_the_worktree_still_matches(tmp_path: Path):
    """A roster from before `registered_at` has one row and no stamp. That still matches."""
    wt = tmp_path / "wt"
    wt.mkdir()
    child = _child(wt)
    row = _row("GRPH-A1", wt)
    assert "registered_at" not in row

    await_registration(
        child, lambda: {"agents": [row]},
        window=5, poll=0, sleep=lambda _: None,
    )

    assert child.agent_id == "GRPH-A1"


def test_two_unstamped_rows_on_one_worktree_match_neither(tmp_path: Path):
    """Taking the first unstamped row is the bug, arrived without timestamps."""
    wt = tmp_path / "wt"
    wt.mkdir()
    child = _child(wt, _Exited())

    with pytest.raises(LaunchFailed):
        await_registration(
            child, lambda: {"agents": [
                _row("GRPH-A3345", wt),
                _row("GRPH-A3349", wt),
            ]}, window=5, poll=0, sleep=lambda _: None,
        )

    assert child.agent_id is None


def _bare_origin(git_repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(
        ["git", "-C", str(git_repo), "remote", "add", "origin", str(bare)],
        check=True,
    )
    return bare


def _sha(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_create_refuses_a_name_the_remote_already_has(git_repo: Path, tmp_path: Path):
    """Nothing is cut, so nothing can be pushed onto the old ref.

    Sabotage: delete the `remote_name` check in `create`. This raises nothing and
    the worktree path exists.
    """
    bare = _bare_origin(git_repo, tmp_path)
    subprocess.run(
        ["git", "-C", str(git_repo), "push", "origin", "HEAD:refs/heads/gb/wave-1"],
        check=True, capture_output=True, text=True,
    )
    pushed = _sha(bare, "refs/heads/gb/wave-1")
    path = tmp_path / "child"

    with pytest.raises(RemoteBranchTaken) as exc:
        create(git_repo, path, "wave", "1")

    assert "gb/wave-1" in str(exc.value)
    assert pushed[:12] in str(exc.value)
    assert not path.exists()


def test_a_neighbouring_remote_ref_does_not_take_this_name(git_repo: Path, tmp_path: Path):
    """`ls-remote`'s pattern can be broader than one ref. `gb/wave-10` is not `gb/wave-1`."""
    _bare_origin(git_repo, tmp_path)
    subprocess.run(
        ["git", "-C", str(git_repo), "push", "origin", "HEAD:refs/heads/gb/wave-10"],
        check=True, capture_output=True, text=True,
    )
    path = tmp_path / "child"

    tree = create(git_repo, path, "wave", "1")

    assert tree.branch == "gb/wave-1"
    assert path.is_dir()


def test_create_does_not_cut_when_the_remote_cannot_be_asked(git_repo: Path, tmp_path: Path):
    """A failed `ls-remote` is unknown. Unknown is not free, and the wave must not
    burn a thousand slots treating it as a taken name either — `GitError` stops it.
    """
    subprocess.run(
        ["git", "-C", str(git_repo), "remote", "add", "origin", str(tmp_path / "missing.git")],
        check=True,
    )
    path = tmp_path / "child"

    with pytest.raises(GitError) as exc:
        create(git_repo, path, "wave", "1")

    assert "could not ask origin" in str(exc.value)
    assert not path.exists()


def test_a_wave_skips_a_remote_branch_and_pushes_nothing_onto_it(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """`gb/wave-1` is already on the remote at a merged commit. The child is cut on
    the next slot, and that remote ref's sha does not move.

    The child writes a file, so salvage has a commit to push. Without the check the
    push fast-forwards `origin/gb/wave-1`, because the old tip is the parent.
    """
    bare = _bare_origin(git_repo, tmp_path)
    subprocess.run(
        ["git", "-C", str(git_repo), "push", "origin", "HEAD:refs/heads/gb/wave-1"],
        check=True, capture_output=True, text=True,
    )
    before = _sha(bare, "refs/heads/gb/wave-1")
    workspace = tmp_path / "ws"

    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"),
        _server(workspace), limits=Limits(max_workers=1),
        state=state, workspace=workspace,
    )

    assert wave.ok, wave.failures
    assert wave.spawned[0].branch == branch_name("wave", "2")
    assert _sha(bare, "refs/heads/gb/wave-1") == before
    assert wave.remote_taken
    assert "gb/wave-1" in wave.remote_taken[0]
    assert before[:12] in wave.remote_taken[0]
    out = io.StringIO()
    report(wave, out)
    assert "BRANCH TAKEN" in out.getvalue()
    assert "gb/wave-1" in out.getvalue()
