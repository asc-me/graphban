"""Create, classify, salvage — and the credential that must not reach a commit.

PRD-22 D-g. The tests that matter here are the ones about a result that reads clean
while being wrong: a worktree called clean with a live seat in it, a salvage that
reports success while the branch already carries the secret, and a removal that
succeeds by forcing away the work salvage exists to keep.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gbfleet.worktree import (
    BranchExists,
    Disposition,
    Orphan,
    SEAT_FILES,
    branch_name,
    create,
    is_dirty,
    orphans,
    porcelain,
    reap,
    salvage,
    agent_slug,
)

SEAT = SEAT_FILES[0]
SECRET = "gbf_live_seat_do_not_commit"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


def _plant_seat(wt: Path) -> Path:
    seat = wt / SEAT
    seat.parent.mkdir(parents=True, exist_ok=True)
    seat.write_text(f'{{"apiKey": "{SECRET}"}}\n', encoding="utf-8")
    return seat


# --- naming -----------------------------------------------------------------------


def test_the_branch_is_named_after_the_agent_not_the_item():
    """One worker owns a cluster, not an item, so an item-derived name stops being
    true the moment the cluster changes underneath it."""
    assert branch_name("wave-3", "GRPH-A7") == "gb/wave-3-grph-a7"
    assert branch_name("wave-3", "GRPH-A7") == branch_name("wave-3", "GRPH-A7")
    assert branch_name("wave-3", "GRPH-A7") != branch_name("wave-4", "GRPH-A7")


def test_the_agent_id_is_not_truncated_into_a_collision():
    """The bug a plausible `[:8]` would have shipped.

    Server agent ids are `<TAG>-A<n>`, so every id in a project shares a prefix and
    differs only in a trailing number. Cut at eight characters and `GRPH-A1234` is
    `grph-a12` — the same branch as `GRPH-A12` — which arrives the moment a project
    passes its hundredth agent, and puts two workers on one branch each committing
    onto the other's history.
    """
    assert branch_name("w", "GRPH-A12") != branch_name("w", "GRPH-A1234")
    assert branch_name("w", "GRPH-A1") != branch_name("w", "GRPH-A11")
    assert agent_slug("GRPH-A1234") == "grph-a1234"


def test_an_id_git_would_reject_is_made_safe_rather_than_trusted():
    assert "/" not in agent_slug("GRPH/../../etc")
    assert " " not in branch_name("wave one", "GRPH A1")
    with pytest.raises(ValueError):
        agent_slug("///")


# --- creation ---------------------------------------------------------------------


def test_create_makes_a_worktree_on_its_own_branch(git_repo: Path, tmp_path: Path):
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    assert wt.path.is_dir()
    assert (wt.path / "README.md").exists()
    assert _git(wt.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == wt.branch


def test_an_existing_branch_refuses_rather_than_forcing_or_suffixing(
    git_repo: Path, tmp_path: Path
):
    """Both alternatives are silent: forcing attaches a worker to somebody else's
    history, auto-suffixing makes the name stop identifying the agent."""
    create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    with pytest.raises(BranchExists) as exc:
        create(git_repo, tmp_path / "w2", "wave-1", "GRPH-A1")
    assert "gb/wave-1-grph-a1" in str(exc.value)
    assert not (tmp_path / "w2").exists()


# --- what "dirty" means -----------------------------------------------------------


def test_a_worktree_holding_only_a_seat_file_is_dirty(git_repo: Path, tmp_path: Path):
    """The definition D-g insists on. A dirty check covering only tracked
    modifications would call this clean while a live credential sat in it."""
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    assert not is_dirty(wt.path)

    _plant_seat(wt.path)
    assert is_dirty(wt.path)
    assert any(SEAT in line for line in porcelain(wt.path))


# --- salvage ----------------------------------------------------------------------


def test_salvage_commits_the_work_and_not_the_credential(git_repo: Path, tmp_path: Path):
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    (wt.path / "feature.py").write_text("print('work')\n", encoding="utf-8")
    _plant_seat(wt.path)

    result = salvage(wt.path)

    assert result.committed and result.commit
    assert "feature.py" in result.files
    assert SEAT not in result.files

    committed = _git(wt.path, "show", "--pretty=format:", "--name-only", result.commit)
    assert "feature.py" in committed
    assert SEAT not in committed

    blob = _git(wt.path, "show", f"{result.commit}:feature.py")
    assert SECRET not in blob
    assert SECRET not in _git(wt.path, "log", "-p", "-1")


def test_a_tree_holding_only_a_credential_is_not_reported_as_salvaged(
    git_repo: Path, tmp_path: Path
):
    """There is nothing to commit, but the tree was NOT clean. Saying `salvaged` would
    claim a commit that does not exist; saying `clean` would claim a tree that held a
    live seat. It gets its own answer."""
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    _plant_seat(wt.path)

    result = salvage(wt.path)
    assert result.committed is False
    assert result.commit is None


def test_salvage_reports_a_credential_the_worker_committed_itself(
    git_repo: Path, tmp_path: Path
):
    """The case salvage cannot fix, and therefore must not paper over.

    A headless agent that runs `git add -A && git commit` has already put the seat in
    the branch. Salvage's own commit is clean and reporting only that would be true
    and useless — the branch carries the secret either way.
    """
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    _plant_seat(wt.path)
    _git(wt.path, "add", "-A")
    _git(wt.path, "-c", "user.email=w@e.invalid", "-c", "user.name=W", "commit", "-qm", "worker commit")

    (wt.path / "feature.py").write_text("print('more')\n", encoding="utf-8")
    result = salvage(wt.path)

    assert result.committed
    assert SEAT not in result.files
    assert result.credential_in_history == [SEAT], (
        "salvage reported a clean result for a branch that already contains the seat"
    )


def test_salvage_leaves_a_clean_tree_alone(git_repo: Path, tmp_path: Path):
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    before = _git(wt.path, "rev-parse", "HEAD").strip()
    result = salvage(wt.path)
    assert not result.committed
    assert _git(wt.path, "rev-parse", "HEAD").strip() == before


# --- reap -------------------------------------------------------------------------


def test_reaping_a_clean_worktree_removes_it(git_repo: Path, tmp_path: Path):
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    reaped = reap(wt)
    assert reaped.disposition is Disposition.CLEAN
    assert reaped.removed
    assert not wt.path.exists()


def test_reaping_a_dirty_worktree_salvages_then_removes(git_repo: Path, tmp_path: Path):
    """Nothing is destroyed: the work is in git, recoverable indefinitely, and the cost
    is a branch rather than a working tree."""
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    (wt.path / "feature.py").write_text("print('work')\n", encoding="utf-8")

    reaped = reap(wt)
    assert reaped.disposition is Disposition.SALVAGED
    assert reaped.removed and not wt.path.exists()
    assert reaped.salvage and reaped.salvage.commit

    kept = _git(git_repo, "show", f"{reaped.branch}:feature.py")
    assert kept == "print('work')\n"


def test_after_reap_the_seat_file_is_gone(git_repo: Path, tmp_path: Path):
    """Acceptance walk step 8, including the Cursor case inside the worktree."""
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    (wt.path / "feature.py").write_text("x\n", encoding="utf-8")
    seat = _plant_seat(wt.path)
    assert seat.exists()

    reaped = reap(wt)
    assert reaped.removed
    assert not seat.exists()
    assert not wt.path.exists()
    assert SECRET not in _git(git_repo, "log", "-p", "--all")


def test_a_tree_holding_only_a_seat_reaps_as_its_own_outcome(
    git_repo: Path, tmp_path: Path
):
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    _plant_seat(wt.path)

    reaped = reap(wt)
    assert reaped.disposition is Disposition.ONLY_CREDENTIAL
    assert reaped.removed
    assert not wt.path.exists()


def test_an_unexpected_leftover_is_left_alone_rather_than_forced(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """`--force` after a salvage that quietly failed deletes the work this design
    exists to keep. Disk growing is a much smaller problem."""
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    (wt.path / "feature.py").write_text("x\n", encoding="utf-8")

    import gbfleet.worktree as mod

    # Something is in the tree AFTER salvage has run. Simulated by making the file
    # appear between the two porcelain calls rather than by breaking salvage, because
    # the point is the reaction, not any particular way of getting there.
    original = mod.porcelain
    calls = {"n": 0}

    def appears_late(path):
        calls["n"] += 1
        if calls["n"] > 1:
            (path / "appeared-late.txt").write_text("?\n", encoding="utf-8")
        return original(path)

    monkeypatch.setattr(mod, "porcelain", appears_late)

    reaped = mod.reap(wt)
    assert reaped.disposition is Disposition.LEFT_DIRTY
    assert not reaped.removed
    assert wt.path.exists(), "forced away a worktree it could not account for"
    assert "not forcing" in reaped.reason


# --- orphans ----------------------------------------------------------------------


def test_orphans_lists_salvaged_branches_and_not_attached_ones(
    git_repo: Path, tmp_path: Path
):
    live = create(git_repo, tmp_path / "live", "wave-1", "GRPH-A1")
    dead = create(git_repo, tmp_path / "dead", "wave-1", "GRPH-A2")
    (dead.path / "feature.py").write_text("x\n", encoding="utf-8")
    reap(dead)

    found = orphans(git_repo)
    names = [o.branch for o in found]
    assert dead.branch in names
    assert live.branch not in names, "a branch with a live worktree is not an orphan"

    row = next(o for o in found if o.branch == dead.branch)
    assert isinstance(row, Orphan)
    assert row.salvaged
    assert row.commit


def test_orphans_ignores_branches_that_are_not_ours(git_repo: Path, tmp_path: Path):
    _git(git_repo, "branch", "feature/human-work")
    dead = create(git_repo, tmp_path / "dead", "wave-1", "GRPH-A2")
    (dead.path / "f.py").write_text("x\n", encoding="utf-8")
    reap(dead)

    names = [o.branch for o in orphans(git_repo)]
    assert names == [dead.branch]


def test_a_branch_with_no_salvage_commit_is_listed_but_not_called_salvaged(
    git_repo: Path, tmp_path: Path
):
    """A worker that exited cleanly having committed its own work leaves a branch with
    no WIP commit. It is still an orphan the planner may want to resume — reporting
    only salvaged branches would hide the tidiest workers."""
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    (wt.path / "f.py").write_text("x\n", encoding="utf-8")
    _git(wt.path, "add", "-A")
    _git(wt.path, "-c", "user.email=w@e.invalid", "-c", "user.name=W", "commit", "-qm", "real work")
    reap(wt)

    row = next(o for o in orphans(git_repo) if o.branch == wt.branch)
    assert row.salvaged is False
    assert row.subject == "real work"


def test_two_ids_that_sanitise_to_one_name_are_refused_not_shared(
    git_repo: Path, tmp_path: Path
):
    """Sanitising is not injective, so the guarantee lives in `create`, not in the
    naming. `GRPH-A1` and `GRPH.A1` both land on `grph-a1`; the second worker must be
    refused rather than quietly handed the first one's branch."""
    create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    with pytest.raises(BranchExists):
        create(git_repo, tmp_path / "w2", "wave-1", "GRPH.A1")


def _ignore_seats(repo: Path) -> None:
    """Make the repository look like the one this ships for.

    graphban's own .gitignore carries `.cursor/mcp.json` — and any repo whose agents
    use Cursor will, because otherwise every developer sees a permanently dirty tree.
    """
    (repo / ".gitignore").write_text(f"{SEAT}\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "-c", "user.email=t@e.invalid", "-c", "user.name=T", "commit", "-qm", "ignore seats")


def test_a_gitignored_seat_still_makes_the_worktree_need_attention(
    git_repo: Path, tmp_path: Path
):
    """The case that reads clean in the repository this actually runs on.

    D-g says a dirty check must include untracked files because "untracked is exactly
    where the seat file lives". It isn't untracked here — it is IGNORED, and
    `--untracked-files=all` does not report ignored paths. Routed through git's opinion,
    a worktree holding a live credential comes back CLEAN, skips salvage, and the one
    disposition the four-state enum exists to keep separate collapses into the
    reassuring one.
    """
    _ignore_seats(git_repo)
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    _plant_seat(wt.path)

    assert porcelain(wt.path) == [], "premise of this test is gone: git now reports it"
    assert is_dirty(wt.path), "a live credential in the tree read as clean"

    reaped = reap(wt)
    assert reaped.disposition is Disposition.ONLY_CREDENTIAL
    assert not (wt.path).exists()


def test_a_gitignored_seat_is_still_gone_after_reap(git_repo: Path, tmp_path: Path):
    """Walk step 8 has to hold whether or not git can see the file."""
    _ignore_seats(git_repo)
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    (wt.path / "feature.py").write_text("x\n", encoding="utf-8")
    seat = _plant_seat(wt.path)

    reaped = reap(wt)
    assert reaped.disposition is Disposition.SALVAGED
    assert not seat.exists()
    assert SECRET not in _git(git_repo, "log", "-p", "--all")


def test_a_committed_credential_is_reported_even_when_there_is_nothing_to_salvage(
    git_repo: Path, tmp_path: Path
):
    """The path a sabotage slipped through.

    `credential_in_history` was computed twice — once for this early return and once
    after the commit — so mutating the first call site left the other test green. This
    is the return that reads it, and nothing covered it.
    """
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    _plant_seat(wt.path)
    _git(wt.path, "add", "-A")
    _git(wt.path, "-c", "user.email=w@e.invalid", "-c", "user.name=W", "commit", "-qm", "worker committed its seat")

    result = salvage(wt.path)
    assert result.committed is False, "nothing new to stage, so nothing to commit"
    assert result.credential_in_history == [SEAT], (
        "reported nothing while the branch carries the seat"
    )


def test_a_worktree_is_never_removed_while_a_seat_is_still_in_it(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """Walk step 8's guarantee must not rest on the unlink having worked.

    Removing the seat and then removing the worktree is two steps, and the second one
    deletes the directory either way — so if the first silently did nothing, the
    credential goes with it and every observable sign says the reap was clean.

    The repository ignores seats, which is what makes this a real test rather than an
    accidental one: with the seat merely untracked, `git status` reports it and the
    guard would hold for a reason that has nothing to do with seats. A sabotage pass
    caught exactly that — the assertion was right and could not fail.
    """
    _ignore_seats(git_repo)
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    (wt.path / "feature.py").write_text("x\n", encoding="utf-8")
    seat = _plant_seat(wt.path)

    real_unlink = Path.unlink

    def refuse_the_seat(self, *a, **kw):
        if self.name == Path(SEAT).name:
            return None  # silently does nothing, which is the case being defended
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", refuse_the_seat)

    reaped = reap(wt)
    assert reaped.disposition is Disposition.LEFT_DIRTY
    assert not reaped.removed
    assert wt.path.exists() and seat.exists()


def test_a_locked_worktree_is_reported_rather_than_forced(git_repo: Path, tmp_path: Path):
    """`git worktree lock` is a deliberate human act meaning do not touch this.

    It is also the one case where declining to force is observable: git removes IGNORED
    files without `--force` quite happily, so the flag's absence protects less than it
    sounds like it does, and this is where it protects something.
    """
    wt = create(git_repo, tmp_path / "w1", "wave-1", "GRPH-A1")
    (wt.path / "feature.py").write_text("x\n", encoding="utf-8")
    _git(git_repo, "worktree", "lock", str(wt.path))

    reaped = reap(wt)
    assert reaped.disposition is Disposition.LEFT_DIRTY
    assert not reaped.removed
    assert wt.path.exists()
    assert "refused" in reaped.reason
    # The salvage still happened and is still reported — a tidy-up failure must not
    # discard the fact that the work was saved.
    assert reaped.salvage and reaped.salvage.committed
