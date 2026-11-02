"""Tearing down a finished drain: service, seats, worktrees, workspace, clone (GRPH-1296).

Before this existed the cleanup was done by hand, and the hand version has one step that
matters more than the rest: before the clone is deleted, look for anything nobody pushed.
On the GRPH-P63 teardown that included a reviewer's worktree at `/private/tmp/rv-1287` —
registered in the clone, outside `<clone>-gbfleet/`, and invisible to anything that only
looked in the workspace. So the clone's own `git worktree list` is the authority on what
belongs to it, not the directory layout.

**Refuse, never force.** Every check below is a refusal with the cause named, and there is
no `--force`: the person pushes or discards and runs it again. A teardown is the one act in
this program whose whole job is deleting a checkout, and the work in a checkout is
unrecoverable once it is gone. `gc` never deletes a checkout by design; this does, and only
once every check has passed.

**Unknown is a refusal.** A ledger that cannot be read, a process table that cannot be
asked, a PR state `gh` could not report — each refuses rather than reading as clean. That is
the failure this repository names in its own guide: absence reading as clean.

`--dry-run` computes the same plan, runs the same checks and prints the same refusals, and
changes nothing on disk or in the service manager.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import lock as lock_mod
from . import service as service_mod
from .client import Graphban, NotPermitted, ServerUnreachable, ToolFailed
from .worktree import Worktree, is_seat_relative, reap, remote_for

#: Drain state a finished drain leaves behind in the clone, and nothing else. `.gbfleet/servers`
#: is the MCP sharing file an operator writes for the drain; `uv.lock` drifts when the drain's
#: own environment is synced against a newer index. Neither is work. Any other modified or
#: untracked file is, and blocks the teardown.
ALLOWED_DRIFT_DIRS = (".gbfleet/servers",)
ALLOWED_DRIFT_NAMES = ("uv.lock",)

#: Item statuses that mean the PRD is not finished. `backlog` is in it because backlog is
#: claimable by design (GRPH-397) — ready, as far as a fleet is concerned. `in_progress` is
#: the live lease, `review` the unsigned work. `blocked` and `done` are not: a blocked item
#: waits on a person, not on this drain, and is printed as a note.
UNFINISHED = ("next", "backlog", "in_progress", "review")

#: The tools this command may call on the server. Reads, plus the one revocation it exists to
#: do. Nothing that writes an item.
TEARDOWN_TOOLS: frozenset[str] = frozenset({"search_items", "fleet_status", "retire_wave"})


@dataclass(frozen=True)
class Drain:
    """What the installed unit says the drain is. Everything teardown acts on comes from here."""

    name: str
    kind: str
    clone: Path
    workspace: Path
    server: str
    project: str
    prd: str
    wave: str
    env_path: Path


@dataclass
class Plan:
    drain: Drain
    worktrees: list[tuple[Path, str]] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    api_key: str = ""
    #: The client the checks read through, reused to retire the seats. None when there is no
    #: server or key — which is itself a refusal, so `execute` never sees a None here.
    ledger: object = None

    @property
    def ok(self) -> bool:
        return not self.refusals

    def steps(self, keep_clone: bool = False) -> list[str]:
        d = self.drain
        out = [f"uninstall service {d.name!r} (unit and key file {d.env_path})",
               f"retire the seats of wave {d.wave!r} on {d.server or '(no server)'}"]
        out += [f"reap worktree {path}" for path, _ in self.worktrees]
        out.append(f"delete {d.workspace}")
        out.append(f"keep {d.clone} (--keep-clone)" if keep_clone else f"delete {d.clone}")
        return out


class Refused(RuntimeError):
    """The drain cannot even be identified. Not one of the checks — nothing to check yet."""


# --- identifying the drain -----------------------------------------------------------


def find_drain(name: str, *, kind: str = "", prd: str = "") -> Drain:
    found = service_mod.host()
    kind = kind or found.kind
    if not kind:
        raise Refused(found.why)
    args = service_mod.until_args_of(name, kind)
    if args is None:
        raise Refused(
            f"no installed drain named {name!r} ({kind}): the unit is what says which clone "
            f"the drain used, and without it there is nothing teardown can safely delete. "
            f"`gbfleet service status` lists what is installed")
    repo = service_mod.value_of(args, "--repo")
    if not repo or not Path(repo).is_absolute():
        raise Refused(f"the unit for {name!r} carries no absolute --repo ({repo!r})")
    clone = Path(repo).resolve()
    workspace = service_mod.value_of(args, "--workspace")
    unit_prd = service_mod.value_of(args, "--prd") or ""
    if prd and unit_prd and prd != unit_prd:
        raise Refused(f"--prd {prd} is not the PRD this drain runs (--prd {unit_prd})")
    return Drain(
        name=name,
        kind=kind,
        clone=clone,
        workspace=Path(workspace).resolve() if workspace
        else clone.parent / f"{clone.name}-gbfleet",
        server=service_mod.value_of(args, "--server") or "",
        project=service_mod.value_of(args, "--project") or "",
        prd=prd or unit_prd,
        wave=service_mod.value_of(args, "--wave") or "wave",
        env_path=service_mod.env_path_for(name),
    )


def read_key(env_path: Path) -> str:
    """The drain's own key, from the owner-only file the unit reads. Falls back to the env."""
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{service_mod.API_KEY_ENV}="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    except OSError:
        pass
    return os.environ.get(service_mod.API_KEY_ENV, "")


# --- git ------------------------------------------------------------------------------


def _git(cwd: Path, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout)


def refresh_remotes(clone: Path) -> str:
    """Fetch every remote, WITHOUT prune. Returns why it failed, or "".

    A fetch can only add remote-tracking refs, so it can only make a commit read as pushed
    when it really is. Pruning is left out on purpose: a squash-merged branch the remote has
    since deleted still has its remote-tracking ref here, and that ref is the evidence its
    commits once reached the remote.
    """
    try:
        done = _git(clone, "fetch", "--all", "--quiet")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return "" if done.returncode == 0 else (done.stderr or done.stdout).strip()[:200]


def unpushed_branches(clone: Path) -> list[str]:
    """One refusal per local branch carrying a commit that no remote-tracking ref reaches."""
    out: list[str] = []
    listed = _git(clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    if listed.returncode != 0:
        return [f"could not list the branches of {clone}: {listed.stderr.strip()}"]
    has_remote = bool(remote_for(clone))
    for branch in [b.strip() for b in listed.stdout.splitlines() if b.strip()]:
        counted = _git(clone, "rev-list", "--count", f"refs/heads/{branch}", "--not",
                       "--remotes")
        try:
            n = int(counted.stdout.strip())
        except ValueError:
            out.append(f"branch {branch}: could not count its unpushed commits")
            continue
        if n:
            where = "on no remote" if has_remote else "and this clone has no remote"
            out.append(f"branch {branch}: {n} commit(s) {where}")
    return out


def _porcelain_paths(tree: Path) -> list[str] | None:
    done = _git(tree, "status", "--porcelain", "--untracked-files=all")
    if done.returncode != 0:
        return None
    paths = []
    for line in done.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip().strip('"'))
    return paths


def is_allowed_drift(path: str) -> bool:
    norm = path.replace("\\", "/").rstrip("/")
    if any(norm == d or norm.startswith(d + "/") for d in ALLOWED_DRIFT_DIRS):
        return True
    return norm.rsplit("/", 1)[-1] in ALLOWED_DRIFT_NAMES


def dirty_paths(tree: Path) -> list[str] | None:
    """Modified or untracked paths that are work: not drain state and not a seat file.

    Seat files are left out because `reap` deletes them before it removes a tree; they are a
    credential the supervisor wrote, not something anybody made.
    """
    paths = _porcelain_paths(tree)
    if paths is None:
        return None
    return [p for p in paths if not is_allowed_drift(p) and not is_seat_relative(p)]


@dataclass(frozen=True)
class Registered:
    path: Path
    branch: str
    head: str
    locked: bool
    prunable: bool


def registered(clone: Path) -> list[Registered]:
    """Every linked worktree the clone knows about, wherever it lives. The main tree is not
    in the list."""
    done = _git(clone, "worktree", "list", "--porcelain")
    if done.returncode != 0:
        raise Refused(f"git worktree list failed in {clone}: {done.stderr.strip()}")
    blocks = [b for b in done.stdout.strip().split("\n\n") if b.strip()]
    out = []
    for block in blocks[1:]:
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        out.append(Registered(
            path=Path(fields.get("worktree", "")),
            branch=fields.get("branch", "").removeprefix("refs/heads/"),
            head=fields.get("HEAD", ""),
            locked="locked" in fields,
            prunable="prunable" in fields or not Path(fields.get("worktree", "")).exists(),
        ))
    return out


def worktree_refusals(clone: Path, trees: list[Registered]) -> list[str]:
    out = []
    for tree in trees:
        if tree.prunable:
            continue
        if tree.locked:
            out.append(f"worktree {tree.path} is locked (`git worktree lock`): a person "
                       "asked for it to be left alone")
            continue
        dirty = dirty_paths(tree.path)
        if dirty is None:
            out.append(f"worktree {tree.path}: git status could not be read")
        elif dirty:
            out.append(f"worktree {tree.path} has uncommitted work: {', '.join(dirty[:5])}"
                       + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else ""))
        if not tree.branch and tree.head:
            counted = _git(clone, "rev-list", "--count", tree.head, "--not", "--remotes")
            if counted.stdout.strip() not in ("", "0"):
                out.append(f"worktree {tree.path}: detached HEAD {tree.head[:12]} carries "
                           f"{counted.stdout.strip()} commit(s) on no remote")
    return out


def clone_refusals(clone: Path) -> list[str]:
    dirty = dirty_paths(clone)
    if dirty is None:
        return [f"{clone}: git status could not be read"]
    if not dirty:
        return []
    more = f" and {len(dirty) - 5} more" if len(dirty) > 5 else ""
    return [f"{clone} has modified or untracked files: {', '.join(dirty[:5])}{more}"]


# --- the ledger -------------------------------------------------------------------------


def ledger_refusals(ledger, prd: str) -> tuple[list[str], list[str]]:
    """(refusals, notes) for the PRD's items. Unreadable is a refusal, not an empty list."""
    refusals: list[str] = []
    notes: list[str] = []
    if not prd:
        notes.append("no --prd on the drain or the command line: checking every item in "
                     "the project")
    for status in UNFINISHED + ("blocked",):
        try:
            payload = ledger.call("search_items", status=status, fields="full", limit=10_000)
        except (NotPermitted, ToolFailed, ServerUnreachable) as exc:
            return [f"could not read the ledger, so cannot tell the PRD is finished: {exc}"], notes
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return [f"search_items({status}) answered without a results list"], notes
        ids = [str(r.get("id")) for r in rows
               if isinstance(r, dict) and r.get("id") and (not prd or r.get("prd_id") == prd)]
        if not ids:
            continue
        shown = ", ".join(ids[:8]) + (f" and {len(ids) - 8} more" if len(ids) > 8 else "")
        if status == "blocked":
            notes.append(f"blocked, waiting on a person (does not stop teardown): {shown}")
        else:
            refusals.append(f"{prd or 'the project'} still has {status} item(s): {shown}")
    return refusals, notes


def gh_open_prs(clone: Path) -> list[dict] | str:
    """Open PRs on the clone's repository, or the reason `gh` could not say."""
    if not shutil.which("gh"):
        return "gh is not installed, so the wave's PRs cannot be checked"
    try:
        done = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", "1000",
             "--json", "headRefName,url"],
            cwd=str(clone), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"gh pr list failed: {exc}"
    if done.returncode != 0:
        return f"gh pr list failed: {(done.stderr or done.stdout).strip()[:200]}"
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return "gh pr list answered with something that is not JSON"
    return rows if isinstance(rows, list) else "gh pr list answered with no list"


def pr_refusals(clone: Path, lister: Callable[[Path], list[dict] | str]) -> list[str]:
    """Every PR still open from one of this clone's branches."""
    listed = _git(clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    branches = {b.strip() for b in listed.stdout.splitlines() if b.strip()}
    got = lister(clone)
    if isinstance(got, str):
        return [got]
    return [f"PR still open from {row.get('headRefName')}: {row.get('url', '')}"
            for row in got
            if isinstance(row, dict) and row.get("headRefName") in branches]


# --- processes and the lock ---------------------------------------------------------------


def processes_under(path: Path) -> list[str] | None:
    """`pid N (command)` for every process with its cwd or an open file under `path`.

    None means the question could not be asked, which the caller treats as a refusal.
    Linux reads `/proc`; macOS and the BSDs ask `lsof` for everything once and filter here,
    because `lsof +D` walks the whole tree and a clone with a `node_modules` takes minutes.
    Windows has neither, and is asked by renaming the directory (see `_windows_in_use`); it
    reports that the clone is held but not which process holds it.
    """
    target = str(path.resolve())
    me = os.getpid()

    def under(name: str) -> bool:
        return name == target or name.startswith(target + os.sep)

    if os.name == "nt":
        return _windows_in_use(Path(target))
    proc = Path("/proc")
    if (proc / "self" / "fd").is_dir():
        found: list[str] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            links = [entry / "cwd"]
            try:
                links += list((entry / "fd").iterdir())
            except OSError:
                pass
            for link in links:
                try:
                    if under(os.readlink(link)):
                        try:
                            cmd = (entry / "comm").read_text().strip()
                        except OSError:
                            cmd = "?"
                        found.append(f"pid {entry.name} ({cmd})")
                        break
                except OSError:
                    continue
        return found
    try:
        listed = subprocess.run(["lsof", "-n", "-P", "-w", "-F", "pcn"],
                                capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode not in (0, 1) or not listed.stdout:
        return None
    hits: dict[int, str] = {}
    pid, cmd = 0, "?"
    for line in listed.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid, cmd = int(value) if value.isdigit() else 0, "?"
        elif tag == "c":
            cmd = value
        elif tag == "n" and pid and pid != me and under(value):
            hits.setdefault(pid, cmd)
    return [f"pid {p} ({c})" for p, c in sorted(hits.items())]


def _windows_in_use(path: Path) -> list[str] | None:
    """Windows has no `/proc` and no `lsof`, so ask the filesystem instead: a directory that
    any process has as its cwd, or holds a file open under, cannot be renamed. Rename it to a
    sibling and straight back. A refusal to rename IS the answer "in use", though Windows does
    not say by whom; any other failure, or a rename that cannot be undone, is "could not
    check" (None) — never clean.
    """
    probe = path.with_name(path.name + ".gbfleet-teardown-probe")
    if probe.exists():
        return None
    try:
        os.rename(path, probe)
    except PermissionError as exc:
        return [f"a process Windows does not name (the directory cannot be renamed: {exc})"]
    except OSError:
        return None
    try:
        os.rename(probe, path)
    except OSError as exc:
        raise RuntimeError(f"in-use probe moved {path} to {probe} and could not move it back: "
                           f"{exc}; rename it back by hand") from exc
    return []


def _clear_readonly_and_retry(func, path, _exc) -> None:
    """git writes object files read-only; on Windows that makes them undeletable until the
    read-only bit is cleared. Clear it and try the same operation once more."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(target: Path) -> None:
    shutil.rmtree(target, onexc=_clear_readonly_and_retry)


def lock_refusal(clone: Path) -> str:
    try:
        lock_mod.probe(clone)
    except lock_mod.RepoLocked as exc:
        return f"the repository lock is held: {exc}"
    except Exception as exc:  # noqa: BLE001 — unknown is a refusal, with the reason
        return f"the repository lock could not be read: {exc}"
    return ""


def service_refusal(state) -> str:
    if state.installed and state.running is None:
        return f"cannot tell whether {state.name!r} is running: {state.detail or 'no answer'}"
    if state.running:
        return f"service {state.name!r} is mid-run; wait for it to go idle"
    return ""


# --- the plan -------------------------------------------------------------------------------


def make_ledger(drain: Drain, api_key: str):
    return Graphban(base_url=drain.server, api_key=api_key, allowed=TEARDOWN_TOOLS,
                    project_id=drain.project or None)


def plan(
    name: str,
    *,
    prd: str = "",
    kind: str = "",
    ledger=None,
    pr_lister: Callable[[Path], list[dict] | str] = gh_open_prs,
    process_finder: Callable[[Path], list[str] | None] = processes_under,
) -> Plan:
    """Identify the drain and run every check. Changes nothing but remote-tracking refs."""
    drain = find_drain(name, kind=kind, prd=prd)
    result = Plan(drain=drain, api_key=read_key(drain.env_path))
    clone = drain.clone

    refusal = service_refusal(service_mod.status(name, kind=drain.kind))
    if refusal:
        result.refusals.append(refusal)

    if not (clone / ".git").exists():
        result.refusals.append(f"{clone} is not a git checkout; refusing to delete it")
        return result

    # The ledger: nothing ready, unsigned or leased.
    if ledger is None and drain.server and result.api_key:
        ledger = make_ledger(drain, result.api_key)
    if ledger is None:
        result.refusals.append(
            f"no server or API key for {name!r} (neither {drain.env_path} nor "
            f"${service_mod.API_KEY_ENV}): cannot check the PRD is finished or retire seats")
    else:
        refusals, notes = ledger_refusals(ledger, drain.prd)
        result.refusals += refusals
        result.notes += notes
        result.ledger = ledger

    why = refresh_remotes(clone)
    if why:
        result.notes.append(f"fetch failed, judging against the remote refs already here: {why}")

    result.refusals += pr_refusals(clone, pr_lister)

    # Unpushed work: branches, the clone itself, and every registered worktree.
    result.refusals += unpushed_branches(clone)
    result.refusals += clone_refusals(clone)
    trees = registered(clone)
    result.refusals += worktree_refusals(clone, trees)
    result.worktrees = [(t.path, t.branch) for t in trees if not t.prunable]
    for t in trees:
        if t.prunable:
            result.notes.append(f"worktree {t.path} is registered but gone; it will be pruned")

    # In use.
    found = process_finder(clone)
    if found is None:
        result.refusals.append(f"cannot tell whether a process has {clone} open on this host")
    elif found:
        result.refusals.append(f"{clone} is in use by: {', '.join(found[:8])}")
    refusal = lock_refusal(clone)
    if refusal:
        result.refusals.append(refusal)
    return result


# --- doing it -------------------------------------------------------------------------------


def retire_seats(ledger, clone: Path, wave: str) -> tuple[int, list[str], list[str]]:
    """Revoke the wave's seats, through every planner agent the drain registered.

    `retire_wave` revokes only the seats the named agent minted, and a drain registers a new
    planner every cycle — so one call from one agent retires one cycle's seats. Each cycle's
    planner registered with `worktree=<clone>` on this same credential, which is how they
    are found again; `minter_for` allows any agent on the calling credential.
    """
    info: list[str] = []
    problems: list[str] = []
    try:
        roster = ledger.call("fleet_status", view="full")
    except (NotPermitted, ToolFailed, ServerUnreachable) as exc:
        return 0, [], [f"could not read the roster to find the wave's planners: {exc}"]
    planners = [a for a in (roster.get("agents") or [])
                if a.get("active_role") == "planner" and a.get("worktree")
                and Path(str(a["worktree"])).resolve() == clone]
    revoked = 0
    for agent in planners:
        try:
            got = ledger.call("retire_wave", agent_id=agent["id"], wave=wave)
        except (NotPermitted, ToolFailed, ServerUnreachable) as exc:
            problems.append(f"retire_wave as {agent['id']} failed: {exc}")
            continue
        revoked += int(got.get("seats_revoked") or 0)
        running = got.get("agents_still_running") or []
        if running:
            info.append(f"seats revoked under {agent['id']}, but still running: "
                         f"{', '.join(map(str, running))}")
    if not planners:
        info.append(f"no planner registered from {clone} on this credential; no seats to "
                    "retire")
    return revoked, info, problems


@dataclass
class Done:
    removed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def execute(p: Plan, *, keep_clone: bool = False) -> Done:
    """Uninstall, retire, reap, delete — in that order, stopping before any deletion of the
    clone if a worktree could not be reaped."""
    if not p.ok:
        raise Refused("execute() called on a plan with refusals")
    d, done = p.drain, Done()

    for path in service_mod.uninstall(d.name, kind=d.kind):
        done.removed.append(f"removed {path}")

    if p.ledger is not None:
        revoked, info, problems = retire_seats(p.ledger, d.clone, d.wave)
        done.removed.append(f"{revoked} seat(s) of wave {d.wave!r} revoked")
        done.removed += info
        done.problems += problems

    # A seat that could not be retired does not stop the deletion below: the service is
    # already gone, so a re-run could not find this drain again, and the seats can still be
    # retired from the server. A worktree that could not be reaped DOES stop it — deleting
    # the clone would orphan that tree's registration and whatever reap refused to touch.
    _git(d.clone, "worktree", "prune")
    unreaped = []
    for path, branch in p.worktrees:
        got = reap(Worktree(path=path, branch=branch, repo=d.clone))
        if got.removed:
            done.removed.append(f"reaped worktree {path}")
        else:
            unreaped.append(f"worktree {path} was not removed: {got.reason}")
    if unreaped:
        done.problems += unreaped
        done.problems.append(f"{d.clone} and {d.workspace} were left in place")
        return done

    for target in ([d.workspace] + ([] if keep_clone else [d.clone])):
        if target.exists():
            remove_tree(target)
            done.removed.append(f"removed {target}")
    return done


def render_plan(p: Plan, *, keep_clone: bool, dry_run: bool, out=None) -> None:
    out = out or sys.stdout
    print(f"drain {p.drain.name!r}: clone {p.drain.clone}, prd {p.drain.prd or '(none)'}, "
          f"wave {p.drain.wave!r}", file=out)
    for note in p.notes:
        print(f"  note: {note}", file=out)
    if p.refusals:
        print("refused, nothing deleted:", file=out)
        for refusal in p.refusals:
            print(f"  - {refusal}", file=out)
        return
    verb = "would" if dry_run else "will"
    for step in p.steps(keep_clone):
        print(f"  {verb} {step}", file=out)


def run(name: str, *, prd: str = "", dry_run: bool = False, keep_clone: bool = False,
        out=None, err=None, **seams) -> int:
    """`gbfleet teardown`. Exit 2 on a refusal (nothing changed), 1 when a step failed
    part-way, 0 when done. `seams` reach `plan` — the ledger, PR lister, process finder."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        planned = plan(name, prd=prd, **seams)
    except (Refused, service_mod.Refused) as exc:
        print(f"gbfleet teardown: {exc}", file=err)
        return 2
    render_plan(planned, keep_clone=keep_clone, dry_run=dry_run, out=out)
    if not planned.ok:
        return 2
    if dry_run:
        return 0
    done = execute(planned, keep_clone=keep_clone)
    for line in done.removed:
        print(line, file=out)
    for line in done.problems:
        print(f"gbfleet teardown: {line}", file=err)
    return 0 if done.ok else 1
