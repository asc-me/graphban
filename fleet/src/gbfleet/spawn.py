"""Launching a child on a seat the server issued, and killing it when the time comes.

PRD-22 D-b, D-c, D-d, D-f. The supervisor holds no authority here: it starts a process
holding a seat the *server* minted, to do work the *server* arbitrates. What it owns is
the process — starting it in its own worktree, noticing when it never registered, and
ending it.

**Exit is the normal end of a worker's life, not a failure** (D-c). A spawned worker
takes `wait_seconds=0`, works what it claims and exits on empty; the idling moves to
the supervisor, which is a few MB of Python and free.

**Killing never cleans up.** The worktree is left exactly as it is and salvage happens
at reap (`worktree.reap`). A kill that tidied would be a kill that could destroy work,
and the four cases below all happen when something has already gone sideways.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import seat as seat_mod
from .seat import Seat

#: How long a child gets to register before it is presumed broken. Seconds, not the
#: seat's 30 minutes: S2 requires that a broken adapter fail loudly rather than produce
#: a child that runs and never registers — the silent drop, which is indistinguishable
#: from a slow start until something puts a bound on "slow".
REGISTRATION_WINDOW = 90.0

#: Between SIGTERM and SIGKILL. Long enough for a vendor CLI to flush and exit.
TERM_GRACE = 10.0

_STDOUT = "stdout.log"
_STDERR = "stderr.log"


class Reason(str, Enum):
    """The only four things that kill a child (PRD-22 D-d).

    `fleet_idle` is deliberately absent: the worker exits itself on empty per D-c, and
    two things owning one transition is how they come to disagree about it.
    """

    ASKED = "asked"  # an explicit `stop`
    LEASE_LAPSED = "lease_lapsed"  # deadline reached with no successful heartbeat
    SCALED_DOWN = "scaled_down"  # --max-workers lowered
    SHUTDOWN = "shutdown"  # the supervisor is going away
    NEVER_REGISTERED = "never_registered"  # the bounded window above


class LaunchFailed(RuntimeError):
    """The child could not be started, or died before registering.

    Names the adapter, the exit code and the tail of stderr, because the planner must
    be able to tell *your adapter is broken* from *the supervisor is gone* (D-a).
    """


@dataclass(frozen=True)
class Launch:
    """Everything one adapter needs to start one child. GRPH-449 builds these."""

    adapter: str
    argv: list[str]
    #: Absolute. Inside the worktree only where a vendor forces it (Cursor).
    seat_path: Path
    config: dict
    instruction: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Child:
    adapter: str
    worktree: Path
    branch: str
    seat_path: Path
    process: subprocess.Popen
    started_at: float
    log_dir: Path
    agent_id: str | None = None
    #: **The load-bearing observability field** (S6). A child that never registers is a
    #: process that runs, burns money and produces nothing, while the roster simply
    #: shows one agent fewer than expected. This is what separates that from a slow
    #: start; without it the two are indistinguishable from outside.
    registration_latency: float | None = None
    stopped_because: Reason | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def tail(self, name: str = _STDERR, lines: int = 20) -> str:
        path = self.log_dir / name
        if not path.exists():
            return ""
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def spawn(launch: Launch, worktree: Path, branch: str, log_dir: Path) -> Child:
    """Start one child in its own worktree, holding its own seat.

    stdout and stderr go to FILES rather than pipes. A pipe nobody drains fills its
    buffer and blocks the writer, and a headless vendor CLI is exactly the sort of
    long-running chatty process that would hit that — a child wedged on a full pipe
    looks identical to a child thinking hard about something.

    `start_new_session` puts the child in its own process group so `stop` can signal
    the whole tree. A vendor CLI that spawns its own helpers is the normal case, and
    signalling only the leader leaves them running.
    """
    worktree = Path(worktree)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    seat_mod.write(launch.seat_path, launch.config)

    env = {**os.environ, **launch.env}
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            launch.argv,
            cwd=str(worktree),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=open(log_dir / _STDOUT, "wb"),
            stderr=open(log_dir / _STDERR, "wb"),
            start_new_session=True,
        )
    except OSError as exc:
        raise LaunchFailed(
            f"adapter {launch.adapter!r}: could not start {launch.argv[0]!r}: {exc}"
        ) from exc

    return Child(
        adapter=launch.adapter,
        worktree=worktree,
        branch=branch,
        seat_path=Path(launch.seat_path),
        process=process,
        started_at=started,
        log_dir=log_dir,
    )


def await_registration(
    child: Child,
    roster: Callable[[], dict],
    window: float = REGISTRATION_WINDOW,
    poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Block until the child appears on the roster, or kill it and say which adapter.

    **Matched on `worktree`.** The obvious key would be the enrolment id — walk step 3
    asks for exactly that — but `fleet_status` exposes `enrolled` as a bare boolean and
    never the id (the gap PRD-22 §6 is about, and GRPH-451's to close). The worktree is
    the next best thing and is structurally unique: one worker one worktree (D-g), and
    one supervisor per repo (D-h). It is self-reported by the child, so this is
    cooperative identification and not authentication — which is all it needs to be,
    since the child is ours and the server is what actually decides anything.
    """
    target = str(child.worktree)
    deadline = child.started_at + window

    while True:
        if not child.running:
            code = child.process.returncode
            stop(child, Reason.NEVER_REGISTERED)
            raise LaunchFailed(
                f"adapter {child.adapter!r}: child exited {code} before registering.\n"
                f"stderr tail:\n{child.tail()}"
            )

        for agent in roster().get("agents") or []:
            if agent.get("worktree") == target:
                child.agent_id = agent.get("id")
                child.registration_latency = time.monotonic() - child.started_at
                return agent

        if time.monotonic() >= deadline:
            stop(child, Reason.NEVER_REGISTERED)
            raise LaunchFailed(
                f"adapter {child.adapter!r}: still not registered after {window:.0f}s "
                f"(pid {child.pid}). Killed. A child that runs without registering is "
                "the silent drop — it burns money and produces nothing while the roster "
                f"just shows one agent fewer.\nstderr tail:\n{child.tail()}"
            )
        sleep(poll)


@dataclass(frozen=True)
class Stopped:
    reason: Reason
    escalated: bool
    exit_code: int | None


def stop(child: Child, reason: Reason, grace: float = TERM_GRACE) -> Stopped:
    """SIGTERM the child's process group, then SIGKILL it if it is still there.

    Signals the GROUP, not the pid: a vendor CLI's helper processes are children of the
    child, and killing only the leader leaves them holding file handles and, worse,
    still working.

    **Cleans up nothing.** No seat file removed, no worktree touched, no branch deleted.
    Every path into here is a path where something already went wrong, and tidying at
    that moment is how uncommitted work disappears. Salvage happens at reap.
    """
    child.stopped_because = reason
    if not child.running:
        return Stopped(reason=reason, escalated=False, exit_code=child.process.returncode)

    _signal_group(child, signal.SIGTERM)
    try:
        code = child.process.wait(timeout=grace)
        return Stopped(reason=reason, escalated=False, exit_code=code)
    except subprocess.TimeoutExpired:
        pass

    _signal_group(child, signal.SIGKILL)
    try:
        code = child.process.wait(timeout=grace)
    except subprocess.TimeoutExpired:  # pragma: no cover - a SIGKILLed group does not linger
        code = None
    return Stopped(reason=reason, escalated=True, exit_code=code)


def _signal_group(child: Child, sig: int) -> None:
    try:
        os.killpg(os.getpgid(child.pid), sig)
    except ProcessLookupError:
        # Gone between the liveness check and the signal. Not an error: a worker
        # exiting on its own is the normal end of its life (D-c).
        pass
