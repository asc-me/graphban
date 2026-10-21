"""One supervisor per repository, and a way to tell how the last one ended.

PRD-22 D-h. Two supervisors on one repository would exceed `--max-workers` between
them, duplicate worktrees, and double-spawn. The lock is what makes the cap *correct*
rather than approximate, because the cap's natural scope is that repository's worktree
pool.

**The kernel is the liveness check for a process that has exited.** The PRD asks that
the lock check pid liveness rather than mere presence, so that a reboot cannot leave a
lock nobody holds and a supervisor that refuses to start forever. An advisory `flock`
gives something stronger than a liveness heuristic: the lock lives on an open file
descriptor, so it is released when the process exits *however* it exits, and a reboot
obviously drops it. There is no stale-lock state to reason about and no PID-reuse
window to get wrong for a dead holder. The pid in the file is for the error message
on that path, not for the decision.

**A live idle holder is a different question** (GRPH-1056). `flock` cannot tell a
supervisor that is running a wave from a `gbfleet mcp` a session forgot about and
left holding the repository overnight — the process is alive, so the lock is real.
The record carries `touched_at`, moved by `Acquired.touch` when the holder actually
does supervisor work. Past `IDLE_TAKEOVER_AFTER` with no touch, `hold` stops that
process and takes the lock, and says so. It will not stop a pid it cannot prove has
this file open and is gbfleet: a recycled pid, or a sweep guarding a crash record,
is left alone and the refusal names who. `probe` and `sweep_hold` never take over.
They only ask.

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
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import __version__
from .hostos import (
    AlreadyLocked,
    end_pid,
    lock_exclusive,
    pid_has_path_open,
    pid_is_alive,
    pid_is_zombie,
    process_command,
    read_at,
    restrict_to_owner,
    write_at,
)
from .state import lock_path, repo_root

_FILE_MODE = 0o600

#: How long a holder may go without supervisor work before `hold` stops it and
#: takes the repository (GRPH-1056). A forgotten `gbfleet mcp` held this lock for
#: more than 22h and forced waves onto separate clones. Four hours is long enough
#: that stepping away from the keyboard is not a takeover, and short enough that
#: one stuck supervisor cannot block the repository for a day. Age is not idle:
#: `until` and `up` touch every pass, and `mcp` touches while a child is running
#: or a tool is called, so a supervisor that is actually working never reaches this.
IDLE_TAKEOVER_AFTER = 4 * 60 * 60


@dataclass(frozen=True)
class Holder:
    """Who is (or was) running a supervisor here."""

    pid: int
    repo: str
    acquired_at: str
    version: str
    #: When this holder last did supervisor work. Empty on a record written before
    #: idle tracking, which is read as `acquired_at`: an old supervisor never
    #: touches, so the whole time it has held the lock is idle time.
    touched_at: str = ""

    def as_json(self) -> str:
        data = {
            "pid": self.pid,
            "repo": self.repo,
            "acquired_at": self.acquired_at,
            "version": self.version,
        }
        # Omitted when empty so a record planted by an older writer still
        # round-trips through `as_json` byte for byte. A tidy-up pass compares
        # the file to that string to prove it wrote nothing of its own.
        if self.touched_at:
            data["touched_at"] = self.touched_at
        return json.dumps(data)

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
                touched_at=str(data.get("touched_at") or ""),
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
    #: `crashed` — the previous process was already gone and left the record.
    #: `idle` — it was still alive, had done no supervisor work past the threshold,
    #: and this acquire stopped it. The two must not read as the same sentence: one
    #: is a crash, the other is a reclaim of a forgotten session.
    because: str = "crashed"

    def describe(self) -> str:
        if self.because == "idle" and self.holder is not None:
            idle_at = self.holder.touched_at or self.holder.acquired_at
            return (
                f"took over from pid {self.holder.pid}, which was still alive but idle "
                f"since {idle_at} (held since {self.holder.acquired_at}) and was stopped "
                f"so this repository could be supervised again"
            )
        if self.because == "idle":
            return (
                "took over from a supervisor that was still alive but idle and was stopped; "
                f"it left an unreadable record ({self.raw[:120]!r}) — its children may "
                "still be running"
            )
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
    #: The descriptor the lock is held on. `touch` rewrites the record through it.
    #: -1 when a caller built an `Acquired` without a live hold; `touch` is then a no-op.
    fd: int = -1

    def touch(self) -> None:
        """Record that this holder just did supervisor work.

        Idle takeover reads `touched_at`. A failed write must not take the wave
        down with it: the clock stays where it was, which can only make a later
        reclaim more likely, never hide a live one.
        """
        if self.fd < 0:
            return
        updated = replace(self.holder, touched_at=_now())
        payload = updated.as_json().encode("utf-8")
        # A shorter write would leave the tail of the previous record in the
        # file, and `Holder.parse` would then fail closed into "unreadable" —
        # a live holder reported as a crash. Pad out to the previous span.
        width = max(len(payload), getattr(self, "_span", 0))
        try:
            write_at(self.fd, payload.ljust(width), 0)
        except OSError:
            return
        object.__setattr__(self, "holder", updated)
        object.__setattr__(self, "_span", width)


class RepoLocked(RuntimeError):
    """Another supervisor holds this repository."""

    def __init__(self, path: Path, holder: Holder | None, *,
                 idle_after: float | None = None, note: str = "") -> None:
        self.path = path
        self.holder = holder
        if holder is not None:
            who = describe_holder(holder)
        else:
            # Held, but the holder had not finished writing its record. Saying "nobody"
            # here would invite the reader to delete a live lock.
            who = "a process that has not yet written its record"
        extra = ""
        if holder is not None and idle_after is not None:
            idle = _seconds_since(holder.touched_at or holder.acquired_at)
            if idle is not None:
                extra = (
                    f" Idle takeover waits until the holder has been idle for "
                    f"{format_age(idle_after)}; it has been idle for {format_age(idle)}."
                )
        if note:
            extra = f"{extra} {note}"
        super().__init__(
            f"another gbfleet supervisor already has {holder.repo if holder else 'this repo'}: "
            f"{who}.{extra} One supervisor per repository (PRD-22 D-h) — stop that one, or run "
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


def _parse_stamp(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _seconds_since(raw: str, now: datetime | None = None) -> float | None:
    stamp = _parse_stamp(raw)
    if stamp is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - stamp).total_seconds()


def format_age(seconds: float) -> str:
    """A duration a person can read off a refusal without doing the subtraction."""
    whole = max(0, int(seconds))
    days, rem = divmod(whole, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def describe_holder(holder: Holder, *, now: datetime | None = None) -> str:
    """Pid and age, which is what a refusal has to hand the reader (GRPH-1056).

    An unparseable `acquired_at` (a test double, a partial record) still names the
    pid and the raw stamp. Inventing an age there would be a confident wrong answer.
    """
    now = now or datetime.now(timezone.utc)
    held = _seconds_since(holder.acquired_at, now)
    parts = [f"pid {holder.pid}"]
    if held is not None:
        parts.append(f"held for {format_age(held)}")
    parts.append(f"since {holder.acquired_at}")
    if holder.touched_at and holder.touched_at != holder.acquired_at:
        idle = _seconds_since(holder.touched_at, now)
        if idle is not None:
            parts.append(f"idle for {format_age(idle)}")
    parts.append(f"gbfleet {holder.version}")
    return ", ".join(parts)


def _read_holder(fd: int) -> tuple[str, Holder | None]:
    raw = read_at(fd, 4096, 0).decode("utf-8", "replace").strip()
    return raw, (Holder.parse(raw) if raw else None)


def _try_reclaim_idle(holder: Holder, path: Path, *, idle_after: float) -> tuple[bool, str]:
    """Stop an idle live holder so the caller can take the lock.

    Returns `(stopped, note)`. `stopped` means the process is gone and the flock
    should now be free. A note is why a holder that WAS idle was left running —
    "could not tell" must not read as "chose to leave a live supervisor in place
    with nothing said". A holder that is simply not idle yet returns `("",)`
    with no note: the ordinary refusal already names it.
    """
    idle = _seconds_since(holder.touched_at or holder.acquired_at)
    if idle is None or idle < idle_after:
        return False, ""
    # The record's pid is us. Killing it would kill the process trying to take
    # the lock, which is this process, and the refusal would never be raised.
    if holder.pid <= 0 or holder.pid == os.getpid():
        return False, ""
    if not pid_is_alive(holder.pid):
        # The flock is held — that is why we are here — but not by the pid in
        # the file. A sweep does this on purpose: it holds the lock and writes
        # no record of its own, so the file still names a supervisor that
        # already exited. Stopping that pid is a no-op; stopping whoever holds
        # the flock would kill the sweep.
        return False, ""
    command = process_command(holder.pid)
    if not command or "gbfleet" not in command:
        return False, (
            "It has been idle past the takeover threshold, but its command line "
            "could not be confirmed as gbfleet, so it was left running."
        )
    opened = pid_has_path_open(holder.pid, path)
    if opened is False:
        return False, (
            "It has been idle past the takeover threshold, but that pid does not "
            "have the lock file open, so it was left running."
        )
    # `opened is None` is "could not ask" (Windows). The command line already
    # matched. Stopping there is the only reclaim that platform can do; the
    # sentence in `end_pid` is why it is still only that one pid.
    end_pid(holder.pid)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        # A zombie still answers `kill(pid, 0)`. It has already dropped the
        # flock; its parent is the only one who can make the pid disappear.
        if not pid_is_alive(holder.pid) or pid_is_zombie(holder.pid):
            return True, ""
        time.sleep(0.05)
    if pid_is_alive(holder.pid) and not pid_is_zombie(holder.pid):
        return False, (
            f"It has been idle past the takeover threshold, but stopping pid "
            f"{holder.pid} did not release the lock, so it was left as it was."
        )
    return True, ""


@contextmanager
def hold(repo: Path | str, state: Path | str | None = None, *,
         idle_after: float | None = None) -> Iterator[Acquired]:
    """Hold this repository for the duration of the block.

    Raises `RepoLocked` if another supervisor has it. Never blocks and never waits:
    a supervisor that queues behind another one is a supervisor nobody asked for.

    A holder whose process is gone needs nothing from this function: the kernel
    already dropped its flock. A holder that is still alive but has been idle for
    `idle_after` seconds (default `IDLE_TAKEOVER_AFTER`) is stopped and the lock
    is taken, and the resulting `Takeover` says `idle` rather than `crashed`.
    """
    root = repo_root(repo)
    path = lock_path(root, state)
    limit = IDLE_TAKEOVER_AFTER if idle_after is None else idle_after

    # O_CREAT without O_TRUNC: the previous holder's record is the crash signal, and
    # opening in "w" or "a" modes would destroy it or make it unwritable respectively.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, _FILE_MODE)
    acquired = False
    reclaimed_idle = False
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
            _raw, parsed = _read_holder(fd)
            note = ""
            if parsed is not None:
                reclaimed_idle, note = _try_reclaim_idle(parsed, path, idle_after=limit)
            if not reclaimed_idle:
                raise RepoLocked(path, parsed, idle_after=limit, note=note) from None
            try:
                lock_exclusive(fd)
            except AlreadyLocked:
                _raw, parsed = _read_holder(fd)
                raise RepoLocked(path, parsed, idle_after=limit) from None

        acquired = True
        previous = read_at(fd, 4096, 0).decode("utf-8", "replace").strip()
        if previous:
            takeover = Takeover(
                Holder.parse(previous), previous,
                because="idle" if reclaimed_idle else "crashed",
            )
        else:
            takeover = None

        stamp = _now()
        holder = Holder(
            pid=os.getpid(), repo=str(root), acquired_at=stamp, version=__version__,
            touched_at=stamp,
        )
        payload = holder.as_json().encode("utf-8")
        os.ftruncate(fd, 0)
        write_at(fd, payload, 0)
        os.fsync(fd)

        taken = Acquired(path=path, holder=holder, takeover=takeover, fd=fd)
        object.__setattr__(taken, "_span", len(payload))
        yield taken
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
            _raw, parsed = _read_holder(fd)
            # Asking does not take. `probe` is what `doctor` runs, and a diagnostic
            # that stopped an idle supervisor would be a check that changes the thing
            # it was asked to look at.
            raise RepoLocked(path, parsed, idle_after=IDLE_TAKEOVER_AFTER) from None
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
            return f"held by {describe_holder(self.holder)}"
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

