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
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import seat as seat_mod
from . import observe
from .hostos import ProcessTree, is_owner_only, pid_is_alive, process_start_token, spawn_kwargs
from .progress import Output
from . import shim as shim_mod
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
    #: The item this child was building is already `done`. A process left alive keeps
    #: committing on the same branch, and the next push moves that branch past the fix
    #: (GRPH-1053). `review` is not this reason: the child stays up to publish the
    #: revision (GRPH-987) and to review other work (PRD-39). The push guard refuses a
    #: review push that would clobber a commit already in the trunk.
    ITEM_LANDED = "item_landed"


class AttachedProcess:
    """A pid another supervisor spawned, duck-typed as `Popen` for `stop` / `running`.

    P30 D7. The original `Popen` died with the previous supervisor. `poll` is false
    while the pid is alive AND the start token still matches — a reused pid is dead
    to us, not ours to signal.
    """

    def __init__(self, pid: int, start_token: str | None) -> None:
        self.pid = pid
        self._token = start_token
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if not pid_is_alive(self.pid):
            self.returncode = 0
            return 0
        now = process_start_token(self.pid)
        if self._token and now is not None and now != self._token:
            self.returncode = 0
            return 0
        return None

    def wait(self, timeout: float | None = None) -> int:
        if self.poll() is not None:
            return int(self.returncode or 0)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.poll() is not None:
                return int(self.returncode or 0)
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(f"pid {self.pid}", timeout)
            time.sleep(0.05)


class LaunchFailed(RuntimeError):
    """The child could not be started, or died before registering.

    Names the adapter, the exit code and the tail of stderr, because the planner must
    be able to tell *your adapter is broken* from *the supervisor is gone* (D-a).
    """


class VendorLimit(LaunchFailed):
    """The VENDOR ACCOUNT is out of quota — not a broken adapter (GRPH-829).

    A subclass, so every existing `except LaunchFailed` keeps working and this can only ever
    make a failure MORE specific. The distinction is worth a type rather than a flag because
    the two call for opposite responses: a broken adapter is a config to fix and the wave
    should stop and say so; an exhausted account is a clock, and spawning the next child into
    the same account will fail identically in under a second.

    `reset` is whatever the vendor said about when it comes back, quoted rather than parsed.
    A timezone-bearing phrase like `12:20pm (America/New_York)` is more useful to the operator
    as the vendor's own words than as this package's guess at a datetime.
    """

    def __init__(self, message: str, adapter: str = "", said: str = "") -> None:
        super().__init__(message)
        self.adapter = adapter
        self.said = said


def build_holdings(agent: dict) -> list[dict]:
    """The agent's BUILD leases only — never its review claims (GRPH-1001).

    The roster reports both kinds since GRPH-1001, tagged `hold: build | review`, because a
    review claim stops an item as surely as a lease does and a supervisor could not see it.
    Everything in gbfleet that consumes `holdings`, though, was written when the list could
    only be build leases, and acts on them accordingly: it salvages a reaped child's branch
    onto them, releases them, and counts them as work in progress. A review claim is none of
    those things — releasing one is `claim_review`'s business, and attaching a reviewer's
    salvage branch to the item it was reviewing would file the reviewer's work under the
    BUILDER's name.

    An older server sends no `hold` at all. Default to "build", because that is what every
    holding meant before the field existed — treating an unlabelled hold as a review claim
    would make a current supervisor stop salvaging against an older deployment.
    """
    return [h for h in (agent.get("holdings") or [])
            if (h or {}).get("hold", "build") != "review"]


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
    #: PRD-36 D15: what the roster says a bound seat handed this child, captured when it
    #: registered. None when the seat was unbound or the roster predates PRD-36.
    assigned: dict | None = None
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
    #: True when this Child was rebuilt around a pid another supervisor spawned
    #: (P30 D7). `stop` must not try to put it in a new Windows job.
    attached: bool = False
    #: Last non-empty holdings the roster reported for this agent (P30 D9 bounce).
    #: `_roster` is overwritten after `release_item`, so reap must not read live
    #: holdings — those are empty and the salvage subject would have no item keys.
    held_items: list = field(default_factory=list)
    #: Every item the roster has EVER read this child holding, in first-seen order (GRPH-1079).
    #: `held_items` is only the latest reading, so a bound child that hands its item to
    #: review and claims another has dropped the first one from it — and the first one is
    #: the work on the branch. Reap compares the branch against this, not the latest reading.
    #: `adopt` writes it into the children file and restores it on takeover (GRPH-1085). A
    #: rebuilt Child used to start from a fresh empty list, so the handed-off item's files
    #: were judged against whatever the child held after the restart.
    ever_held: list = field(default_factory=list)
    #: False only when this Child was rebuilt from a roster that did not record `ever_held`.
    #: A child this process spawned knows its own history, including while the list is still
    #: empty — empty-and-known is "watched, held nothing". Empty-and-unknown is "the file
    #: did not say", and reap asks the server which items this agent authored rather than
    #: treating that silence as a complete history.
    ever_held_known: bool = True
    #: worker | reviewer (P30 D2). Until must not start both as a simultaneous cohort.
    role: str = "worker"
    #: The turn budget this child was launched with, when the adapter takes one. Posted with
    #: the exit report so a child that stopped AT its budget is distinguishable from one that
    #: finished early — the same number read two ways otherwise.
    turn_budget: int | None = None
    #: Per-child wall-clock cap override (seconds). When set, watch_tick uses this instead
    #: of limits.child_wall_clock. Allows frontier/effort-8 spawns to request more time
    #: without changing the global default (GRPH-849).
    wall_clock_cap: float | None = None
    #: PRD-38 D3: this child's exit has already been reported. A flag rather than a set held
    #: by the caller, because `watch_tick` is shared by `up` and the MCP supervisor and the
    #: two would keep separate sets that disagree after an adopt.
    reported: bool = False
    #: This child's worktree has been reaped — salvaged onto its branch and removed. A flag
    #: for the same reason `reported` is one, and checked by BOTH surfaces so the wave-end
    #: reap and the on-exit reap cannot run over each other.
    reaped: bool = False
    #: The model the operator ASKED for, carried from the launch (GRPH-982). Kept so the reap
    #: can compare it with the model that actually answered: `-m bogus-name` runs the
    #: configured default with no warning anywhere, so a request and a result can differ and
    #: only the child's own record knows which.
    model: str = ""
    #: PRD-41 S1. Set by the on-exit reap so the exit post can carry the diff; None
    #: until then, which the server stores as null rather than as a zero-file shape.
    diff_shape: dict | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def stdout_text(self) -> str:
        path = self.log_dir / _STDOUT
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def tail(self, name: str = _STDERR, lines: int = 20) -> str:
        path = self.log_dir / name
        if not path.exists():
            return ""
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def spawn(
    launch: Launch, worktree: Path, branch: str, log_dir: Path, base: str = "",
    shim_dir: Path | None = None
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

    # The shim goes on LAST so nothing downstream can put the real binary back in front
    # (GRPH-818). One place, because a second env-building site is a second thing to forget.
    env = shim_mod.environment({**os.environ, **launch.env}, shim_dir)
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
        model=launch.model,
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


def _resolved(path: str) -> str:
    """One spelling for one directory. Falls back to the string when it cannot resolve."""
    try:
        return os.path.realpath(path) if path else path
    except OSError:
        return path


def _account_limit(child: Child) -> str:
    """What the child's adapter makes of its output, or "" when the adapter is unknown here.

    Unknown adapter is "" rather than an error: `make_launch_factory` builds children whose
    `adapter` is a stand-in name for tests and probes, and a lookup that raised would turn a
    diagnostic into a crash on exactly the path that is already failing.
    """
    from .adapters import ADAPTERS

    adapter = ADAPTERS.get(child.adapter)
    if adapter is None:
        return ""
    return adapter.account_limit(child.stdout_text(), child.tail(_STDERR, 200))


#: A row the server still lists, and that cannot be a child that just started (GRPH-1088).
#: `offline` is derived from `last_seen_at`; `quarantined` is a stored refusal. Either way
#: the process we just spawned is not that row.
_ABSENT_STATES = ("offline", "quarantined")

#: The child's clock and the server's clock are not the same reading. A row written in the
#: same second the child started is this child; a row from the previous run is hours older.
_REGISTRATION_SLACK = timedelta(seconds=2)


def _started_wall(child: Child) -> datetime:
    """Wall time of `child.started_at`. That field is monotonic, not a timestamp."""
    elapsed = max(0.0, time.monotonic() - child.started_at)
    return datetime.now(timezone.utc) - timedelta(seconds=elapsed)


def _registered_at(agent: dict) -> datetime | None:
    """The roster's `registered_at`, or None when the row does not carry one.

    None does not mean "old". Fixtures and servers from before this field omit it, and
    treating that omission as "registered before the child" would refuse every child.
    A value that does not parse is the same: unknown, not earlier.
    """
    raw = agent.get("registered_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cannot_be_this_child(agent: dict, child: Child, started_wall: datetime) -> bool:
    """True when this roster row is some other run's agent on the same directory.

    A reused wave name reuses the worktree path. The previous row stays listed, often
    still `working`, until presence lapses — and the first row with that path is the
    previous run (GRPH-1088). Skipping it is the match. Waiting for it to disappear
    is how the new child got stopped under the old id.
    """
    if agent.get("retired") or agent.get("dismissed"):
        return True
    if agent.get("state") in _ABSENT_STATES:
        return True
    seat = child.seat_id
    enrolment = agent.get("enrolment_id")
    # Set only when this process already knows the seat (adopt, or a test). A fresh
    # child learns `seat_id` FROM the row, so an empty seat does not filter.
    if seat and enrolment and enrolment != seat:
        return True
    registered = _registered_at(agent)
    if registered is not None and registered < started_wall - _REGISTRATION_SLACK:
        return True
    return False


def _pick_this_child(candidates: list[dict]) -> dict | None:
    """One row that can be this child, or None when the roster does not say.

    A stamp wins over an unstamped row: the server emits `registered_at`, and the
    latest one is the registration that just happened. One unstamped row still
    matches, because that is every fixture from before the field existed. Several
    unstamped rows on one path do not: taking the first is the bug.
    """
    if not candidates:
        return None
    dated: list[tuple[datetime, dict]] = []
    for agent in candidates:
        when = _registered_at(agent)
        if when is not None:
            dated.append((when, agent))
    if dated:
        return max(dated, key=lambda pair: pair[0])[1]
    if len(candidates) == 1:
        return candidates[0]
    return None


def await_registration(
    child: Child,
    roster: Callable[[], dict],
    window: float = REGISTRATION_WINDOW,
    poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Block until the child appears on the roster, or kill it and say which adapter.

    **Matched on the worktree, then on which row can be this child.** The worktree is
    what the child reports and what this supervisor created. It is not unique across
    runs (GRPH-1088): the previous agent's row is still there, and the first row with
    that path is the previous run. Offline, quarantined, retired, dismissed, registered
    before this child started, or sitting on a different seat — none of those is this
    child. When `child.seat_id` is already set, that seat is the stronger key.

    The enrolment id has been on the roster since GRPH-451. A fresh child does not
    know it until this match reads it, so the production path filters on
    `registered_at` rather than waiting for a seat id the child does not have yet.
    It is self-reported by the child, so this is cooperative identification and not
    authentication — which is all it needs to be, since the child is ours and the
    server is what actually decides anything.
    """
    # Compared RESOLVED (GRPH-718 walk finding): the child reports `os.path.realpath` of its
    # worktree, and a supervisor started with `--workspace ../x` or through a symlink carries
    # the unresolved spelling — the same directory, two strings, and a child that registered
    # was reported never_registered and killed.
    target = _resolved(str(child.worktree))
    deadline = child.started_at + window

    def matched() -> dict | None:
        started_wall = _started_wall(child)
        candidates = []
        for agent in roster().get("agents") or []:
            if _resolved(str(agent.get("worktree") or "")) != target:
                continue
            if _cannot_be_this_child(agent, child, started_wall):
                continue
            candidates.append(agent)
        agent = _pick_this_child(candidates)
        if agent is None:
            return None
        child.agent_id = agent.get("id")
        # Available since GRPH-451 put the seat on the roster. Before that,
        # `enrolled` was a bare boolean and a supervisor could not name the seat
        # its own child had redeemed.
        child.seat_id = agent.get("enrolment_id")
        got = agent.get("assigned")
        child.assigned = got if isinstance(got, dict) else None
        child.registration_latency = time.monotonic() - child.started_at
        held = [h.get("id") for h in build_holdings(agent) if h.get("id")]
        if held:
            child.held_items = held
            child.ever_held += [i for i in held if i not in child.ever_held]
        return agent

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
            # GRPH-829: ask the adapter whether the VENDOR said something about the account
            # before blaming the adapter. Both streams, because the one measured instance of
            # this printed 67 bytes to stdout and nothing at all to stderr — so the report
            # read `child exited 1 before registering. stderr tail:` followed by nothing, and
            # sent the operator to debug a CLI that was working perfectly.
            said = _account_limit(child)
            if said:
                raise VendorLimit(
                    f"adapter {child.adapter!r}: the vendor account is out of quota, so no "
                    f"child can start until it resets. It said: {said}",
                    adapter=child.adapter, said=said,
                )
            raise LaunchFailed(
                f"adapter {child.adapter!r}: child exited {code} before registering.\n"
                # Both streams named, and both PRINTED. An empty stderr used to be rendered as
                # an empty tail with no hint that the other stream existed, which is how a
                # message sitting in stdout.log stayed unread through a whole wave.
                f"stderr tail:\n{child.tail()}\n"
                f"stdout tail:\n{child.tail(_STDOUT)}"
            )

        if (agent := matched()) is not None:
            return agent

        if time.monotonic() >= deadline:
            stop(child, Reason.NEVER_REGISTERED)
            raise LaunchFailed(
                f"adapter {child.adapter!r}: still not registered after {window:.0f}s "
                f"(pid {child.pid}). Killed. A child that runs without registering is "
                "the silent drop — it burns money and produces nothing while the roster "
                f"just shows one agent fewer.\nstderr tail:\n{child.tail()}\n"
                f"stdout tail:\n{child.tail(_STDOUT)}"
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

    tree = child.tree or ProcessTree(child.process, attached=child.attached)
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
