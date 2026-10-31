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

import os
import re
import subprocess
from dataclasses import dataclass, field, replace
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
    ".agents/mcp_config.json",
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


class WorkspaceUnwritable(GitError):
    """The worktree pool cannot be created. Not a git bug — `--workspace` is wrong.

    The measured case (GRPH-826): Grok's sandbox cannot write gbfleet's sibling default,
    and spawn reported `git worktree add` 128. The planner retried git. The flag is
    `--workspace`.
    """


class SeatPathIsTracked(GitError):
    """The repository itself commits a file the supervisor must write a seat into.

    There is no safe way through, which is why this refuses rather than choosing one:

    * write the seat and the repository's own file is destroyed. `seat.write` truncates,
      and these paths are not only credentials — `.grok/config.toml` is where a repo
      states its `[permission]` rules, so a committed `deny` rule protecting the repo
      would be removed by the supervisor without a word. It would not even show up as a
      modification, because `SEAT_FILES` is excluded from salvage on purpose.
    * skip the seat and the child starts with no tools and no error, which is the exact
      failure GRPH-575 was about.

    Refusing is loud and takes ten seconds to fix. Either alternative is silent and
    changes what the worker is allowed to do.
    """


class ResumeFailed(RuntimeError):
    """Could not check out a salvage branch. Spawn from HEAD instead; do not abort."""


class BranchExists(RuntimeError):
    """Refuse to spawn onto a branch somebody else already owns.

    Never force and never auto-suffix: both silently attach a worker to somebody
    else's history, and the second one also makes the branch name stop identifying
    the agent, which is the only thing it is for.
    """


class RemoteBranchTaken(BranchExists):
    """This name is already on the remote. The slot loop takes the next one.

    A local collision and a remote collision are the same refusal (GRPH-1088). The
    subclass is how the slot loop records WHY — a push onto the old name can
    fast-forward when the old tip is an ancestor of the new cut, so the refusal
    has to happen here, at the name. An `ls-remote` that fails is not this class:
    that is unknown, and `create` raises `GitError` so the wave stops on one line
    instead of burning every slot.
    """


@dataclass(frozen=True)
class RemoteName:
    """Whether `branch` is already a head on the remote.

    `known=False` means the question was not answered. That is not "the name is
    free". A repository with no remote is known: there is nowhere for the name
    to already be.
    """

    known: bool
    exists: bool
    remote: str = ""
    sha: str = ""
    reason: str = ""


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
    # Before any worktree exists. A name the remote already has is an earlier run's
    # branch (GRPH-1088). Cutting it and pushing later fast-forwards that ref when
    # the old tip is an ancestor — `push_branch` is right to allow that for a branch
    # this process just published, so the refusal is choosing a different name.
    named = remote_name(repo, branch)
    if not named.known:
        raise GitError(
            f"could not ask {named.remote} whether {branch} already exists "
            f"({named.reason}); not cutting a name that might be an earlier run's"
        )
    if named.exists:
        raise RemoteBranchTaken(
            f"{branch} exists on {named.remote} at {named.sha[:12]}; refusing to "
            "cut onto a branch the remote already has"
        )
    base_sha = _git(repo, "rev-parse", base).strip()

    # Checked against the base COMMIT, before the worktree exists. Checking afterwards
    # would leave a worktree behind on the failure path, and checking the working tree
    # rather than the commit would miss it on a clean checkout — which is every child.
    tracked = _tracked_at(repo, base_sha, SEAT_FILES)
    if tracked:
        raise SeatPathIsTracked(
            f"{repo} commits {tracked} at {base_sha[:12]}, and the supervisor has to "
            f"write a child's seat to that path. Writing it would truncate the "
            f"repository's own file — including any `[permission]` deny rules it sets "
            f"for its agents — and salvage excludes these paths, so nothing would ever "
            f"report the loss. Not writing it leaves the child with no tools and no "
            f"error.\n"
            f"Remedy: stop committing {tracked} (user-scope config does the same job — "
            f"`grok mcp add --scope user`), or run the fleet against a checkout that "
            f"does not."
        )

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


def seat_key(path: Path, worktree: Path) -> str | None:
    """`path`'s key in `SEAT_FILES`, or None when it is not inside `worktree`.

    `SEAT_FILES` holds forward slashes because every production comparison is against
    **git** output, which always uses them. A path derived from the FILESYSTEM does not:
    `Path.relative_to()` yields `.grok\\config.toml` on Windows, and `in SEAT_FILES` is
    then quietly False.

    Nothing was broken by that yet — the existing call sites all compare git output — but
    "correct by luck" is a poor guarantee, and the first caller to reach for
    `relative_to` gets a silent no on one platform. This is the comparison to use.
    """
    try:
        relative = Path(path).resolve().relative_to(Path(worktree).resolve())
    except ValueError:
        return None
    return relative.as_posix()


def is_seat_file(path: Path, worktree: Path) -> bool:
    """Whether `path` is one of the credential files salvage must exclude."""
    key = seat_key(path, worktree)
    return key is not None and key in SEAT_FILES


def is_seat_relative(name: str) -> bool:
    """Whether this repo-relative name is a seat file, even with Windows separators.

    Git itself reports forward slashes. A filesystem-derived or Windows-mocked name
    can carry backslashes; `set(staged) & set(SEAT_FILES)` then misses it and salvage
    would commit a live credential (GRPH-588 bounce).
    """
    return name.replace("\\", "/") in SEAT_FILES


def _tracked_at(gitdir: Path, ref: str, paths: tuple[str, ...]) -> list[str]:
    """Which of `paths` the commit `ref` tracks. Empty on a repository with no commits."""
    if not _git(gitdir, "rev-parse", "--quiet", "--verify", ref, check=False):
        return []
    listed = _git(gitdir, "ls-tree", "-r", "--name-only", ref).splitlines()
    return sorted(set(listed) & set(paths))


def _tracked_in_head(worktree: Path, paths: tuple[str, ...]) -> list[str]:
    return _tracked_at(worktree, "HEAD", paths)


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
    leaked = sorted(f for f in staged if is_seat_relative(f))
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
    still_leaked = sorted(f for f in in_commit if f and is_seat_relative(f))
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


def _empty_diff_shape() -> dict:
    return {
        "files_added": 0, "files_modified": 0, "files_deleted": 0, "files_renamed": 0,
        "test_files": 0, "net_lines": 0, "layers": [],
        "added": [], "modified": [], "deleted": [], "renamed": [], "paths": [],
    }


_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+$|_test\.(py|ts|tsx|js)$|"
    r"\.test\.(ts|tsx|js)$|\.spec\.(ts|tsx|js)$",
    re.I,
)


def _layer_of(path: str) -> str | None:
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if "/routers/" in f"/{p}/" or p.endswith("docs/api-reference.md") or p == "docs/api-reference.md":
        return "B1"
    if p.endswith("mcp_server.py") or p.endswith("tool_tiers.py"):
        return "B2"
    if "/services/" in f"/{p}/" and p.endswith(".py"):
        return "B3"
    if "/models/" in f"/{p}/" or "models/__init__" in p or "alembic/versions" in p:
        return "B4"
    if p.startswith("web/src/features") or p.startswith("web/src/lib"):
        return "B5"
    if p.startswith("web/src/components/ui") or p.endswith(".css"):
        return "B6"
    if p.startswith("fleet/src/gbfleet/") or p.startswith("cli/") or p.startswith("fleet/"):
        return "B7"
    if p.startswith(".github/") or p.startswith("docker") or p.startswith("pyproject"):
        return "B8"
    return None


def diff_shape_against(cwd: Path, base: str, head: str = "HEAD") -> dict:
    """`git diff --name-status --stat` against the worktree's base (PRD-41 S1).

    Counts are the contract; path lists travel with them so the server can derive
    capabilities without a second git. A missing base is an empty shape, not a zero
    invented from a failed command.
    """
    if not base:
        return _empty_diff_shape()
    status = _git(cwd, "diff", "--name-status", f"{base}...{head}", check=False)
    stat = _git(cwd, "diff", "--stat", f"{base}...{head}", check=False)
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0][:1]
        if code == "A" and len(parts) >= 2:
            added.append(parts[1])
        elif code == "M" and len(parts) >= 2:
            modified.append(parts[1])
        elif code == "D" and len(parts) >= 2:
            deleted.append(parts[1])
        elif code in ("R", "C") and len(parts) >= 3:
            renamed.append(parts[2])
        elif len(parts) >= 2:
            modified.append(parts[-1])
    insertions = deletions = 0
    if stat.strip():
        tail = stat.strip().splitlines()[-1]
        ins = re.search(r"(\d+) insertion", tail)
        dele = re.search(r"(\d+) deletion", tail)
        insertions = int(ins.group(1)) if ins else 0
        deletions = int(dele.group(1)) if dele else 0
    paths = added + modified + deleted + renamed
    layers: list[str] = []
    for path in paths:
        layer = _layer_of(path)
        if layer and layer not in layers:
            layers.append(layer)
    return {
        "files_added": len(added),
        "files_modified": len(modified),
        "files_deleted": len(deleted),
        "files_renamed": len(renamed),
        "test_files": sum(1 for p in paths if _TEST_PATH.search(p)),
        "net_lines": insertions - deletions,
        "layers": layers,
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "renamed": renamed,
        "paths": paths,
    }


@dataclass(frozen=True)
class Reaped:
    disposition: Disposition
    branch: str
    salvage: Salvage | None = None
    removed: bool = False
    reason: str = ""
    #: PRD-41 S1. Always a dict; an empty one means "nothing against the base", which is
    #: a real measurement, not a missing post.
    diff_shape: dict = field(default_factory=_empty_diff_shape)
    #: GRPH-948: `(item, branch)` for every held item after the first, each salvaged onto a
    #: branch of its own. Empty when one item (or none) was held, or nothing was salvaged.
    siblings: tuple[tuple[str, str], ...] = ()
    #: Why a sibling could not be written, or "". Never silent: an item with no salvage
    #: branch of its own is an item `choose_resume` cannot find.
    split_reason: str = ""


@dataclass(frozen=True)
class Pushed:
    """What became of the attempt to put a branch where a reviewer can read it.

    `skipped` is a real outcome and is not `ok`: a branch with nothing beyond its base has
    nothing to publish, and saying "pushed" about it would make the two cases look alike.
    """

    branch: str
    remote: str = ""
    ok: bool = False
    skipped: bool = False
    reason: str = ""


def commits_beyond_base(repo: Path | str, branch: str, base: str) -> int:
    """How many commits the branch carries that its base does not."""
    out = _git(repo, "rev-list", "--count", f"{base}..{branch}", check=False).strip()
    try:
        return int(out)
    except ValueError:
        return 0


def remote_name(repo: Path | str, branch: str) -> RemoteName:
    """Ask the remote whether `branch` is already a head.

    `ls-remote`'s pattern is broader than one ref, so the answer is the line whose
    ref is exactly `refs/heads/<branch>`. No remote is a known absence. A non-zero
    exit is unknown, and the reason is kept so the wave can say why it stopped.
    """
    remote = remote_for(repo)
    if not remote:
        return RemoteName(known=True, exists=False)
    proc = subprocess.run(
        ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        if len(reason) > 200:
            reason = reason[:200]
        return RemoteName(
            known=False, exists=False, remote=remote,
            reason=reason or "ls-remote failed",
        )
    want = f"refs/heads/{branch}"
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == want:
            return RemoteName(known=True, exists=True, remote=remote, sha=parts[0])
    return RemoteName(known=True, exists=False, remote=remote)


def remote_for(repo: Path | str) -> str:
    """The remote this repository pushes to, or "" when it has none.

    `origin` when it exists, else the only remote there is. Two-or-more without an `origin`
    is not guessed at: pushing a worker's branch to the wrong place is worse than not
    pushing, and the refusal names what it found.
    """
    remotes = [r for r in _git(repo, "remote", check=False).splitlines() if r.strip()]
    if "origin" in remotes:
        return "origin"
    return remotes[0] if len(remotes) == 1 else ""


def divert_branch(repo: Path | str, branch: str, item: str = "") -> str:
    """A new branch at `branch`'s tip. `branch` itself is not moved.

    The salvage that must not be published under `branch` still has to exist somewhere
    a person can find it (GRPH-830). Forcing `branch` to stay put and forcing this name
    onto an existing commit are different acts: an existing side branch at another
    commit is somebody's work, so the name grows the tip sha rather than being reset.
    """
    sha = _git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}").strip()
    label = re.sub(r"[^A-Za-z0-9]+", "-", item or "late").strip("-") or "late"
    name = f"{branch}{ITEM_BRANCH_SEP}{label}--late"
    if branch_exists(repo, name):
        have = _git(repo, "rev-parse", "--verify", f"{name}^{{commit}}").strip()
        if have == sha:
            return name
        name = f"{name}-{sha[:8]}"
        if branch_exists(repo, name):
            have = _git(repo, "rev-parse", "--verify", f"{name}^{{commit}}").strip()
            if have != sha:
                raise GitError(
                    f"{name} exists at {have[:12]}, not {sha[:12]}; not moving it"
                )
            return name
    _git(repo, "branch", name, sha)
    return name


def _commit_sha(repo: Path | str, ref: str) -> str:
    if not (ref or "").strip():
        return ""
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                check=False).strip()


def _remote_tip_in_trunk(repo: Path | str, branch: str, tip: str) -> bool:
    """True when the remote ref already points at a trunk commit, and `tip` is not it.

    Pushing `branch` would move that ref. Asked only when the ledger could not be read:
    an unfetched evidence sha is omitted from a list that was actually read, and this
    is not that question. A remote ref this clone has never fetched is not "in the
    trunk" — nothing is diverted for a ref that is not here.
    """
    remote = remote_for(repo)
    if not remote or not tip:
        return False
    remote_tip = _commit_sha(repo, f"{remote}/{branch}")
    if not remote_tip or remote_tip == tip:
        return False
    trunk = default_ref(repo, remote)
    if not trunk:
        return False
    return reaches(repo, trunk, remote_tip) is True


def salvage_push_target(repo: Path | str, branch: str, base: str,
                        landed: list[str] | None, *, item: str = "") -> tuple[str, str]:
    """Where a salvage — or a later rework — may be pushed (GRPH-1053).

    Returns `(branch_to_push, reason)`. An empty reason means `branch` itself is safe.

    `landed` is commits of this item that are already in the trunk. `None` is not an
    empty list: the evidence was not read (`NotPermitted`, the tool missing from the
    client's allowlist, the server dark). An empty list is a read that named nothing
    this clone can see, including a sha it has never fetched. Those two must not take
    the same path — the empty list still publishes `branch`, and `None` must not move
    a ref whose tip is already in the trunk.

    Two pushes lose one:

    * The cut `base` does not contain a landed commit, and neither does `branch`.
      Pushing `branch` publishes a line written before the fix, under the name the fix
      uses.
    * `branch` contains the landed commit and the tip has moved past it — a salvage
      commit, or a child that kept editing after the fix landed. Pushing fast-forwards
      the fix's branch onto that tip.

    A salvage tip that sits on earlier non-salvage commits diverts even when nothing
    has reached the trunk yet: the WIP becomes the tip a reviewer would merge, which
    is how salvage commits landed on top of real fixes. The ordinary salvage — the
    only commit beyond `base` — still pushes as `branch`. That is the commit the next
    child resumes, and diverting it would hide the only copy of the work.

    A landed commit this clone has never fetched is not in `landed`. Unknown is not
    "missing", and treating it as missing would divert every salvage on a fresh clone.
    """
    tip = _commit_sha(repo, branch)
    if not tip:
        return branch, ""
    subject = _git(repo, "log", "-1", "--format=%s", branch, check=False).strip()
    salvage_tip = is_salvage_subject(subject)
    beyond = commits_beyond_base(repo, branch, base) if base else 0
    on_real_work = salvage_tip and beyond > 1
    reason = ""
    if landed is None:
        # Not "nothing landed". The read did not happen. A ref already in the trunk
        # is not moved; an unfetched sha never arrives here, because that read
        # returned a list with the sha left out.
        if _remote_tip_in_trunk(repo, branch, tip):
            reason = ("tip would move a branch already in the trunk; "
                      "the landed-commit check did not run")
    else:
        known = [sha for raw in landed if (sha := _commit_sha(repo, raw))]
        # Landed *after* this cut: the base does not contain them. A commit the base
        # already has is history the child started from, not a fix this push would cover.
        landed_after_cut = [
            sha for sha in known
            if base and reaches(repo, base, sha) is False
        ]
        for sha in landed_after_cut:
            contained = reaches(repo, branch, sha)
            if contained is False:
                reason = f"base does not contain landed {sha[:12]}"
                break
            if contained is True and sha != tip:
                reason = f"tip moves past landed {sha[:12]}"
                break
    if not reason and on_real_work:
        reason = "salvage commit sits on top of earlier work"
    if not reason:
        return branch, ""
    return divert_branch(repo, branch, item), reason


def push_branch(repo: Path | str, branch: str, base: str) -> Pushed:
    """Put a reaped branch where the reviewer can read it (GRPH-750).

    **Salvage commits; this publishes.** They are separate steps because they answer to
    different things: committing is local bookkeeping the supervisor owes the work, and
    pushing reaches outside the machine. Keeping them apart is also what lets a vendor that
    pushes for itself (claude, cursor and qwen-code all have shells) make this a no-op rather
    than a duplicate.

    Pushed when the branch is AHEAD of its base, whoever committed it — that, not who did the
    committing, is the condition for "there is work a reviewer needs to see". Already-pushed
    is a cheap no-op on git's side.

    Uses whatever git credential the operator's own session already has. The supervisor holds
    a Graphban API key and no git credential, and inventing one here would be a second
    credential path for a thing `git push` already does correctly.
    """
    ahead = commits_beyond_base(repo, branch, base) if base else 0
    if not ahead:
        return Pushed(branch=branch, skipped=True,
                      reason="nothing beyond its base to publish")
    remote = remote_for(repo)
    if not remote:
        return Pushed(branch=branch, skipped=True,
                      reason="this repository has no remote (or several and no `origin`), so "
                             "the branch stays local and the reviewer cannot read it")
    try:
        _git(repo, "push", "-q", remote, f"{branch}:{branch}")
    except GitError as exc:
        # Loud, never silent. A branch that did not reach the remote is invisible to review,
        # and a supervisor that swallowed the failure would leave the reviewer to infer it
        # from an empty diff — which is exactly the defect this exists to close.
        return Pushed(branch=branch, remote=remote, reason=f"push refused: {str(exc)[:200]}")
    return Pushed(branch=branch, remote=remote, ok=True)


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
    # Diff shape is taken AFTER salvage and BEFORE removal: uncommitted work is now on
    # the branch, and the worktree still exists to be asked.
    shape = diff_shape_against(wt.path, wt.base or "", "HEAD")
    if leftover:
        return Reaped(
            disposition=Disposition.LEFT_DIRTY,
            branch=wt.branch,
            salvage=result,
            removed=False,
            reason=f"unexpected content after salvage, not forcing removal: {leftover[:5]}",
            diff_shape=shape,
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
            diff_shape=shape,
        )
    return Reaped(disposition=disposition, branch=wt.branch, salvage=result, removed=True,
                  diff_shape=shape)


#: Salvage commits name the items they held, so the next spawn can resume the
#: right branch (P30 D9). `WIP: salvaged by gbfleet (fake) items=GRPH-1` — one item since
#: GRPH-948; the comma form is still parsed, because older salvage branches carry it.
_ITEMS_IN_SUBJECT = re.compile(r"\bitems=([A-Za-z0-9][A-Za-z0-9_,-]*)")


@dataclass(frozen=True)
class Orphan:
    """A branch a worker left behind, with no worktree checked out on it."""

    branch: str
    commit: str
    subject: str
    salvaged: bool
    #: Item keys parsed from the salvage subject. Empty when the commit pre-dates
    #: D9 or the child held nothing. Several orphans may name one item.
    item_keys: tuple[str, ...] = ()
    #: Unix committer time. Newest salvage for one item is the one to resume.
    committed_at: int = 0


def _checked_out_branches(repo: Path) -> set[str]:
    out = _git(repo, "worktree", "list", "--porcelain")
    return {
        line.split("refs/heads/", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("branch refs/heads/")
    }


def registered_worktrees(repo: Path) -> set[Path]:
    """The paths git itself lists as worktrees of `repo`, resolved.

    For anything about to DELETE a directory on the strength of a path it read out of a
    JSON file in the temp directory (GRPH-1011's `gc`, which walks rosters no supervisor
    is answerable for). `reap` runs `git status` and then `git add -A` + commit inside the
    path before it ever asks git to remove it, so a path that is not one of ours is not a
    no-op: it is a salvage commit in somebody else's checkout. Asking git first is what
    keeps the deletion scoped to trees this repository cut.

    Includes the MAIN working tree, which is the entry a caller must not act on — the
    clone is another agent's supervisor, and one lock per repository is what lets two
    agents work at once.
    """
    out = _git(repo, "worktree", "list", "--porcelain")
    prefix = "worktree "
    return {
        Path(line[len(prefix):].strip()).resolve()
        for line in out.splitlines()
        if line.startswith(prefix)
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
        "--format=%(refname:short)%09%(objectname)%09%(committerdate:unix)%09%(subject)",
        f"refs/heads/{BRANCH_PREFIX}",
    ).splitlines()

    found = []
    for row in rows:
        if not row.strip():
            continue
        branch, commit, when, subject = (row.split("\t", 3) + ["", "", "", ""])[:4]
        if branch in attached:
            continue
        found.append(
            Orphan(
                branch=branch,
                commit=commit,
                subject=subject,
                salvaged=subject.startswith("WIP: salvaged by gbfleet"),
                item_keys=_item_keys(subject),
                committed_at=int(when) if str(when).isdigit() else 0,
            )
        )
    return sorted(found, key=lambda o: o.branch)


def _item_keys(subject: str) -> tuple[str, ...]:
    found = _ITEMS_IN_SUBJECT.search(subject or "")
    if not found:
        return ()
    return tuple(k for k in found.group(1).split(",") if k)


def salvage_message(adapter: str, item_ids: list[str] | tuple[str, ...] = ()) -> str:
    """The salvage commit subject. Item keys make D9 resume possible.

    ONE item at most (GRPH-948). The reported salvage named three items in one commit and
    became one PR carrying all of them; `reap_held` writes one commit per item instead, and
    this refuses rather than quietly joining a list again.
    """
    if len(item_ids) > 1:
        raise ValueError(f"a salvage commit names one item, not {len(item_ids)}: {list(item_ids)}")
    msg = f"WIP: salvaged by gbfleet ({adapter})"
    if item_ids:
        msg += " items=" + item_ids[0]
    return msg


#: `gb/w-1` held GRPH-1 and GRPH-2: GRPH-2's work goes on `gb/w-1--GRPH-2`. Under the same
#: prefix, so `orphans` lists it and `choose_resume` can pick it for its own item.
ITEM_BRANCH_SEP = "--"


def item_branch(branch: str, item: str) -> str:
    """The branch that carries `item` alone, split off `branch` (GRPH-948)."""
    return f"{branch}{ITEM_BRANCH_SEP}{item}"


def reap_held(wt: Worktree, adapter: str, items: list[str] | tuple[str, ...] = ()) -> Reaped:
    """`reap`, writing one salvage commit per held item and never one naming several (GRPH-948).

    The first item's commit lands on the worker's own branch, as before. Every other item gets
    the SAME tree committed onto the same parent, under its own subject, on
    `item_branch(branch, item)`. The tree is not divided: which uncommitted file belonged to
    which item is not something the supervisor can know, and guessing is how work is lost.
    Each item resumes from all of it; what it no longer does is share a commit, and so a PR,
    with the others.
    """
    held = [i for i in dict.fromkeys(items) if i]
    reaped = reap(wt, message=salvage_message(adapter, held[:1]))
    commit = reaped.salvage.commit if reaped.salvage and reaped.salvage.committed else None
    if not commit or len(held) < 2:
        return reaped
    siblings: list[tuple[str, str]] = []
    failed: list[str] = []
    for item in held[1:]:
        name = item_branch(wt.branch, item)
        try:
            sha = _git(wt.repo, "commit-tree", f"{commit}^{{tree}}", "-p", f"{commit}^",
                       "-m", salvage_message(adapter, [item])).strip()
            # Never forced: a branch already there is somebody's work.
            _git(wt.repo, "branch", name, sha)
        except GitError as exc:
            failed.append(f"{item}: {str(exc)[:160]}")
            continue
        siblings.append((item, name))
    return replace(reaped, siblings=tuple(siblings), split_reason="; ".join(failed))


def is_salvage_subject(subject: str) -> bool:
    """True when `subject` is a salvage commit, not shipped work.

    Used so a salvage HEAD is never opened as a PR: those drafts were getting merged
    under a `WIP: salvaged by gbfleet` title. The prefix is the whole contract —
    `salvage_message` is the only writer.
    """
    return (subject or "").startswith("WIP: salvaged by gbfleet")


def is_ancestor(repo: Path, ancestor: str, commit: str) -> bool:
    """True when `ancestor` is `commit` or an ancestor of it."""
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, commit],
        cwd=str(repo), capture_output=True, text=True,
    )
    return proc.returncode == 0


def resume(repo: Path, path: Path, orphan: Orphan) -> Worktree:
    """`git worktree add` of an existing salvage branch (P30 D9).

    The dirty tree is already gone; salvage committed first. Missing branch, a
    branch that is not a descendant of the recorded salvage commit, or add
    failing: ResumeFailed — the caller spawns from HEAD and leaves this ref listed.
    Never force, never auto-suffix, never rebase.
    """
    repo = Path(repo)
    path = Path(path)
    if not branch_exists(repo, orphan.branch):
        raise ResumeFailed(f"{orphan.branch} is gone; not resuming")
    if orphan.commit and not is_ancestor(repo, orphan.commit, orphan.branch):
        raise ResumeFailed(
            f"{orphan.branch} is not a descendant of salvage {orphan.commit[:12]}; "
            "not resuming"
        )
    try:
        _git(repo, "worktree", "add", "-q", str(path), orphan.branch)
    except GitError as exc:
        raise ResumeFailed(f"{orphan.branch}: worktree add failed ({exc})") from exc
    sha = _git(repo, "rev-parse", f"refs/heads/{orphan.branch}").strip()
    return Worktree(path=path, branch=orphan.branch, repo=repo, base=sha)


def bounce_branch_of(row: dict | None) -> str:
    """The recorded branch of a bounced item, or "".

    A bounce of a pushed PR is not a salvage: `bounce_reason` is what `was_bounced`
    reads, and `branch` is the head the next spawn must resume. Either missing is
    not a bounce-resume — cutting from the base is then the first-spawn path.
    """
    if not row:
        return ""
    if not str(row.get("bounce_reason") or "").strip():
        return ""
    return str(row.get("branch") or "").strip()


def _ensure_local_branch(repo: Path, branch: str) -> None:
    """Make `branch` a local ref, fetching from the remote when that is where it lives."""
    if branch_exists(repo, branch):
        return
    remote = remote_for(repo)
    if remote:
        subprocess.run(
            ["git", "fetch", "--quiet", remote, branch],
            cwd=str(repo), capture_output=True, text=True,
        )
        tracking = f"refs/remotes/{remote}/{branch}"
        if _git(repo, "rev-parse", "--verify", "--quiet", tracking, check=False).strip():
            _git(repo, "branch", branch, f"{remote}/{branch}")
            return
    raise ResumeFailed(
        f"{branch} is gone locally and not on the remote; not resuming"
    )


def resume_bounced(repo: Path, path: Path, branch: str, base: str = "") -> Worktree:
    """Checkout the item's bounced branch and merge `base` into it (GRPH-1031).

    A bounce of a pushed PR is not a salvage: the head is on the remote, the item is
    `next`, and `create` would cut a sibling from main — which is how reviewed work
    was rebuilt and regressed. Never force, never auto-suffix, never rebase.

    Merge conflicts stay in the tree. The history is this item's own work; the child
    is told so and resolves them. Aborting to a fresh cut from `base` is the defect
    this exists to close.
    """
    repo = Path(repo)
    path = Path(path)
    branch = (branch or "").strip()
    if not branch:
        raise ResumeFailed("no bounced branch to resume")
    if path.exists():
        raise ResumeFailed(f"{path} already exists; not resuming {branch}")
    _ensure_local_branch(repo, branch)
    try:
        _git(repo, "worktree", "add", "-q", str(path), branch)
    except GitError as exc:
        raise ResumeFailed(f"{branch}: worktree add failed ({exc})") from exc
    merge_from = (base or "HEAD").strip()
    try:
        base_sha = _git(repo, "rev-parse", merge_from).strip()
    except GitError as exc:
        raise ResumeFailed(f"{branch}: could not resolve base {merge_from} ({exc})") from exc
    # Conflicts are left for the child (returncode 1). Anything else is git failing
    # to answer, and still must not fall through to a sibling cut from the base.
    proc = subprocess.run(
        ["git", "merge", "--no-edit", base_sha],
        cwd=str(path), capture_output=True, text=True,
    )
    if proc.returncode not in (0, 1):
        raise ResumeFailed(
            f"{branch}: merge of {merge_from} failed ({proc.stderr.strip() or proc.returncode})"
        )
    return Worktree(path=path, branch=branch, repo=repo, base=base_sha)


def choose_resume(
    found: list[Orphan],
    items: dict[str, dict],
    *,
    lease_seconds: int = 1800,
    now: int | None = None,
) -> list[Orphan]:
    """Newest salvage orphan per open unclaimed item (P30 D9).

    `items` is id -> {status, claimed_by[, claimed_at]}. Missing, done, review, or
    blocked: leave the branch listed, do not reuse. `in_progress` with a LIVE holder
    also skips — but a stale lease (claimed_at older than `lease_seconds`) or no holder
    at all is eligible, because the row is claimable on the server by the same clock
    (GRPH-850). `blocked` at death does not burn the branch — once the item is
    next/backlog and unclaimed, it is eligible.
    """
    import time as _time
    now = now if now is not None else int(_time.time())
    cutoff = now - lease_seconds
    eligible: dict[str, list[Orphan]] = {}
    for orphan in found:
        if not orphan.salvaged or not orphan.item_keys:
            continue
        for key in orphan.item_keys:
            row = items.get(key)
            if not row:
                continue
            status = str(row.get("status") or "")
            if status in ("done", "review", "blocked"):
                continue
            claimed_by = row.get("claimed_by") or ""
            if status == "in_progress":
                # Eligible only when the holder's lease has expired or no holder exists.
                # A live holder means another agent is actively working; resuming under
                # them would produce two workers on one branch.
                if claimed_by:
                    claimed_at = row.get("claimed_at")
                    if claimed_at is None:
                        continue
                    try:
                        if int(claimed_at) > cutoff:
                            continue
                    except (TypeError, ValueError):
                        continue
            elif status in ("next", "backlog"):
                if claimed_by:
                    continue
            else:
                continue
            eligible.setdefault(key, []).append(orphan)
    picked: list[Orphan] = []
    seen: set[str] = set()
    for key, group in eligible.items():
        newest = max(group, key=lambda o: o.committed_at)
        if newest.branch in seen:
            continue
        seen.add(newest.branch)
        picked.append(newest)
    return picked


def take_resume(resumes: list[Orphan], item_id: str | None) -> Orphan | None:
    """The salvage this spawn may start on, removed from `resumes`.

    A seat bound to an item takes the salvage whose subject names that item and nothing
    else. The next orphan in the list is a nearest match, and that is how one child's
    work was resumed under a different item (GRPH-1053). A subject that names several
    items is not taken for any of them: one commit attributed to each of its keys is
    the same defect.

    An unbound seat still takes the next eligible salvage. `up` does not know the item
    until the child claims it (P30 D9); refusing every resume there would cut from HEAD
    and rebuild the work the salvage exists to keep.
    """
    wanted = (item_id or "").strip()
    if wanted:
        for orphan in resumes:
            if orphan.item_keys == (wanted,):
                resumes.remove(orphan)
                return orphan
        return None
    if not resumes:
        return None
    return resumes.pop(0)


def default_ref(repo: Path | str, remote: str) -> str:
    """The remote's default branch as a ref (`origin/main`), or "" when it cannot be told.

    Read from the remote HEAD symref the clone already recorded, never guessed from a list
    of likely names: a repository whose trunk is `develop` or `master` is not unusual, and a
    guess that happened to match would make the check silently measure nothing everywhere it
    did not.
    """
    if not remote:
        return ""
    out = _git(repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD",
               check=False).strip()
    return out if out.startswith(f"{remote}/") else ""


def refresh_ref(repo: Path | str, remote: str, ref: str) -> bool:
    """Fetch just the default branch, so "behind" is measured against something current.

    **Without this the check is the defect it exists to catch.** A remote-tracking ref is
    only as fresh as the last fetch, and a supervisor that never fetches would measure every
    branch as zero commits behind — absence reading as clean, on a check built to stop
    exactly that.

    One ref, once per wave, best effort. Returns whether it succeeded, because "we could not
    ask" and "nothing has moved" are different facts and the caller reports them differently.
    """
    if not remote or not ref.startswith(f"{remote}/"):
        return False
    branch = ref.split("/", 1)[1]
    proc = subprocess.run(["git", "fetch", "--quiet", remote, branch],
                          cwd=str(repo), capture_output=True, text=True)
    return proc.returncode == 0


class BaseBranchNotFound(GitError):
    """The `--base` branch does not exist on the remote. Refuse at startup."""


def resolve_base(repo: Path | str, remote: str, branch: str) -> str:
    """Resolve a branch name to `origin/<branch>`, fetching it and verifying it exists.

    GRPH-847. A `--base` that does not exist on the remote refuses at startup, naming the
    branch; it does not fall back to the default ref. A silent fallback would cut children
    from the wrong base and the dependency check would measure against the wrong ref —
    the exact absence-reads-as-clean failure the whole chain exists to prevent.
    """
    if not remote:
        raise BaseBranchNotFound(
            f"--base {branch}: repository has no remote to resolve it against"
        )
    ref = f"{remote}/{branch}"
    proc = subprocess.run(
        ["git", "fetch", "--quiet", remote, branch],
        cwd=str(repo), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise BaseBranchNotFound(
            f"--base {branch}: could not fetch {ref} from {remote} "
            f"({proc.stderr.strip() or 'branch does not exist on the remote'})"
        )
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/{ref}"],
        cwd=str(repo), capture_output=True, text=True,
    )
    if probe.returncode != 0:
        raise BaseBranchNotFound(
            f"--base {branch}: {ref} does not exist on the remote"
        )
    return ref


def behind_ref(repo: Path | str, base: str, ref: str) -> int:
    """How many commits `ref` has that `base` does not — how STALE this work is.

    Counted from the base the worktree was cut from, not from the branch tip: the question
    is what the worker did not have in front of it, and its own commits are not that.

    Zero when the ref cannot be resolved, which is the honest answer for a repository with
    no remote or an unfetched one — and the caller reports "not measured" rather than "not
    behind", because those are different facts (GRPH-786).

    The count is not a verdict on whether the branch can be proposed. A trunk that moved
    in files this branch never touches still counts, and refusing on the count alone is
    what GRPH-1087 stops. `shared_with_missed` and `merge_tree_conflicts` are that check.
    """
    if not base or not ref:
        return 0
    out = _git(repo, "rev-list", "--count", f"{base}..{ref}", check=False).strip()
    try:
        return int(out)
    except ValueError:
        return 0


def changed_paths(repo: Path | str, old: str, new: str) -> list[str] | None:
    """Paths that differ between two commits. None when git cannot answer.

    None is not an empty list. An empty list means the trees were compared and nothing
    differs; None means the comparison was not made, and a caller that treats the two
    alike will propose a branch it never looked at.
    """
    if not (old or "").strip() or not (new or "").strip():
        return None
    proc = subprocess.run(
        ["git", "diff", "--name-only", old, new],
        cwd=str(repo), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def shared_with_missed(repo: Path | str, base: str, ref: str, branch: str) -> list[str] | None:
    """Files the missed commits (`base`..`ref`) change that `branch` also changes.

    Net tree diff, not every path a commit in the range happened to touch: a change the
    trunk made and then reverted is not a file the branch has to reconcile. None when
    either side cannot be listed — "we could not look" is not "no shared files"
    (GRPH-1087).
    """
    missed = changed_paths(repo, base, ref)
    own = changed_paths(repo, base, branch)
    if missed is None or own is None:
        return None
    return sorted(set(missed) & set(own))


def merge_tree_conflicts(repo: Path | str, ref: str, branch: str) -> bool | None:
    """Does `git merge-tree --write-tree ref branch` report a conflict?

    False when git exits 0 (a clean merge). True when it exits 1 (conflicts). None on
    any other status — a missing ref, a git that cannot run the merge — because that is
    "could not be asked", and it must not read as a clean merge (GRPH-1087).

    This is the check the path intersection cannot be: a rename on one side and an edit
    of the old path on the other need not share a name in `git diff --name-only`, and
    they still conflict.
    """
    if not (ref or "").strip() or not (branch or "").strip():
        return None
    proc = subprocess.run(
        ["git", "merge-tree", "--write-tree", ref, branch],
        cwd=str(repo), capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    return None


def conflicted_paths(repo: Path | str, ref: str, branch: str) -> list[str] | None:
    """The files `git merge-tree --write-tree ref branch` reports conflicted (GRPH-1236).

    Empty on a clean merge. None when merge-tree could not be asked, for the same reason
    `merge_tree_conflicts` returns None: "could not ask" is not "clean".
    """
    if not (ref or "").strip() or not (branch or "").strip():
        return None
    proc = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", ref, branch],
        cwd=str(repo), capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return []
    if proc.returncode != 1:
        return None
    # The first line is the tree; the conflicted names follow it, one per line.
    return sorted({line.strip() for line in proc.stdout.splitlines()[1:] if line.strip()})


@dataclass
class Merged:
    """`ref` merged into a branch (GRPH-1236). `sha` is the new head, `ref_sha` is what merged."""

    branch: str
    ref: str
    sha: str = ""
    ref_sha: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.sha)


def _checkout_of(repo: Path | str, branch: str) -> Path | None:
    """The worktree that has `branch` checked out, or None."""
    out = _git(repo, "worktree", "list", "--porcelain", check=False)
    path: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):].strip())
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return None


def _identity_env(cwd: Path | str) -> dict[str, str]:
    """Committer env for a repo with no identity of its own. A real one always wins."""
    env = dict(os.environ)
    if not _git(cwd, "config", "user.email", check=False).strip():
        env.setdefault("GIT_AUTHOR_NAME", "gbfleet")
        env.setdefault("GIT_AUTHOR_EMAIL", "gbfleet@localhost")
        env.setdefault("GIT_COMMITTER_NAME", "gbfleet")
        env.setdefault("GIT_COMMITTER_EMAIL", "gbfleet@localhost")
    return env


def merge_ref_into(repo: Path | str, branch: str, ref: str) -> Merged:
    """Merge `ref` into `branch` with a real merge commit (GRPH-1236). Never a rebase.

    The branch's own commits keep their shas, so nothing already pushed is rewritten and
    the push that follows is a fast-forward. Where the branch is checked out, this is
    `git merge --no-ff --no-edit <ref>` in that worktree, so its files follow the ref.
    A merge that fails is aborted and reported, and the branch is left where it was.

    Where nothing has the branch checked out, the same commit is built from
    `git merge-tree --write-tree` and `git commit-tree` with both parents. The ref only
    moves if it still points at the head the merge was built on.
    """
    head = _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}",
                check=False).strip()
    ref_sha = _git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}",
                   check=False).strip()
    if not head or not ref_sha:
        return Merged(branch=branch, ref=ref, reason=f"could not resolve {branch} or {ref}")
    message = f"Merge {ref} into {branch} before proposing (gbfleet)"
    tree = _checkout_of(repo, branch)
    if tree is not None and tree.exists():
        env = _identity_env(tree)
        proc = subprocess.run(
            ["git", "merge", "--no-ff", "--no-edit", "-m", message, ref_sha],
            cwd=str(tree), capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=str(tree),
                           capture_output=True, text=True)
            why = (proc.stderr.strip() or proc.stdout.strip())[:200]
            return Merged(branch=branch, ref=ref, ref_sha=ref_sha,
                          reason=f"git merge {ref} failed: {why}")
        sha = _git(tree, "rev-parse", "HEAD").strip()
        return Merged(branch=branch, ref=ref, sha=sha, ref_sha=ref_sha)
    written = subprocess.run(["git", "merge-tree", "--write-tree", ref_sha, head],
                             cwd=str(repo), capture_output=True, text=True)
    if written.returncode != 0:
        return Merged(branch=branch, ref=ref, ref_sha=ref_sha,
                      reason=f"merge-tree did not merge {ref} cleanly")
    oid = written.stdout.splitlines()[0].strip()
    made = subprocess.run(
        ["git", "commit-tree", oid, "-p", head, "-p", ref_sha, "-m", message],
        cwd=str(repo), capture_output=True, text=True, env=_identity_env(repo),
    )
    if made.returncode != 0:
        return Merged(branch=branch, ref=ref, ref_sha=ref_sha,
                      reason=f"commit-tree failed: {made.stderr.strip()[:200]}")
    sha = made.stdout.strip()
    try:
        _git(repo, "update-ref", f"refs/heads/{branch}", sha, head)
    except GitError as exc:
        return Merged(branch=branch, ref=ref, ref_sha=ref_sha, reason=str(exc)[:200])
    return Merged(branch=branch, ref=ref, sha=sha, ref_sha=ref_sha)


def reaches(repo: Path | str, base: str, commit: str) -> bool | None:
    """Is `commit` already in `base`? `None` when this clone cannot tell (GRPH-798).

    THREE ANSWERS, NOT TWO, and collapsing them is the whole failure this guards. A commit
    this clone has never fetched is not an unmerged commit — it is an unknown one — and a
    check that read "I could not find it" as "it is not merged" would refuse every wave on a
    fresh clone, while one that read it as "merged" would wave through exactly the dependency
    it exists to catch.
    """
    if not base or not commit:
        return None
    proc = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                          cwd=str(repo), capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    proc = subprocess.run(["git", "merge-base", "--is-ancestor", commit, base],
                          cwd=str(repo), capture_output=True, text=True)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    # Anything else is git failing to answer — a bad base ref, a corrupt object — and that is
    # not evidence either way.
    return None


def paths_unchanged(repo: Path | str, old: str, new: str,
                    paths: list[str] | tuple[str, ...] | None) -> bool:
    """True when `paths` are byte-identical at `old` and `new` (GRPH-1032).

    Empty `paths` cannot prove it. Treating "we did not look" as "nothing changed" is how a
    main-merge that also edited the item would still merge unsigned work.
    """
    clean = [p.strip() for p in (paths or []) if isinstance(p, str) and p.strip()]
    if not clean or not (old or "").strip() or not (new or "").strip():
        return False
    proc = subprocess.run(
        ["git", "diff", "--quiet", old, new, "--", *clean],
        cwd=str(repo), capture_output=True, text=True,
    )
    return proc.returncode == 0
