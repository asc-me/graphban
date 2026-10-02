"""Local JSON of live children, so a crashed supervisor can adopt rather than spawn blind.

P30 D7. Children outlive the supervisor (`JOB_LIMIT_FLAGS = 0`). `up` used to log
`takeover` and start a new wave, which then refused existing `gb/<wave>-<slot>`
branches. The next supervisor has to attach live PIDs and salvage dead ones.

State lives in the existing `gbfleet-{user}/` dir, next to the lock — not the Graphban
DB. Seat **id**, never the enrolment code. Writes go through a temp file then rename
(atomic). A corrupt, truncated, or unknown-generation file is **unadoptable**, not an
empty roster: an empty reading is how a takeover starts blind beside a fleet.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from .hostos import pid_is_alive, process_start_token, restrict_to_owner
from .lock import LockState, sweep_hold
from .observe import emit
from .progress import Output
from .spawn import Child
from .state import NotARepository, lock_path, repo_key, repo_root, state_root
from .worktree import Disposition, Worktree, reap as reap_tree, registered_worktrees

GENERATION = 1

_REQUIRED = ("pid", "worktree", "branch", "adapter")


class UnadoptableFile(RuntimeError):
    """The children file cannot be trusted. Treat it as a full roster we cannot read."""


@dataclass
class Snapshot:
    pid: int
    worktree: str
    branch: str
    adapter: str
    start_token: str | None = None
    seat_id: str | None = None
    agent_id: str | None = None
    slot: str = ""
    base: str = ""
    seat_path: str = ""
    log_dir: str = ""
    started_wall: float = 0.0
    #: What this child held when the record was written (GRPH-830). Persisted so a salvage
    #: can say WHICH item the recovered branch belongs to. A record from an older supervisor
    #: has none, and an empty list means "not recorded" — the salvage still publishes the
    #: branch, it just cannot write the receipt on an item.
    held_items: list[str] = field(default_factory=list)
    #: Per-child wall-clock cap override (GRPH-849). None means use the process default.
    #: Persisted so a supervisor restart does not drop a 4h override back to 3600s.
    wall_clock_cap: float | None = None


@dataclass
class Verdict:
    """What to do with one record."""

    snapshot: Snapshot
    #: attached | gone | unadoptable
    fate: str
    why: str = ""


def children_path(repo: Path | str, state: Path | str | None = None) -> Path:
    root = Path(state) if state else state_root()
    return root / f"{repo_key(repo_root(repo))}.children.json"


def save(path: Path, snapshots: list[Snapshot]) -> None:
    """Atomic replace. A crash mid-write leaves the previous file, not a half one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generation": GENERATION,
        "children": [_as_dict(s) for s in snapshots],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=None) + "\n", encoding="utf-8")
    restrict_to_owner(tmp)
    os.replace(tmp, path)


def load(path: Path) -> list[Snapshot] | UnadoptableFile:
    """A missing file is empty. A bad file is unadoptable, not empty (P30 D7)."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        return UnadoptableFile(f"{path}: unreadable ({exc})")
    if not isinstance(data, dict):
        return UnadoptableFile(f"{path}: not an object")
    generation = data.get("generation")
    if generation != GENERATION:
        return UnadoptableFile(
            f"{path}: unknown schema generation {generation!r} (want {GENERATION})"
        )
    rows = data.get("children")
    if not isinstance(rows, list):
        return UnadoptableFile(f"{path}: children is not a list")
    out: list[Snapshot] = []
    for i, row in enumerate(rows):
        parsed = _parse_row(row)
        if parsed is None:
            return UnadoptableFile(f"{path}: child[{i}] missing required fields")
        out.append(parsed)
    return out


def snapshot_of(child: Child, slot: str = "") -> Snapshot:
    token = process_start_token(child.pid) if child.running else None
    return Snapshot(
        pid=child.pid,
        start_token=token,
        worktree=str(child.worktree),
        branch=child.branch,
        adapter=child.adapter,
        seat_id=child.seat_id,
        agent_id=child.agent_id,
        slot=slot or _slot_of(child.branch),
        base=child.base,
        seat_path=str(child.seat_path),
        log_dir=str(child.log_dir),
        started_wall=time.time() - max(0.0, time.monotonic() - child.started_at),
        held_items=list(child.held_items or []),
        wall_clock_cap=child.wall_clock_cap,
    )


def persist(path: Path, children: list[Child]) -> None:
    """Running children only. A reaped child is gone from the file so the next
    takeover does not salvage a tree that is already gone."""
    save(path, [snapshot_of(c) for c in children if c.running])


def classify(snap: Snapshot) -> Verdict:
    """Alive + matching start token → attach. ESRCH → gone. Anything else → unadoptable."""
    if not pid_is_alive(snap.pid):
        return Verdict(snap, "gone", "pid is not running")
    now = process_start_token(snap.pid)
    if snap.start_token is None or now is None:
        return Verdict(
            snap, "unadoptable",
            "cannot tell this live pid from a reused one",
        )
    if now != snap.start_token:
        return Verdict(snap, "unadoptable", "pid was reused")
    return Verdict(snap, "attached")


@dataclass
class Salvaged:
    """One tree whose real work was recovered, and where it belongs (GRPH-830).

    `items` is what the dead child held when its record was last written, which is what makes
    a salvage attributable. Empty means the record predates that field — the branch is still
    worth publishing, there is just nobody to hand the receipt to.
    """

    branch: str
    base: str
    items: list[str]


@dataclass
class Recovered:
    """What a takeover found. Unpacks as the historical three; `salvaged` is by name.

    The fourth answer is deliberately NOT part of the tuple. Every caller in this package and
    two in tests unpack exactly three, and widening the arity is the kind of edit that reads
    fine and breaks a supervisor's crash path — the one path that only runs when something
    has already gone wrong.
    """

    children: list[Child]
    occupied: set[str]
    notes: list[str]
    salvaged: list[Salvaged]

    def __iter__(self):
        return iter((self.children, self.occupied, self.notes))


def recover(
    repo: Path,
    workspace: Path,
    state: Path | None = None,
) -> Recovered:
    """Adopt live PIDs, salvage the rest. Returns attached children, occupied
    branches, notes (unadoptable / salvage), and what was salvaged with real content in it.

    Occupied branches must not be spawned onto. A corrupt file occupies every `gb/`
    branch already in the repo rather than reading as empty.
    """
    path = children_path(repo, state)
    loaded = load(path)
    notes: list[str] = []
    occupied: set[str] = set()
    attached: list[Child] = []

    salvaged: list[Salvaged] = []

    if isinstance(loaded, UnadoptableFile):
        notes.append(str(loaded))
        occupied.update(_existing_gb_branches(repo))
        # No records, so no branch and no item: a workspace salvage commits what it finds and
        # cannot say what it belongs to. Reported, never published.
        _salvage_workspace(repo, workspace, notes)
        return Recovered(attached, occupied, notes, salvaged)

    for snap in loaded:
        occupied.add(snap.branch)
        verdict = classify(snap)
        if verdict.fate == "attached":
            try:
                attached.append(attach(snap))
            except OSError as exc:
                notes.append(f"{snap.branch}: attach failed ({exc}); treating as unadoptable")
                _salvage_snapshot(repo, snap, notes, salvaged)
        elif verdict.fate == "gone":
            _salvage_snapshot(repo, snap, notes, salvaged)
        else:
            notes.append(f"{snap.branch}: unadoptable ({verdict.why})")
            _salvage_snapshot(repo, snap, notes, salvaged)

    persist(path, attached)
    return Recovered(attached, occupied, notes, salvaged)


def attach(snap: Snapshot) -> Child:
    """Rebuild a Child around a still-running pid. The original Popen is gone."""
    from .spawn import AttachedProcess

    process = AttachedProcess(snap.pid, snap.start_token)
    started_at = time.monotonic() - max(0.0, time.time() - (snap.started_wall or time.time()))
    log_dir = Path(snap.log_dir) if snap.log_dir else Path(snap.worktree)
    logs = [p for p in (log_dir / "stdout.log", log_dir / "stderr.log") if p.exists()]
    child = Child(
        adapter=snap.adapter,
        worktree=Path(snap.worktree),
        branch=snap.branch,
        base=snap.base,
        seat_path=Path(snap.seat_path) if snap.seat_path else Path(snap.worktree) / ".gbfleet-seat",
        process=process,  # type: ignore[arg-type]
        started_at=started_at,
        log_dir=log_dir,
        tree=None,
        output=Output.watching(logs, started_at=started_at) if logs else None,
        agent_id=snap.agent_id,
        seat_id=snap.seat_id,
        attached=True,
        wall_clock_cap=snap.wall_clock_cap,
    )
    emit("adopted", pid=snap.pid, branch=snap.branch, agent_id=snap.agent_id or "")
    return child


def _as_dict(s: Snapshot) -> dict:
    return {
        "pid": s.pid,
        "start_token": s.start_token,
        "worktree": s.worktree,
        "branch": s.branch,
        "adapter": s.adapter,
        "seat_id": s.seat_id,
        "agent_id": s.agent_id,
        "slot": s.slot,
        "base": s.base,
        "seat_path": s.seat_path,
        "log_dir": s.log_dir,
        "started_wall": s.started_wall,
        "held_items": list(s.held_items or []),
        "wall_clock_cap": s.wall_clock_cap,
    }


def _parse_row(row: object) -> Snapshot | None:
    if not isinstance(row, dict):
        return None
    if any(not row.get(k) and row.get(k) != 0 for k in ("worktree", "branch", "adapter")):
        return None
    try:
        pid = int(row["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    return Snapshot(
        pid=pid,
        start_token=str(row["start_token"]) if row.get("start_token") else None,
        worktree=str(row["worktree"]),
        branch=str(row["branch"]),
        adapter=str(row["adapter"]),
        seat_id=str(row["seat_id"]) if row.get("seat_id") else None,
        agent_id=str(row["agent_id"]) if row.get("agent_id") else None,
        slot=str(row.get("slot") or ""),
        base=str(row.get("base") or ""),
        seat_path=str(row.get("seat_path") or ""),
        log_dir=str(row.get("log_dir") or ""),
        started_wall=float(row["started_wall"]) if row.get("started_wall") else 0.0,
        # Tolerant, because a file written by an older supervisor has no such key and a
        # takeover that refused to read it would strand the very trees this exists to save.
        held_items=[str(i) for i in (row.get("held_items") or []) if i],
        wall_clock_cap=float(row["wall_clock_cap"]) if row.get("wall_clock_cap") is not None else None,
    )


def _slot_of(branch: str) -> str:
    if "-" in branch:
        return branch.rsplit("-", 1)[-1]
    return ""


def _existing_gb_branches(repo: Path) -> set[str]:
    """Every `gb/` branch, including those checked out in a worktree.

    `git branch --list` prefixes a worktree checkout with `+ `, so a leftover child's
    own branch occupied `+ gb/wave-1` and `_start` would still spawn onto `gb/wave-1`.
    `for-each-ref` is the same listing `orphans()` already uses.
    """
    from .worktree import BRANCH_PREFIX
    try:
        out = __import__("subprocess").run(
            ["git", "for-each-ref", "--format=%(refname:short)",
             f"refs/heads/{BRANCH_PREFIX}"],
            cwd=repo, capture_output=True, text=True, check=False,
        ).stdout
    except OSError:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _salvage_snapshot(repo: Path, snap: Snapshot, notes: list[str],
                      salvaged: list[Salvaged] | None = None) -> None:
    """Commit what a dead child left, and REMEMBER that it was real work (GRPH-830).

    The commit is local. That was the whole of the previous behaviour and it recovered and
    lost the same work in one step: one measured takeover salvaged 614 insertions across
    exactly one item's touchpoints, said so in a note, and left the commit on a local branch
    nothing pointed at. The item was re-delegated minutes later, branched from `main`, and
    rebuilt every line.

    Only `SALVAGED` is collected. `ONLY_CREDENTIAL` means the sole uncommitted file was the
    seat — genuinely nothing to publish, and pushing an empty branch per dead child would
    make every crash look like work.
    """
    tree_path = Path(snap.worktree)
    if not tree_path.exists():
        return
    try:
        reaped = reap_tree(Worktree(
            path=tree_path, branch=snap.branch, repo=Path(repo), base=snap.base,
        ))
        notes.append(f"{snap.branch}: salvaged ({reaped.disposition.value})")
        if salvaged is not None and reaped.disposition is Disposition.SALVAGED:
            salvaged.append(Salvaged(branch=snap.branch, base=snap.base,
                                     items=list(snap.held_items or [])))
    except Exception as exc:  # noqa: BLE001 — salvage is best-effort on a crash path
        notes.append(f"{snap.branch}: salvage failed ({exc})")


def _salvage_workspace(repo: Path, workspace: Path, notes: list[str]) -> None:
    workspace = Path(workspace)
    if not workspace.is_dir():
        return
    for path in workspace.iterdir():
        if path.is_dir() and path.name != "logs":
            try:
                reaped = reap_tree(Worktree(
                    path=path, branch="", repo=Path(repo), base="",
                ))
                notes.append(f"{path.name}: salvaged unadoptable tree ({reaped.disposition.value})")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{path.name}: salvage failed ({exc})")


# --- the sweep `gc` runs, and every supervisor runs at startup -----------------------


@dataclass
class Sweep:
    """What one pass over the state directory did.

    `lines` is the report — one per thing worth reading back, in the order it happened.
    The counts sit beside it rather than being parsed out of the prose because a caller
    that emits the lines still has to answer "did this pass SEE anything". Zero of
    everything is also what an empty state directory produces, and `repos` is what keeps
    a clean machine distinguishable from a pass that never looked.
    """

    lines: list[str] = field(default_factory=list)
    #: Roster files the state directory named. NOT repositories: a clone whose roster is
    #: already gone is a clone this pass cannot see at all, which is the hole it exists to
    #: stop growing, so the count says which of the two a zero means.
    repos: int = 0
    #: Records this pass READ. A roster whose lock is held is not read at all — reading it
    #: would mean holding a copy this pass is not allowed to act on — so it contributes
    #: nothing here and `locked` is what says it was walked past.
    records: int = 0
    removed: int = 0
    #: `reap` refused to remove it. Distinct from `removed` and from `absent`: the tree is
    #: still on disk, still in the record, and needs a person.
    left_dirty: int = 0
    #: A recorded child that is still running. Children outlive their supervisor, so this
    #: is a normal answer and not a failure of the sweep.
    live: int = 0
    #: The record named a directory that is no longer there. Counted separately from
    #: `removed` because nothing was removed — reporting it as a reap would hide that the
    #: roster had gone stale on its own.
    absent: int = 0
    #: Repositories left alone because a supervisor holds them, or because the question
    #: could not be asked.
    locked: int = 0
    #: Rosters, records or trees this pass could not make sense of. Left exactly as found.
    unreadable: int = 0
    #: Log directories removed with their worktree.
    logs: int = 0

    @property
    def needs_a_human(self) -> bool:
        """Something is still on disk that this pass could not take away."""
        return bool(self.left_dirty or self.unreadable)


def sweep(state: Path | str | None = None) -> Sweep:
    """Reap the recorded worktrees of every repository whose supervisor is gone.

    Walks the STATE DIRECTORY rather than one `--repo`, because the trees that need this
    belong to the clone nobody starts a supervisor on again. `worktree.reap` runs inside
    the supervisor that owns the repository, and `recover` runs it again only when a later
    supervisor takes the SAME lock — so a clone that is never re-opened keeps every tree it
    ever cut. Measured on one such clone: 1.4 GB, six worktrees, no supervisor running.

    Three rules bound it, and each is a refusal rather than a judgement call at runtime:

    * A repository whose lock is HELD is not touched at all — not "its live children are
      spared". A supervisor mid-wave owns that roster and writes it constantly, and a lock
      this pass could not read is treated as held. A dead pid in the file is NOT held: the
      kernel released the flock when that process exited, however it exited.
    * The lock is HELD for the pass, not consulted. `lock.sweep_hold` keeps the flock from
      the moment the repository answers "free" until that roster has been rewritten, so a
      supervisor cannot start into the middle of a pass: it would adopt the trees being
      removed and then have its own roster replaced by the copy this pass read before it
      arrived. Asking and acting are one step or they are a race.
    * Removal goes through the existing `reap`, without `--force`. A tree `reap` did not
      remove STAYS IN THE RECORD so the next pass still sees it — dropping it is exactly
      how `persist` loses a `LEFT_DIRTY` path today.
    * The clone itself is never deleted, and neither is any recorded path git does not
      list as one of that repository's worktrees — `reap` salvages INSIDE the path before
      it asks git to remove anything, so a stale record pointing somewhere else is a
      commit in somebody else's checkout rather than a no-op. That checkout is another
      agent's supervisor, and one lock per repository is what lets two agents work at once.

    Never raises. `up`, `until` and `mcp` run this at startup, and a tidy-up that could
    not run is a line in the report, not a failed wave.
    """
    out = Sweep()
    try:
        _walk(Path(state) if state else state_root(), out)
    except Exception as exc:  # noqa: BLE001 — reported, never fatal to the caller
        out.unreadable += 1
        out.lines.append(f"the sweep stopped early: {exc}")
    return out


def _walk(root: Path, out: Sweep) -> None:
    if not root.is_dir():
        out.lines.append(f"{root}: no state directory, so nothing names a worktree")
        return
    for roster in sorted(root.glob("*.children.json")):
        out.repos += 1
        _sweep_roster(roster, out)


def _lock_beside(roster: Path) -> Path:
    """The lock guarding `roster`, taken from its NAME rather than from anything inside it.

    `children_path` and `lock_path` key the same `repo_key`, so `<key>.children.json` and
    `<key>.lock` are siblings by construction. Deriving the repository from the records
    instead would leave the rewrite unprotected exactly when the records are worst: a
    roster whose every tree is already gone names no repository at all, and that is the
    ordinary state of the clone nobody supervises any more — the clone this exists for.
    """
    return roster.with_name(f"{roster.name.removesuffix('.children.json')}.lock")


def _sweep_roster(roster: Path, out: Sweep) -> None:
    """One roster, from the lock check through the rewrite, under the lock guarding it.

    The lock is taken BEFORE the read and not merely before the reaping: what this pass
    writes at the end is the copy it read at the start, so a read taken outside the lock
    is a stale write waiting to happen.
    """
    with sweep_hold(_lock_beside(roster)) as answer:
        if not answer.free:
            # Nothing of this repository is read, reaped or rewritten. A lock this pass
            # could not even open counts as held, and `describe` says which of the two.
            out.locked += 1
            out.lines.append(
                f"{roster.name}: {answer.describe()} — left unread, so left alone")
            return
        loaded = load(roster)
        if isinstance(loaded, UnadoptableFile):
            # A roster we cannot read is a FULL roster we cannot read (P30 D7). Rewriting
            # it would turn "we do not know what is out there" into "nothing is".
            out.unreadable += 1
            out.lines.append(f"{roster.name}: {loaded} — left as it is")
            return
        out.records += len(loaded)
        _sweep_records(roster, loaded, out, own=answer)


def _sweep_records(
    roster: Path, loaded: list[Snapshot], out: Sweep, own: LockState
) -> None:
    # The directory this roster lives in, named explicitly rather than left to
    # `state_root()`: the pass walks the directory it was given, and every lock it takes
    # has to be a file in that same one.
    state = roster.parent
    by_repo: dict[Path, list[Snapshot]] = {}
    kept: list[Snapshot] = []
    removed: list[Snapshot] = []

    for snap in loaded:
        tree = Path(snap.worktree)
        if not tree.exists():
            out.absent += 1
            out.lines.append(
                f"{snap.branch}: {tree} is already gone; dropped from {roster.name}")
            continue
        try:
            repo = repo_root(tree)
        except NotARepository as exc:
            # Not a worktree of anything we can find, so there is no repository to ask
            # about a lock and no `git worktree remove` that could run. Left, and named.
            out.unreadable += 1
            kept.append(snap)
            out.lines.append(
                f"{snap.branch}: {tree} does not resolve to a repository ({exc}); "
                "left in the record")
            continue
        by_repo.setdefault(repo, []).append(snap)

    with ExitStack() as stack:
        # The roster's own lock is held by `_sweep_roster` already, and flock belongs to an
        # open file description rather than to a process — so asking for that file again
        # would refuse THIS pass and report the repository as held by itself. Any other
        # repository a record reaches is asked once and held to the end as well: a record
        # is a path read out of a JSON file, and one that resolves into a different
        # checkout is a different supervisor's business.
        answers: dict[Path, LockState] = {_lock_beside(roster): own}
        for repo in sorted(by_repo, key=str):
            guard = lock_path(repo, state)
            if guard not in answers:
                answers[guard] = stack.enter_context(sweep_hold(guard))

        for repo in sorted(by_repo, key=str):
            snaps = by_repo[repo]
            answer = answers[lock_path(repo, state)]
            if not answer.free:
                out.locked += 1
                kept.extend(snaps)
                out.lines.append(
                    f"{repo}: {answer.describe()} — {len(snaps)} recorded tree(s) untouched")
                continue
            # Asked of git rather than taken from the record. `reap` salvages INSIDE the
            # path before it asks git to remove it, so a recorded path that is not a
            # worktree of this repository is not a no-op — see `registered_worktrees`.
            try:
                ours = registered_worktrees(repo)
            except Exception as exc:  # noqa: BLE001 — a repo we cannot list is a repo we skip
                out.unreadable += 1
                kept.extend(snaps)
                out.lines.append(
                    f"{repo}: git would not list its worktrees ({exc}); "
                    f"{len(snaps)} record(s) left")
                continue
            main = repo.resolve()
            for snap in snaps:
                path = _resolved(snap.worktree)
                if path == main or path not in ours:
                    out.unreadable += 1
                    kept.append(snap)
                    what = ("that is the clone itself" if path == main
                            else f"git does not list it as a worktree of {repo}")
                    out.lines.append(
                        f"{snap.branch}: {snap.worktree} — {what}; left in the record")
                    continue
                (removed if _reap_one(repo, snap, out) else kept).append(snap)

        # After the whole roster is decided, because a log directory is only safe to remove
        # once every record that might name it has been kept or dropped.
        protected = {_resolved(s.log_dir) for s in kept if s.log_dir}
        for snap in removed:
            _drop_logs(snap, protected, out)

        # Only when a record actually went, and only HERE — inside the lock this roster was
        # read under, which is what makes the write safe rather than merely atomic. Both
        # halves are load-bearing: rewriting an unchanged roster is not the no-op it looks
        # like, because the file belongs to a supervisor that may be writing it several
        # times a second; and writing a copy read outside the lock drops whatever that
        # supervisor recorded in between, leaving live children named nowhere.
        if len(kept) != len(loaded):
            save(roster, kept)


def _reap_one(repo: Path, snap: Snapshot, out: Sweep) -> bool:
    """One recorded worktree. True when it went away and the record may drop it."""
    if pid_is_alive(snap.pid):
        # Children outlive their supervisor (`JOB_LIMIT_FLAGS = 0`), so an unheld lock is
        # NOT evidence that nothing is running here. A live pid keeps its tree whichever
        # way the start-token check comes out: a reused pid is still a live process, and
        # this is a tidy-up pass, not a place to decide about somebody else's process.
        out.live += 1
        out.lines.append(f"{snap.branch}: pid {snap.pid} is still running; left")
        return False
    try:
        reaped = reap_tree(Worktree(
            path=Path(snap.worktree), branch=snap.branch, repo=repo, base=snap.base))
    except Exception as exc:  # noqa: BLE001 — one tree must not end the pass
        out.unreadable += 1
        out.lines.append(f"{snap.branch}: reap raised ({exc}); left in the record")
        return False
    if not reaped.removed:
        # `LEFT_DIRTY`, or a worktree somebody locked. It stays in the record: the next
        # pass has to see it, and a directory named nowhere is a directory nobody reaps.
        out.left_dirty += 1
        out.lines.append(f"{snap.branch}: LEFT at {snap.worktree} — {reaped.reason}")
        return False
    out.removed += 1
    out.lines.append(
        f"{snap.branch}: removed ({reaped.disposition.value}) {snap.worktree}")
    return True


def _resolved(path: str) -> Path:
    try:
        return Path(path).resolve()
    except OSError:  # pragma: no cover - resolve() only fails on a loop
        return Path(path)


def _drop_logs(snap: Snapshot, protected: set[Path], out: Sweep) -> None:
    """A removed slot's log directory goes with it — unless something still writes there.

    `protected` is every log directory a KEPT record names, and a directory that CONTAINS
    one is protected too: `logs/` is the parent of every slot's, and deleting the parent
    of a live child's log is deleting a live child's log.
    """
    if not snap.log_dir:
        return
    path = _resolved(snap.log_dir)
    if not path.is_dir():
        return
    if any(path == keep or keep.is_relative_to(path) for keep in protected):
        out.lines.append(f"{snap.branch}: log directory {path} kept — a live child writes there")
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        out.lines.append(f"{snap.branch}: log directory {path} survived removal ({exc})")
        return
    out.logs += 1
    out.lines.append(f"{snap.branch}: log directory {path} removed with it")
