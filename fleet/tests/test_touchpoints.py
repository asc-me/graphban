"""PRD-22 S5 — what a worker actually changed, measured off its own branch.

One worker, one worktree, one branch means the diff boundary is already exact:
everything on that branch since it was cut is this worker's doing and nothing else is.

The interesting tests here are about what the measurement must NOT do — overwrite the
prediction it exists to be compared against, report a credential as work, or answer
"nothing changed" when the truth is "we could not tell".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gbfleet import touchpoints as tp
from gbfleet.client import ALLOWED_TOOLS
from gbfleet.observe import LOG_FILE
from gbfleet.supervisor import up
from gbfleet.worktree import SEAT_FILES, Worktree, create, reap

from tests.test_supervisor import _factory, _seats, _server


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


# --- the measurement -----------------------------------------------------------------


def test_it_reports_what_the_worker_changed(git_repo: Path, tmp_path: Path):
    tree = create(git_repo, tmp_path / "w1", "wave", "1")
    (tree.path / "feature.py").write_text("print(1)\n", encoding="utf-8")
    (tree.path / "docs").mkdir()
    (tree.path / "docs" / "note.md").write_text("why\n", encoding="utf-8")
    reap(tree)

    assert tp.measure(tree) == ["docs/note.md", "feature.py"]


def test_a_worker_that_changed_nothing_reports_nothing(git_repo: Path, tmp_path: Path):
    """The control. Without it the measurement could be returning the whole tree."""
    tree = create(git_repo, tmp_path / "w1", "wave", "1")
    reap(tree)
    assert tp.measure(tree) == []


def test_the_base_is_fixed_at_creation_not_read_as_HEAD_later(
    git_repo: Path, tmp_path: Path
):
    """`HEAD` stops meaning the same thing the moment anything else moves.

    A supervisor runs several workers and the repository keeps moving underneath them —
    measuring against a live `HEAD` would attribute somebody else's commits to this
    worker, or drop its own.
    """
    tree = create(git_repo, tmp_path / "w1", "wave", "1")
    (tree.path / "mine.py").write_text("mine\n", encoding="utf-8")
    reap(tree)

    # main moves on afterwards, as it does while a fleet runs.
    (git_repo / "somebody-else.py").write_text("theirs\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "-c", "user.email=o@e.invalid", "-c", "user.name=O", "commit", "-qm", "other")

    assert tp.measure(tree) == ["mine.py"], "the measurement moved with the repository"


def test_the_seat_file_is_never_reported_as_work(git_repo: Path, tmp_path: Path):
    """It is the supervisor's own doing and it is a credential. Reporting it as a file
    the worker touched would be wrong twice over."""
    tree = create(git_repo, tmp_path / "w1", "wave", "1")
    seat = tree.path / SEAT_FILES[0]
    seat.parent.mkdir(parents=True, exist_ok=True)
    seat.write_text('{"apiKey": "live"}', encoding="utf-8")
    (tree.path / "feature.py").write_text("print(1)\n", encoding="utf-8")

    _git(tree.path, "add", "-A", "-f")
    _git(tree.path, "-c", "user.email=w@e.invalid", "-c", "user.name=W", "commit", "-qm", "oops")

    measured = tp.measure(tree)
    assert "feature.py" in measured
    for seat_file in SEAT_FILES:
        assert seat_file not in measured


def test_unmeasurable_is_not_reported_as_unchanged(git_repo: Path, tmp_path: Path):
    """"We could not tell" and "it changed nothing" are different answers, and only one
    of them is reassuring. A worktree with no recorded base has no fixed point to
    measure from, so it raises rather than returning an empty list."""
    tree = Worktree(path=tmp_path / "nope", branch="gb/w-1", repo=git_repo, base="")
    with pytest.raises(ValueError) as exc:
        tp.measure(tree)
    assert "base" in str(exc.value)


def test_salvaged_work_is_included(git_repo: Path, tmp_path: Path):
    """Measured AFTER the reap, deliberately: salvage has just committed whatever the
    worker left uncommitted, so the branch holds the whole of what it did. Measuring
    before would miss exactly the work that was most at risk."""
    tree = create(git_repo, tmp_path / "w1", "wave", "1")
    (tree.path / "committed.py").write_text("done\n", encoding="utf-8")
    _git(tree.path, "add", "-A")
    _git(tree.path, "-c", "user.email=w@e.invalid", "-c", "user.name=W", "commit", "-qm", "work")
    (tree.path / "half-done.py").write_text("wip\n", encoding="utf-8")

    reaped = reap(tree)
    assert reaped.salvage and reaped.salvage.commit

    assert tp.measure(tree) == ["committed.py", "half-done.py"]


# --- what it must not do -------------------------------------------------------------


def test_the_supervisor_still_cannot_write_an_item():
    """S5 said "write it back as `touchpoints`". P30 D10 still does not let the
    supervisor do it: the allowlist is two reads, this module measures, and the
    writer with standing lives in `gbfleet.record`.

    Union (not replace) is why walk step 17 still has both operands: `touched` on
    the child record is the measurement, the item's stored prediction stays.
    """
    assert "update_item" not in ALLOWED_TOOLS
    assert ALLOWED_TOOLS == frozenset({"fleet_status", "propose_allocation"})

    source = Path(tp.__file__).read_text(encoding="utf-8")
    assert "update_item" not in source
    assert "prediction" in source, "the reason has to travel with the code"


def test_a_wave_reports_what_each_worker_touched(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
        state=state, workspace=workspace,
    )
    assert wave.ok, wave.failures
    branch = wave.reaped[0].branch
    assert wave.touched[branch] == ["feature.py"]


def test_a_branch_that_cannot_be_measured_is_reported_not_swallowed(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """The handler had no test at all, and a sabotage replacing it with `pass` passed
    everything.

    It is unreachable through the happy path — `create` always records a base — so the
    only honest way to exercise it is to make the measurement fail. Which it can: a
    branch deleted underneath the supervisor, a git that refuses. Swallowing that would
    leave `touched` silently absent for one worker among several, which reads as "that
    one changed nothing".
    """
    import gbfleet.supervisor as mod

    def refuses(tree):
        raise ValueError("no base commit recorded")

    monkeypatch.setattr(mod.tp_mod, "measure", refuses)

    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
        state=state, workspace=workspace,
    )

    assert wave.touched == {}, "nothing was measurable, so nothing should be claimed"
    assert any("no base commit" in f for f in wave.failures), (
        "an unmeasurable branch vanished from the report entirely, which reads as a "
        "worker that changed nothing"
    )
    assert wave.ok is False


def test_the_measurement_reaches_the_record(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """A measurement the supervisor keeps to itself is one nobody can act on. It goes in
    the child's record, which is what a planner reads to do the comparison."""
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
        state=state, workspace=workspace,
    )
    lines = [
        json.loads(line)
        for line in (state / LOG_FILE).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    record = next(r for r in lines if r["event"] == "child")
    assert record["touched"] == ["feature.py"]
