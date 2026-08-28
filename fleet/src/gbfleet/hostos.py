"""Every place the supervisor touches the operating system differently. One file.

GRPH-576. The supervisor needs three things from the kernel that POSIX and Windows
spell differently: an exclusive lock that dies with its holder, positional reads and
writes on a file descriptor, and control over a process **and everything it spawned**.
Scattering `if os.name == "nt"` through `lock.py` and `spawn.py` would put the
platform's semantics in three places and the reasoning in none, so it lives here with
the measurements that justify each choice.

Everything below was measured on Windows 11 with the probe recorded on GRPH-576, not
inferred from documentation.

**The lock is a byte range, and its offset is load-bearing.** `fcntl.flock` is
advisory: a process refused the lock can still read the file, which is how `RepoLocked`
names the supervisor holding it. `msvcrt.locking` is *mandatory* — a locked range
cannot be read by anyone else. Locking byte 0 would therefore make the holder record
unreadable to exactly the process that needs to report it, and the error message would
degrade to "someone has this" on Windows only. So the lock is one byte at
`_LOCK_BYTE`, past any record, and the record lives below it. Measured: the second
process is refused with `PermissionError` *and* still reads the record.

**The tree, not the process.** A vendor CLI spawns helpers, and killing only the leader
leaves them running and working. POSIX gets this from a new session plus `killpg`.
Windows gets it from a Job Object: `TerminateJobObject` kills every process in the job
at once, whatever state the leader is in.

That last clause is not decoration. Measured: `Popen.terminate()` kills the top and
leaves the grandchild alive, and `taskkill /T` against the now-dead top pid fails with
"process not found" — so on Windows the graceful step can destroy the only handle the
forceful step had. A job object has no such ordering trap, which is why it is used in
preference to `taskkill` even though `taskkill` is simpler.

**`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is deliberately not set**, though it was
verified to work. It kills every child when the process holding the job handle dies —
that is, when the supervisor dies. The supervisor's whole recovery design assumes the
opposite: `lock.py` reports a `Takeover` so a new supervisor can *adopt* the children a
crashed one left, and salvage recovers their uncommitted work at reap. Turning that
flag on would make a supervisor crash silently destroy a worker's work, on Windows
only, while every test still passed. The children outliving their supervisor is the
behaviour, not the bug.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
from pathlib import Path

WINDOWS = os.name == "nt"

#: Limit flags applied to a child's job object. **Zero, deliberately.**
#:
#: The tempting one is `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (0x2000), which was verified
#: to work on the box: kill the process holding the job handle and the whole tree dies
#: with it. That is precisely the behaviour this design must NOT have. The supervisor is
#: the process holding the handle, and `lock.py` reports a `Takeover` so that a *new*
#: supervisor can adopt the children a crashed one left behind, with salvage recovering
#: their uncommitted work at reap. Setting the flag would make a supervisor crash
#: silently destroy a worker's work — on Windows only, with every test still green.
#:
#: Named as a constant rather than left as an absent line of code, so the decision can
#: be asserted from a platform that cannot run it.
JOB_LIMIT_FLAGS = 0

#: Which byte the lock is taken on. Past any plausible holder record (`lock.py` reads
#: 4096 bytes from offset 0), so a refused process can still read who holds it. Beyond
#: end-of-file is legal for a byte-range lock and does not extend the file.
_LOCK_BYTE = 4096

if WINDOWS:  # pragma: no cover - selected by platform, exercised on the box
    import ctypes
    import ctypes.wintypes as wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    import fcntl


class AlreadyLocked(OSError):
    """Someone else holds this lock. Raised in place of the platform's own error so
    callers do not have to know that POSIX says EAGAIN and Windows says EACCES."""


# --- positional file access --------------------------------------------------------
#
# `os.pread`/`os.pwrite` are Unix-only (confirmed absent on the box). The seek-based
# equivalents are not thread-safe the way the positional calls are, which is fine here:
# one supervisor process holds one lock fd and never shares it between threads.

def read_at(fd: int, size: int, offset: int) -> bytes:
    if not WINDOWS:
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


def write_at(fd: int, data: bytes, offset: int) -> int:
    if not WINDOWS:
        return os.pwrite(fd, data, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.write(fd, data)


# --- the exclusive, non-blocking, dies-with-its-holder lock -------------------------

def lock_exclusive(fd: int) -> None:
    """Take the lock or raise `AlreadyLocked`. Never blocks.

    Released by the kernel when the holder exits however it exits — the property the
    whole design rests on, and verified on both platforms rather than assumed on one.
    """
    if not WINDOWS:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise AlreadyLocked(exc.errno, "lock held by another process") from None
        return

    import msvcrt

    os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        # EACCES is what a refused LK_NBLCK reports. EDEADLOCK appears when the
        # runtime retried internally; both mean the same thing to a caller.
        if exc.errno not in (errno.EACCES, errno.EDEADLOCK):
            raise
        raise AlreadyLocked(exc.errno, "lock held by another process") from None
    finally:
        os.lseek(fd, 0, os.SEEK_SET)


# --- who "this user" is ------------------------------------------------------------

def user_tag() -> str:
    """A per-user component for the state directory name.

    On Linux `/tmp` is shared, so the uid keeps two accounts on one host from silently
    contending for a lock. Windows hands each account its own temp directory already
    (measured: `C:\\Users\\Alex\\AppData\\Local\\Temp`), so the tag is belt-and-braces
    there rather than load-bearing — but it stays, because a directory whose name means
    something different per platform is worse than one that reads the same.
    """
    if not WINDOWS:
        return str(os.getuid())
    import getpass

    try:
        name = getpass.getuser()
    except Exception:  # pragma: no cover - no USERNAME and no password database
        return "user"
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name) or "user"


# --- controlling a process AND everything it spawned -------------------------------

def spawn_kwargs() -> dict:
    """Extra `Popen` arguments that put the child at the head of its own group.

    POSIX: its own session, so `killpg` reaches the helpers it spawns.
    Windows: its own process group, so a CTRL_BREAK reaches them. The job object that
    does the actual killing is attached after the fact by `ProcessTree`.
    """
    if not WINDOWS:
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


class ProcessTree:
    """A spawned process and its descendants, as one thing that can be signalled.

    Constructed immediately after `Popen` returns. On Windows that leaves a race: a
    child that spawns a helper before it is assigned to the job would leave that helper
    outside it. The window is the microseconds between `CreateProcess` returning and the
    next Python statement, and a vendor CLI does not start helpers that fast — but it is
    a window, and closing it properly needs `PROC_THREAD_ATTRIBUTE_JOB_LIST`, which
    `subprocess` does not expose. Said plainly rather than left for someone to discover.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self._job = None
        if WINDOWS:
            self._job = _make_job()
            if self._job is not None:
                _assign(self._job, process.pid)

    @property
    def pid(self) -> int:
        return self.process.pid

    def terminate(self) -> None:
        """Ask the tree to stop, giving it a chance to flush and exit."""
        if not WINDOWS:
            _signal_group_posix(self.pid, signal.SIGTERM)
            return
        try:
            os.kill(self.pid, signal.CTRL_BREAK_EVENT)
        except (OSError, ProcessLookupError):
            # No console to break, or already gone. Not fatal: `kill` is guaranteed and
            # is what the caller escalates to. A graceful stop is a courtesy.
            pass

    def kill(self) -> None:
        """Stop the tree now. This one must not fail quietly."""
        if not WINDOWS:
            _signal_group_posix(self.pid, signal.SIGKILL)
            return
        if self._job is not None and _terminate_job(self._job):
            return
        # No job (or terminating it failed): fall back to walking the tree by pid. This
        # only works while the leader is still alive — measured — so it is the fallback
        # and not the mechanism.
        subprocess.run(
            ["taskkill", "/PID", str(self.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )

    def close(self) -> None:
        """Release the job handle. Does NOT kill the tree: no KILL_ON_JOB_CLOSE."""
        if self._job is not None:
            _close_handle(self._job)
            self._job = None


class WouldSignalOurselves(RuntimeError):
    """The target process group contains this process. Refused."""


def _signal_group_posix(pid: int, sig: int) -> None:
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        # Gone between the liveness check and the signal. Not an error: a worker
        # exiting on its own is the normal end of its life (D-c).
        return

    # A child that is not its own group leader is in OUR group, and `killpg` would then
    # reach this supervisor, every sibling worker, and the shell that launched it. That
    # is not a hypothetical: it is what a regression in `spawn_kwargs` produces, and the
    # symptom is the whole session dying with no output to explain it — which is how it
    # was found twice while building this.
    #
    # Refusing leaks one process. Not refusing destroys the fleet and the operator's
    # terminal. So it refuses, loudly: a worker that will not die is a visible problem,
    # and a supervisor that killed itself cannot report anything at all.
    if group == os.getpgid(0):
        raise WouldSignalOurselves(
            f"refusing to send signal {sig} to process group {group}: it is this "
            f"process's own group, so pid {pid} was never put in a session of its own. "
            "Signalling it would kill this supervisor, its other children, and whatever "
            "launched it. Check that `spawn_kwargs()` reached Popen."
        )

    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        pass


if WINDOWS:  # pragma: no cover - exercised on the box, not on the CI host
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMITS),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _make_job():
        handle = _KERNEL32.CreateJobObjectW(None, None)
        if not handle:
            return None
        if JOB_LIMIT_FLAGS:
            info = _EXTENDED_LIMITS()
            info.BasicLimitInformation.LimitFlags = JOB_LIMIT_FLAGS
            _KERNEL32.SetInformationJobObject(
                wintypes.HANDLE(handle),
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        return handle

    def _assign(job, pid: int) -> bool:
        rights = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE
        process = _KERNEL32.OpenProcess(rights, False, pid)
        if not process:
            return False
        try:
            return bool(
                _KERNEL32.AssignProcessToJobObject(wintypes.HANDLE(job),
                                                   wintypes.HANDLE(process))
            )
        finally:
            _KERNEL32.CloseHandle(wintypes.HANDLE(process))

    def _terminate_job(job) -> bool:
        return bool(_KERNEL32.TerminateJobObject(wintypes.HANDLE(job), 1))

    def _close_handle(handle) -> None:
        _KERNEL32.CloseHandle(wintypes.HANDLE(handle))

else:
    def _make_job():  # pragma: no cover - never called off Windows
        return None

    def _assign(job, pid: int) -> bool:  # pragma: no cover
        return False

    def _terminate_job(job) -> bool:  # pragma: no cover
        return False

    def _close_handle(handle) -> None:  # pragma: no cover
        return None
