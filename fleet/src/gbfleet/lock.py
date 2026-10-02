"""One supervisor per repository, and a way to tell how the last one ended.

PRD-22 D-h. Two supervisors on one repository would exceed `--max-workers` between
them, duplicate worktrees, and double-spawn. The lock is what makes the cap *correct*
rather than approximate, because the cap's natural scope is that repository's worktree
pool.

**The kernel is the liveness check.** The PRD asks that the lock check pid liveness
rather than mere presence, so that a reboot cannot leave a lock nobody holds and a
supervisor that refuses to start forever. An advisory `flock` gives something stronger
than a liveness heuristic: the lock lives on an open file descriptor, so it is released
when the process exits *however* it exits, and a reboot obviously drops it. There is no
stale-lock state to reason about and no PID-reuse window to get wrong. The pid in the
file is for the error message, not for the decision.

**Releasing cleanly empties the file, and that is load-bearing.** A lock file left with
a holder record in it means the previous supervisor died without releasing — which is
exactly when a new supervisor must adopt its orphaned children rather than start blind
beside them. Acquiring after a crash therefore must not look identical to acquiring
fresh, so `acquire` reports which happened.

Caveat worth stating: file locking on a networked filesystem is unreliable. A repository
on NFS or SMB is outside what this can promise, and the supervisor runs on the
developer's own machine by design (PRD-22 §7 rules out remote spawn).

The lock itself lives in `hostos`, which explains why Windows takes it on a byte at a
non-zero offset rather than on the whole file: `msvcrt.locking` is mandatory, so a lock
over the holder record would stop the refused process reading the very record it needs
in order to say who is holding it.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import __version__
from .hostos import AlreadyLocked, lock_exclusive, read_at, restrict_to_owner, write_at
from .state import lock_path, repo_root

_FILE_MODE = 0o600


@dataclass(frozen=True)
class Holder:
    """Who is (or was) running a supervisor here."""

    pid: int
    repo: str
    acquired_at: str
    version: str

    def as_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "repo": self.repo,
                "acquired_at": self.acquired_at,
                "version": self.version,
            }
        )

    @staticmethod
    def parse(raw: str) -> "Holder | None":
        """None means the record is unreadable, which is not the same as absent."""
        try:
            data = json.loads(raw)
            return Holder(
                pid=int(data["pid"]),
                repo=str(data["repo"]),
                acquired_at=str(data["acquired_at"]),
                version=str(data["version"]),
            )
        except (ValueError, TypeError, KeyError):
            return None


@dataclass(frozen=True)
class Takeover:
    """A previous supervisor held this lock and never released it: it crashed.

    `holder` is None when the record it left was unreadable — a partial write during
    the crash is precisely when that happens. That is NOT the same as no takeover, and
    the two must not collapse into one another: whether a fleet may be running
    unsupervised is the question adoption turns on, and "we cannot tell who" still
    answers it yes.
    """

    holder: Holder | None
    raw: str

    def describe(self) -> str:
        if self.holder is not None:
            return (
                f"took over from pid {self.holder.pid}, which held this lock since "
                f"{self.holder.acquired_at} and did not release it"
            )
        return (
            "took over from a supervisor that left an unreadable record "
            f"({self.raw[:120]!r}) — its children may still be running"
        )


@dataclass(frozen=True)
class Acquired:
    path: Path
    holder: Holder
    takeover: Takeover | None


class RepoLocked(RuntimeError):
    """Another supervisor holds this repository."""

    def __init__(self, path: Path, holder: Holder | None) -> None:
        self.path = path
        self.holder = holder
        if holder is not None:
            who = f"pid {holder.pid} (since {holder.acquired_at}, gbfleet {holder.version})"
        else:
            # Held, but the holder had not finished writing its record. Saying "nobody"
            # here would invite the reader to delete a live lock.
            who = "a process that has not yet written its record"
        super().__init__(
            f"another gbfleet supervisor already has {holder.repo if holder else 'this repo'}: "
            f"{who}. One supervisor per repository (PRD-22 D-h) — stop that one, or run "
            f"against a different checkout. Lock: {path}\n"
            # GRPH-811. The refusal was correct and read as a limitation, because it named
            # neither of the two things a person hitting it is actually choosing between.
            # `mcp` and `until` are the two SUPERVISION MODES (PRD-39 D-e), not a tool and a
            # convenience: both spawn children into this one repository, which is the thing
            # the lock exists to keep to one. Whoever hits this wants both, and the answer is
            # that `mcp` already contains the other — a planner holding it drives the same
            # wave under its own judgement instead of a loop's.
            "\n`gbfleet mcp` and `gbfleet until` are the two supervision modes, and both "
            "spawn into this repository — that is what is being kept to one, rather than the "
            "command. A planner holding `mcp` runs the same wave with `spawn`/`ps`/`stop` "
            "under its own judgement; `until` is that loop with no model in it. Pick the "
            "mode, or give the second one its own checkout."
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def hold(repo: Path | str, state: Path | str | None = None) -> Iterator[Acquired]:
    """Hold this repository for the duration of the block.

    Raises `RepoLocked` if another supervisor has it. Never blocks and never waits:
    a supervisor that queues behind another one is a supervisor nobody asked for.
    """
    root = repo_root(repo)
    path = lock_path(root, state)

    # O_CREAT without O_TRUNC: the previous holder's record is the crash signal, and
    # opening in "w" or "a" modes would destroy it or make it unwritable respectively.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, _FILE_MODE)
    acquired = False
    try:
        # `restrict_to_owner`, not `chmod`: O_CREAT's mode is masked by the umask on
        # POSIX and means almost nothing on Windows, and this file names the process
        # holding the repository (GRPH-584).
        restrict_to_owner(path)
        try:
            lock_exclusive(fd)
        except AlreadyLocked:
            # Reading the holder record while another process holds the lock is only
            # possible because the lock is taken on a byte PAST the record — see
            # `hostos._LOCK_BYTE`. On Windows the lock is mandatory, and a lock on byte
            # 0 would make this read fail and this error message useless.
            raw = read_at(fd, 4096, 0).decode("utf-8", "replace").strip()
            raise RepoLocked(path, Holder.parse(raw) if raw else None) from None

        acquired = True
        previous = read_at(fd, 4096, 0).decode("utf-8", "replace").strip()
        takeover = Takeover(Holder.parse(previous), previous) if previous else None

        holder = Holder(
            pid=os.getpid(), repo=str(root), acquired_at=_now(), version=__version__
        )
        os.ftruncate(fd, 0)
        write_at(fd, holder.as_json().encode("utf-8"), 0)
        os.fsync(fd)

        yield Acquired(path=path, holder=holder, takeover=takeover)
    finally:
        # Empty on the way out, so the next supervisor can tell a clean shutdown from a
        # crash. Best-effort: if this fails we are already unwinding, and a spurious
        # takeover report is a far better failure than a lost one.
        #
        # ONLY if we actually acquired. Truncating unconditionally means a refused
        # attempt erases the record of the supervisor that refused it — after which
        # nothing can name the holder, and when that holder eventually crashes its
        # successor finds an empty file and starts blind. Every visible symptom of that
        # bug is a lock that works.
        if acquired:
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
        # Safe on the refused path too: flock is held per open file description, so
        # closing ours does not release theirs.
        os.close(fd)


def probe(repo: Path | str, state: Path | str | None = None) -> None:
    """Raise `RepoLocked` if a live supervisor holds this repository.

    Never writes a holder record and never truncates. `hold` empties the file on a
    clean release so the next supervisor can tell a crash from a shutdown; a
    diagnostic that used `hold` would clear a crash record, and the next `up` would
    start blind beside live children (GRPH-599). Closing the fd is what releases
    the flock we take to ask — the file contents are left as they were.
    """
    root = repo_root(repo)
    path = lock_path(root, state)
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return
    try:
        try:
            lock_exclusive(fd)
        except AlreadyLocked:
            raw = read_at(fd, 4096, 0).decode("utf-8", "replace").strip()
            raise RepoLocked(path, Holder.parse(raw) if raw else None) from None
    finally:
        os.close(fd)


@dataclass(frozen=True)
class LockState:
    """May this repository's abandoned trees be touched? THREE answers, not two.

    `free` and `locked` are what the kernel says. `unknown` is the question not being
    answerable — the lock file would not open — and it is treated as `locked`, because
    collapsing it into `free` is the version of this that deletes a live supervisor's
    worktrees. It is NOT collapsed into `locked` either: a caller that reports why it
    skipped a repository has to be able to say "somebody is running here" and "I could
    not ask" apart.

    An unreadable holder record is `locked`, not `unknown`. The flock does not care what
    is written in the file, so a record we cannot parse is still a lock somebody holds —
    which is the same reason `RepoLocked` refuses to say "nobody" about it.
    """

    state: str  # free | locked | unknown
    holder: "Holder | None" = None
    why: str = ""

    @property
    def free(self) -> bool:
        return self.state == "free"

    def describe(self) -> str:
        if self.state == "free":
            return "no supervisor holds this repository"
        if self.holder is not None:
            return f"held by pid {self.holder.pid} since {self.holder.acquired_at}"
        if self.state == "locked":
            return "held by a supervisor that has not finished writing its record"
        return f"the lock could not be read ({self.why})"


@contextmanager
def sweep_hold(lock_file: Path | str) -> Iterator[LockState]:
    """Hold one repository's lock for the length of a tidy-up pass, without claiming it.

    Yields the same three answers `LockState` carries and, when the answer is `free`,
    KEEPS the flock until the block ends. Holding is the whole difference from `probe`,
    and it is not a refinement. `probe` answers and closes its descriptor, so between its
    answer and the caller's `git worktree remove` a supervisor can start on that
    repository: it adopts the very trees the caller is about to delete, and the caller's
    rewrite of the roster then drops the children that supervisor recorded in between —
    live children named nowhere, which is the exact failure `gc` exists to stop
    (GRPH-1011, on review). Acting on a question you asked a minute ago is racing every
    supervisor that starts while you work.

    Takes the lock FILE rather than a repository, because the caller usually has the file
    first: `<key>.children.json` and `<key>.lock` are the same `repo_key`, so a roster
    names the lock guarding it even when not one of its records still resolves to a
    checkout — the ordinary case for the clone nobody supervises any more, which is the
    clone this exists for.

    **Writes no holder record and truncates nothing**, exactly like `probe`, for the
    reason `hold`'s own comment gives: the file's contents are how the next supervisor
    tells a crash from a clean shutdown, and a sweep is not a supervisor. Writing itself
    in would leave a record reading "a supervisor held this and died" whenever the sweep
    is killed mid-pass, and the next `up` would report a takeover that never happened.

    So a supervisor refused while a sweep runs reads the file as the sweep left it — empty,
    which `RepoLocked` reports as "a process that has not yet written its record". True of
    a sweeper, and over in the seconds one repository's reaps take. There is deliberately
    no retry: `hold` never waits, because a supervisor that queues behind another one is a
    supervisor nobody asked for, and `mcp` already answers a refusal by attaching
    read-only instead of dying (GRPH-881).
    """
    path = Path(lock_file)
    try:
        # O_CREAT, because the lock has to be TAKEN: a repository nobody has supervised
        # since the last reboot has no lock file yet, and answering `free` because the
        # file is missing — what `probe` does — would leave the pass holding nothing.
        # No O_TRUNC: the previous holder's record is the crash signal.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, _FILE_MODE)
    except OSError as exc:
        yield LockState("unknown", why=str(exc)[:200])
        return
    try:
        # Same reason `hold` does it: O_CREAT's mode is masked by the umask on POSIX and
        # means almost nothing on Windows (GRPH-584).
        restrict_to_owner(path)
        try:
            lock_exclusive(fd)
        except AlreadyLocked:
            raw = read_at(fd, 4096, 0).decode("utf-8", "replace").strip()
            yield LockState("locked", holder=Holder.parse(raw) if raw else None)
            return
        yield LockState("free")
    finally:
        # Closing is the release, and it is the kernel's rather than ours: a sweep killed
        # mid-reap drops the lock however it died. Nothing to un-truncate, because
        # nothing was written. Safe on the refused path too — flock is held per open file
        # description, so closing ours does not release theirs.
        os.close(fd)

