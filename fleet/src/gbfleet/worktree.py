"""One worker, one worktree, one branch — created and removed by the supervisor.

PRD-22 D-g. Graphban keeps its hands clean here: `worktree` and `branch` stay
self-reported strings on the server, `branch_orphaned` stays informational, and
PRD-17's "touching git at all" non-goal binds the *server*. The supervisor is a client
and may run git; that is most of why it exists.

The reason never to delete a dirty worktree is that uncommitted work is unrecoverable.
So commit it, and that reason evaporates. The supervisor never judges content: at reap
it classifies, and a dirty tree becomes a WIP commit on the worker's own branch.
Nothing is destroyed, the work is in git and recoverable indefinitely, and the cost is
a branch rather than a working tree.

Nothing here deletes on a timer, in any form. A timer that removes uncommitted work is
the tool this PRD refused, running slower.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

BRANCH_PREFIX = "gb/"

#: Credential files a vendor forces INSIDE the worktree (PRD-22 D-f). Cursor has no
#: per-invocation config flag and reads `.cursor/mcp.json` from the project directory,
#: and each child's worktree is its own project directory — so this is where a live
#: seat ends up. Every other vendor takes a path to a private temp file outside the
#: tree. Adapters extend this list (GRPH-449); it is declared here because salvage
#: must know it before any adapter exists.
SEAT_FILES: tuple[str, ...] = (
    ".cursor/mcp.json", ".grok/config.toml", ".grok/mcp.json", ".gbfleet-instruction",
)

#: `.grok/mcp.json` is in that tuple but nothing writes it any more. It is kept because
#: a worktree left behind by a gbfleet from before GRPH-575 still has one, with a live
#: key in it, and salvage running from this version would otherwise commit it. Removing
#: the entry costs a credential leak in exactly the case nobody would test.

#: `.gbfleet-instruction` is in that tuple for the same reason as the Cursor config and
#: it is easier to miss: it carries the enrolment CODE, because the code is an argument
#: to `register_agent` rather than a config value, and putting it on argv would publish
#: it to every `ps` on the machine. So it goes in the worktree — which makes the tree
#: dirty, which triggers salvage, which would commit a live credential. It is excluded
#: and verified with the rest.



class GitError(RuntimeError):
    pass


class BranchExists(RuntimeError):
    """Refuse to spawn onto a branch somebody else already owns.

    Never force and never auto-suffix: both silently attach a worker to somebody
    else's history, and the second one also makes the branch name stop identifying
    the agent, which is the only thing it is for.
    """


class Disposition(str, Enum):
    """What became of a worktree at reap. Four outcomes, deliberately.

    Collapsing any pair of these is how disk fills while every log line reads fine.
    `ONLY_CREDENTIAL` in particular is NOT clean: the tree was non-empty, and the one
    thing in it was a live seat.
    """

    CLEAN = "clean"
    SALVAGED = "salvaged"
    ONLY_CREDENTIAL = "only_credential"
    LEFT_DIRTY = "left_dirty"


def _git(cwd: Path | str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {cwd} ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def agent_slug(agent_id: str) -> str:
    """The identifying half of a branch name — the WHOLE id, not a prefix of it.

    Truncating looks obviously right and is obviously wrong here. Server agent ids are
    `<TAG>-A<n>` (`services/keys.py:mint` via `tagging.render`: `GRPH-A7`,
    `GRPH-A1234`), so every id in a project shares a prefix and differs only in a
    trailing number. Cut at eight characters and `GRPH-A1234` becomes `grph-a12` —
    the same branch as `GRPH-A12`, reached the moment a project passes its hundredth
    agent. Two workers would then share a branch and each would be committing onto the
    other's history, which is the exact outcome D-g's refuse-never-force rule exists to
    prevent, arriving through the naming instead of through the refusal.

    Sanitising is not injective either (`GRPH-A1` and `GRPH.A1` both land on
    `grph-a1`), so the guarantee is not really in this function: `create` refuses a
    branch that already exists, and that refusal is what makes a collision loud rather
    than silent. Real ids come from one minter with one format and cannot collide here.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", agent_id).strip("-").lower()
    if not cleaned:
        raise ValueError(f"agent id {agent_id!r} has nothing usable in it")
    return cleaned


def branch_name(wave: str, agent_id: str) -> str:
    """`gb/<wave>-<agent-short-id>`.

    Named after the AGENT, not the item: one worker owns a cluster rather than a
    single item, so an item-derived name would stop being true the moment the cluster
    changed underneath it. Deterministic and collision-free by construction.
    """
    wave_part = re.sub(r"[^A-Za-z0-9]+", "-", wave).strip("-").lower()
    if not wave_part:
        raise ValueError(f"wave {wave!r} has nothing usable in it")
    return f"{BRANCH_PREFIX}{wave_part}-{agent_slug(agent_id)}"


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    repo: Path
    #: The commit this worktree was cut from, resolved to a sha at creation. Kept
    #: because "HEAD" stops meaning the same thing the moment anything else moves, and
    #: measuring what a worker changed needs a fixed point to measure from.
    base: str = ""


def branch_exists(repo: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def create(
    repo: Path, path: Path, wave: str, agent_id: str, base: str = "HEAD"
) -> Worktree:
    branch = branch_name(wave, agent_id)
    if branch_exists(repo, branch):
        raise BranchExists(
            f"{branch} already exists in {repo}. Refusing to spawn: forcing would "
            "attach this worker to somebody else's history, and auto-suffixing would "
            "make the branch stop identifying the agent. Reap or rename it first."
        )
    base_sha = _git(repo, "rev-parse", base).strip()
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base_sha)
    return Worktree(path=Path(path), branch=branch, repo=Path(repo), base=base_sha)


def porcelain(worktree: Path) -> list[str]:
    """`git status --porcelain`, untracked included.

    Untracked is not an afterthought: work an agent left uncommitted is mostly new
    files, and a check covering only tracked modifications would discard them.
    """
    out = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    return [line for line in out.splitlines() if line.strip()]


def seats_present(worktree: Path) -> list[str]:
    """Seat files actually on disk, asked of the filesystem rather than of git.

    **Measured against the repository this ships for.** D-g says a dirty check must
    include untracked files because "untracked is exactly where the seat file lives" —
    but in this repo `.cursor/mcp.json` is *gitignored*, and `--untracked-files=all`
    does not report ignored paths. So `git status` is empty for a worktree holding a
    live credential: it reads CLEAN, skips salvage, and the one outcome the four-state
    disposition exists to keep separate collapses into the reassuring one.

    Ignored is the normal case, not the exotic one — any repo whose agents use Cursor
    will have gitignored that path, because otherwise every developer sees a dirty tree.
    So seat detection cannot be routed through git's opinion of the file at all.
    """
    return [p for p in SEAT_FILES if (Path(worktree) / p).exists()]


def is_dirty(worktree: Path) -> bool:
    """Anything here that should stop this worktree being deleted without a look."""
    return bool(porcelain(worktree)) or bool(seats_present(worktree))


def _tracked_in_head(worktree: Path, paths: tuple[str, ...]) -> list[str]:
    if not _git(worktree, "rev-parse", "--quiet", "--verify", "HEAD", check=False):
        return []
    listed = _git(worktree, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    return sorted(set(listed) & set(paths))


@dataclass(frozen=True)
class Salvage:
    """What a WIP commit did and did not capture."""

    committed: bool
    commit: str | None
    files: list[str] = field(default_factory=list)
    #: Seat files found in the branch's HISTORY, not in this commit. Salvage cannot
    #: undo a credential the worker itself committed earlier, so it reports one rather
    #: than returning a clean result that is true only of the commit it made.
    credential_in_history: list[str] = field(default_factory=list)


def salvage(worktree: Path, message: str | None = None) -> Salvage:
    """Commit whatever is in this tree onto the worker's own branch.

    **The credential is excluded at staging and then verified**, rather than relying on
    an ignore file. Measured, not assumed: git does NOT honour
    `$GIT_DIR/info/exclude` inside a linked worktree, and the common `.git/info/exclude`
    that it does honour is shared with the developer's own checkout — so the ignore
    route either does nothing or reaches further than the supervisor should.

    Verification is in two parts because they answer different questions. The index
    check asks whether *this* commit is clean. The history check asks whether the
    BRANCH is, and a worker that ran `git add -A` itself may already have committed its
    seat. Salvage cannot undo that, so it says so.
    """
    # Stage everything, then take the seat back out. The obvious version —
    # `git add -A -- . ':(exclude).cursor/mcp.json'` — FAILS outright when the seat is
    # gitignored, which is the normal case: naming an ignored path in a pathspec makes
    # git refuse the whole command ("paths are ignored by one of your .gitignore
    # files"). So the two situations need one code path that handles both, and this is
    # it: `-A` never stages an ignored file without `-f`, and the reset removes the
    # seat in the case where it is not ignored and did get staged.
    _git(worktree, "add", "-A", "--", ".")
    _git(worktree, "reset", "-q", "--", *SEAT_FILES, check=False)

    staged = [f for f in _git(worktree, "diff", "--cached", "--name-only").splitlines() if f]

    # The verification, not the staging, is the guarantee. Staging strategies are the
    # kind of thing that quietly stops working across git versions and ignore rules;
    # this is the assertion that notices.
    leaked = sorted(set(staged) & set(SEAT_FILES))
    if leaked:
        _git(worktree, "reset", "-q")
        raise GitError(
            f"refusing to salvage: {leaked} reached the index and must not be committed"
        )

    history = _tracked_in_head(worktree, SEAT_FILES)

    if not staged:
        return Salvage(committed=False, commit=None, credential_in_history=history)

    _git(worktree, "commit", "-q", "-m", message or "WIP: salvaged by gbfleet")
    commit = _git(worktree, "rev-parse", "HEAD").strip()

    in_commit = _git(worktree, "show", "--pretty=format:", "--name-only", commit).splitlines()
    still_leaked = sorted(set(f for f in in_commit if f) & set(SEAT_FILES))
    if still_leaked:  # pragma: no cover
        raise GitError(f"salvage commit {commit} contains {still_leaked}")

    # `history` is read before the commit and reused after it deliberately. `ls-tree -r
    # HEAD` lists the whole tree, and committing staged changes preserves everything
    # else in it, so the two answers are identical by construction. Asking twice is
    # duplication — and duplication is what let a sabotage of one call site pass while
    # the other kept the test green.
    return Salvage(
        committed=True, commit=commit, files=staged, credential_in_history=history
    )


@dataclass(frozen=True)
class Reaped:
    disposition: Disposition
    branch: str
    salvage: Salvage | None = None
    removed: bool = False
    reason: str = ""


def reap(wt: Worktree, message: str | None = None) -> Reaped:
    """Classify, salvage if there is anything to save, then remove the worktree.

    Removal is never forced, and the leftover check above is what that actually rests
    on. Measured: `git worktree remove` without `--force` still deletes IGNORED files
    without complaint, so git's own refusal covers less than it sounds like it does —
    it protects tracked modifications and non-ignored untracked files, and nothing
    else. The explicit check is the guard; not passing `--force` is the second line.

    Either way an unexpected leftover reports `LEFT_DIRTY` and the worktree stays on
    disk for a human. Disk growing is a much smaller problem than work disappearing.
    """
    result: Salvage | None = None
    disposition = Disposition.CLEAN

    if is_dirty(wt.path):
        result = salvage(wt.path, message)
        disposition = Disposition.SALVAGED if result.committed else Disposition.ONLY_CREDENTIAL

    # Walk step 8: after reap, the child's seat file is gone — including the Cursor
    # case, which is the one that lives inside the worktree.
    for rel in SEAT_FILES:
        seat = wt.path / rel
        if seat.exists():
            seat.unlink()

    leftover = porcelain(wt.path) + seats_present(wt.path)
    if leftover:
        return Reaped(
            disposition=Disposition.LEFT_DIRTY,
            branch=wt.branch,
            salvage=result,
            removed=False,
            reason=f"unexpected content after salvage, not forcing removal: {leftover[:5]}",
        )

    removal = subprocess.run(
        ["git", "worktree", "remove", str(wt.path)],
        cwd=str(wt.repo),
        capture_output=True,
        text=True,
    )
    if removal.returncode != 0:
        # `git worktree lock` is a deliberate human act meaning do not touch this, and
        # `--force --force` would walk straight past it. Reporting beats forcing, and
        # beats raising: the salvage above already succeeded, and turning a tidy-up
        # failure into an exception would lose that result.
        return Reaped(
            disposition=Disposition.LEFT_DIRTY,
            branch=wt.branch,
            salvage=result,
            removed=False,
            reason=f"git refused to remove the worktree: {removal.stderr.strip()}",
        )
    return Reaped(disposition=disposition, branch=wt.branch, salvage=result, removed=True)


@dataclass(frozen=True)
class Orphan:
    """A branch a worker left behind, with no worktree checked out on it."""

    branch: str
    commit: str
    subject: str
    salvaged: bool


def _checked_out_branches(repo: Path) -> set[str]:
    out = _git(repo, "worktree", "list", "--porcelain")
    return {
        line.split("refs/heads/", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("branch refs/heads/")
    }


def orphans(repo: Path) -> list[Orphan]:
    """Every `gb/` branch with no worktree on it.

    Mechanical and complete: which branches exist, and which are unattended. What is
    deliberately NOT here is any judgement about whether a half-finished diff is worth
    continuing — resuming an item another agent has already rebuilt is how two
    divergent solutions appear. The supervisor offers; the planner decides.
    """
    attached = _checked_out_branches(repo)
    rows = _git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)%09%(objectname)%09%(subject)",
        f"refs/heads/{BRANCH_PREFIX}",
    ).splitlines()

    found = []
    for row in rows:
        if not row.strip():
            continue
        branch, commit, subject = (row.split("\t", 2) + ["", ""])[:3]
        if branch in attached:
            continue
        found.append(
            Orphan(
                branch=branch,
                commit=commit,
                subject=subject,
                salvaged=subject.startswith("WIP: salvaged by gbfleet"),
            )
        )
    return sorted(found, key=lambda o: o.branch)
