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
from . import observe
from .hostos import ProcessTree, is_owner_only, spawn_kwargs
from .progress import Output
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
    #: PRD-22 §7 authorises a max wall-clock per child as one of the three things the
    #: supervisor "enforces because it can measure them" — and D-d's list of kill cases
    #: does not include it. The two sections disagree. Following §7, because the whole
    #: point it argues is that a limit people rely on which silently does not bind is
    #: worse than no limit at all, and a wall-clock cap that never fires is exactly that.
    WALL_CLOCK = "wall_clock"
    #: The server is reachable, answering, and no longer counts this child — while its
    #: process is still alive. D-d's backstop (GRPH-452).
    #:
    #: Deliberately NOT named `seat_revoked`, because the supervisor cannot prove that.
    #: A revoked seat, a revoked credential and a child whose MCP client died all look
    #: identical from here: the roster stops listing it, or lists it `offline`. What is
    #: observable is that the server has stopped counting it, and all three mean the same
    #: thing operationally — the claim is gone and the process is spending money without
    #: one. Naming it for the cause would be asserting something unmeasured.
    SEAT_GONE = "seat_gone"


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
    #: The on-disk language `seat_path` must be written in. Carried on the Launch rather
    #: than inferred from the suffix: a `.toml` path written as JSON is a file the vendor
    #: silently ignores, and guessing from an extension is how that mistake gets made
    #: twice. `adapters.Adapter.seat_format` is the source, and a test asserts every
    #: adapter's `launch()` actually passes its own.
    seat_format: str = seat_mod.JSON
    #: What `--version` reported for the binary that will run. Carried so the child's
    #: record can name the build (S6), not just the vendor.
    binary_version: str = ""
    #: The model the CALLER named, or "" for the vendor default. Carried for the same
    #: reason as `binary_version`: a record naming the vendor but not the model cannot
    #: answer "was this the cheap one?" — which is the whole point of naming it.
    model: str = ""
    #: Fed to the child on stdin. This is how the enrolment CODE reaches a vendor whose
    #: only prompt channel is a command-line argument: argv is readable by every process
    #: on the machine, stdin is not. `grok` takes `--prompt-file` and needs neither.
    stdin_file: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    #: Where this vendor was told to write a debug log, or None when it has no flag for
    #: one. `None` is a real answer and the supervisor reports it: `cursor-agent` and
    #: `gbagent` have no debug flag at all, and an operator who asked for `--debug` and
    #: silently got nothing from half the fleet has been misled about how much they can
    #: see (GRPH-579).
    debug_path: Path | None = None


@dataclass
class Child:
    adapter: str
    worktree: Path
    branch: str
    #: The commit the worktree was cut from, carried so what the child changed can be
    #: measured after the worktree itself is gone.
    base: str
    seat_path: Path
    process: subprocess.Popen
    started_at: float
    log_dir: Path
    #: Control over the child AND everything it spawned. A vendor CLI's helpers are its
    #: own children, and signalling only the leader leaves them running and working.
    #: POSIX gets this from a session and `killpg`; Windows from a job object. `None`
    #: only for a `Child` built by hand, which `stop` handles by falling back to the pid.
    tree: "ProcessTree | None" = None
    #: Reads the child's log files to answer "is it producing anything?" — the only
    #: liveness signal that needs no network and no cooperation from the vendor. See
    #: `progress.Output` for why silence is reported and never acted on.
    output: "Output | None" = None
    #: Where the vendor was told to write its debug log, when it can. None means this
    #: adapter has no such flag, which is reported rather than left to be inferred.
    debug_path: Path | None = None
    agent_id: str | None = None
    binary_version: str = ""
    #: The enrolment's ROW id, read off the roster once the child registers. Never the
    #: code: a code is single-use and short-lived, and a log file is neither.
    seat_id: str | None = None
    #: **The load-bearing observability field** (S6). A child that never registers is a
    #: process that runs, burns money and produces nothing, while the roster simply
    #: shows one agent fewer than expected. This is what separates that from a slow
    #: start; without it the two are indistinguishable from outside.
    registration_latency: float | None = None
    #: When the roster first read this child `offline`, or None while the server still
    #: counts it. The backstop needs SUSTAINED silence rather than one reading, because
    #: `offline` is derived from `last_seen_at` and a busy child produces it exactly as a
    #: revoked one does (GRPH-452). Cleared when the child reappears, so two unrelated
    #: quiet spells are never summed into one.
    offline_since: float | None = None
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


def spawn(
    launch: Launch, worktree: Path, branch: str, log_dir: Path, base: str = ""
) -> Child:
    """Start one child in its own worktree, holding its own seat.

    stdout and stderr go to FILES rather than pipes. A pipe nobody drains fills its
    buffer and blocks the writer, and a headless vendor CLI is exactly the sort of
    long-running chatty process that would hit that — a child wedged on a full pipe
    looks identical to a child thinking hard about something.

    The child is put at the head of its own process group so `stop` can signal the whole
    tree. A vendor CLI that spawns its own helpers is the normal case, and signalling
    only the leader leaves them running. How that is spelled — a new session on POSIX, a
    new process group plus a job object on Windows — lives in `hostos`.
    """
    worktree = Path(worktree)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    seat_mod.write(launch.seat_path, launch.config, launch.seat_format)
    # Verified, not assumed. `seat.write` asks for the restriction; this checks it took.
    # The two are separate because the platform where the request silently did nothing is
    # exactly the platform where nobody was checking (GRPH-584). Reported rather than
    # refused: D-k says this is not a security boundary, and a workspace on a filesystem
    # with no permissions at all must not stop the fleet — it must not be quiet either.
    if not is_owner_only(launch.seat_path):
        observe.emit(
            "credential_unrestricted",
            path=str(launch.seat_path),
            what="api key",
            adapter=launch.adapter,
            detail=(
                "the seat file could not be restricted to this user and may be readable "
                "by others on this host"
            ),
        )

    env = {**os.environ, **launch.env}
    started = time.monotonic()
    stdin = subprocess.DEVNULL
    if launch.stdin_file is not None:
        stdin = open(launch.stdin_file, "rb")
    try:
        process = subprocess.Popen(
            launch.argv,
            cwd=str(worktree),
            env=env,
            stdin=stdin,
            stdout=open(log_dir / _STDOUT, "wb"),
            stderr=open(log_dir / _STDERR, "wb"),
            **spawn_kwargs(),
        )
    except OSError as exc:
        raise LaunchFailed(
            f"adapter {launch.adapter!r}: could not start {launch.argv[0]!r}: {exc}"
        ) from exc
    finally:
        if stdin is not subprocess.DEVNULL:
            stdin.close()  # the child holds its own dup

    return Child(
        adapter=launch.adapter,
        worktree=worktree,
        branch=branch,
        base=base,
        seat_path=Path(launch.seat_path),
        process=process,
        started_at=started,
        log_dir=log_dir,
        tree=ProcessTree(process),
        output=Output.watching(
            [p for p in (log_dir / _STDOUT, log_dir / _STDERR, launch.debug_path) if p],
            started_at=started,
        ),
        debug_path=launch.debug_path,
        binary_version=launch.binary_version,
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

    def matched() -> dict | None:
        for agent in roster().get("agents") or []:
            if agent.get("worktree") == target:
                child.agent_id = agent.get("id")
                # Available since GRPH-451 put the seat on the roster. Before that,
                # `enrolled` was a bare boolean and a supervisor could not name the seat
                # its own child had redeemed.
                child.seat_id = agent.get("enrolment_id")
                child.registration_latency = time.monotonic() - child.started_at
                return agent
        return None

    while True:
        if not child.running:
            # **Ask the roster before concluding it never registered.** A worker that
            # registers, finds nothing to claim and exits is doing the normal thing —
            # D-c says exiting on empty is the normal end of a worker's life, not a
            # failure — and a fast one can be gone before the first poll. Checking
            # liveness first reported exactly that child as a broken adapter, which is
            # both wrong and the most expensive way to be wrong: the operator goes
            # looking at the vendor.
            #
            # Found by the acceptance walk (PRD-22 §9) on its third step. No mock caught
            # it because mocked children sleep and mocked rosters always answer.
            if (agent := matched()) is not None:
                return agent
            code = child.process.returncode
            stop(child, Reason.NEVER_REGISTERED)
            raise LaunchFailed(
                f"adapter {child.adapter!r}: child exited {code} before registering.\n"
                f"stderr tail:\n{child.tail()}"
            )

        if (agent := matched()) is not None:
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
    """Ask the child's whole tree to stop, then make it.

    Signals the TREE, not the pid: a vendor CLI's helper processes are children of the
    child, and killing only the leader leaves them holding file handles and, worse,
    still working.

    On Windows this ordering is not merely tidy, it is required. Measured there:
    `Popen.terminate()` kills the leader and leaves the grandchild running, and
    `taskkill /T` against a leader that has already exited fails with "process not
    found" — so a graceful step that kills the leader outright destroys the only handle
    the forceful step had. `hostos.ProcessTree` sidesteps that with a job object, which
    can terminate the tree whatever state the leader is in.

    **Cleans up nothing.** No seat file removed, no worktree touched, no branch deleted.
    Every path into here is a path where something already went wrong, and tidying at
    that moment is how uncommitted work disappears. Salvage happens at reap.
    """
    child.stopped_because = reason
    if not child.running:
        return Stopped(reason=reason, escalated=False, exit_code=child.process.returncode)

    tree = child.tree or ProcessTree(child.process)
    tree.terminate()
    try:
        code = child.process.wait(timeout=grace)
        return Stopped(reason=reason, escalated=False, exit_code=code)
    except subprocess.TimeoutExpired:
        pass

    tree.kill()
    try:
        code = child.process.wait(timeout=grace)
    except subprocess.TimeoutExpired:  # pragma: no cover - a killed tree does not linger
        code = None
    return Stopped(reason=reason, escalated=True, exit_code=code)
