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
import time
from dataclasses import dataclass
from pathlib import Path

from .hostos import pid_is_alive, process_start_token, restrict_to_owner
from .observe import emit
from .progress import Output
from .spawn import Child
from .state import repo_key, repo_root, state_root
from .worktree import Worktree, reap as reap_tree

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


def recover(
    repo: Path,
    workspace: Path,
    state: Path | None = None,
) -> tuple[list[Child], set[str], list[str]]:
    """Adopt live PIDs, salvage the rest. Returns attached children, occupied
    branches, and notes (unadoptable / salvage).

    Occupied branches must not be spawned onto. A corrupt file occupies every `gb/`
    branch already in the repo rather than reading as empty.
    """
    path = children_path(repo, state)
    loaded = load(path)
    notes: list[str] = []
    occupied: set[str] = set()
    attached: list[Child] = []

    if isinstance(loaded, UnadoptableFile):
        notes.append(str(loaded))
        occupied.update(_existing_gb_branches(repo))
        _salvage_workspace(repo, workspace, notes)
        return attached, occupied, notes

    for snap in loaded:
        occupied.add(snap.branch)
        verdict = classify(snap)
        if verdict.fate == "attached":
            try:
                attached.append(attach(snap))
            except OSError as exc:
                notes.append(f"{snap.branch}: attach failed ({exc}); treating as unadoptable")
                _salvage_snapshot(repo, snap, notes)
        elif verdict.fate == "gone":
            _salvage_snapshot(repo, snap, notes)
        else:
            notes.append(f"{snap.branch}: unadoptable ({verdict.why})")
            _salvage_snapshot(repo, snap, notes)

    persist(path, attached)
    return attached, occupied, notes


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
    )


def _slot_of(branch: str) -> str:
    if "-" in branch:
        return branch.rsplit("-", 1)[-1]
    return ""


def _existing_gb_branches(repo: Path) -> set[str]:
    from .worktree import BRANCH_PREFIX
    try:
        out = __import__("subprocess").run(
            ["git", "branch", "--list", f"{BRANCH_PREFIX}*"],
            cwd=repo, capture_output=True, text=True, check=False,
        ).stdout
    except OSError:
        return set()
    return {line.strip().lstrip("* ").strip() for line in out.splitlines() if line.strip()}


def _salvage_snapshot(repo: Path, snap: Snapshot, notes: list[str]) -> None:
    tree_path = Path(snap.worktree)
    if not tree_path.exists():
        return
    try:
        reaped = reap_tree(Worktree(
            path=tree_path, branch=snap.branch, repo=Path(repo), base=snap.base,
        ))
        notes.append(f"{snap.branch}: salvaged ({reaped.disposition.value})")
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
