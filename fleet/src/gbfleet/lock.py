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
            f"against a different checkout. Lock: {path}"
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
