"""`gbfleet up` — one wave, deterministically, with no LLM in the loop.

PRD-22 S1. Reads `propose_allocation`, mints nothing, takes pre-minted seats, spawns a
child per seat in its own worktree, waits for them to finish, and reaps. Shippable and
useful alone: if the deterministic version proves sufficient, stopping here is a real
outcome rather than a failure to finish.

**The supervisor never decides a role** (D-j). Every seat's role was fixed by the
server when the planner minted it; redeeming a seat from a pre-authorised pool is not
assigning a role, because the authority was granted at mint time. What the supervisor
decides is HOW MANY of an already-authorised kind to run, and when to stop.

**On `propose_allocation`, honestly.** D-j says the server computes the mix and the
supervisor executes it — but the proposal is computed over the agents *already
registered* (`services/fleet.py:1938`), so before any child exists it returns
`workers: 0` with the rationale "no agents online — nothing to allocate". It cannot
bootstrap. In deterministic mode that is fine, because the operator decided the count
by minting that many seats. So the proposal is read and REPORTED rather than obeyed,
both before and after the wave, and `AllocationRead` keeps "the server has no opinion
yet" separate from "the server says nothing is needed". Those are different answers and
only one of them means stop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import adopt as adopt_mod
from . import worktree as wt_mod
from .client import Graphban, ServerUnreachable
from .hostos import restrict_to_owner
from .lock import Acquired, hold
from . import observe
from . import seat as seat_mod
from . import touchpoints as tp_mod
from .observe import NEVER_REGISTERED, ChildRecord
from .seat import Seat, instruction_for
from .spawn import (
    REGISTRATION_WINDOW, Child, Launch, LaunchFailed, Reason, await_registration,
    spawn, stop,
)
from .worktree import Reaped, Worktree

#: Defaults chosen to be boring. PRD-22's own risk table keeps `max_workers` at 4 on
#: the grounds that human attention is still the ceiling: removing the human from
#: *spawn* does not remove them from *bounce adjudication* and *resume*.
DEFAULT_MAX_WORKERS = 4


@dataclass(frozen=True)
class Limits:
    """Only what can actually be measured (PRD-22 §7).

    There is deliberately no spend ceiling. Vendors report usage inconsistently and
    some not at all in headless output, and a budget guardrail people rely on which
    silently does not bind is worse than none — the reliance is what causes the spend.
    """

    max_workers: int = DEFAULT_MAX_WORKERS
    max_children: int = 8
    child_wall_clock: float = 3600.0
    #: How long a child gets to register before it is presumed broken (S2). A limit the
    #: supervisor enforces because it can measure it, same as the two above — and the
    #: only one of the three whose default is short enough to matter in a test.
    registration_window: float = REGISTRATION_WINDOW
    #: How long a child may read `offline` on the roster before the backstop stops it
    #: (GRPH-452). NOT the presence TTL, and the difference is the whole point: `offline`
    #: means "no heartbeat within the TTL", which a revoked child and a BUSY one produce
    #: identically. The presence TTL is 150s by default and one run of this repository's
    #: own backend suite is ~9 minutes of silence, so acting on the first reading stops
    #: healthy children for working.
    #:
    #: 1800s is the longest single blocking call this fleet ships — `gbagent`'s run_tests
    #: timeout — so a child cannot legitimately be quieter than this inside one tool call.
    #: Deliberately generous: a revoked child costs extra spend, a killed healthy one
    #: costs the work AND the spend, and only one of those is recoverable.
    disowned_after: float = 1800.0
    #: How long a child may write nothing before the wave REPORTS it as quiet. Not a
    #: kill threshold and deliberately much shorter than `disowned_after`: nothing acts
    #: on it, so a false positive costs one line, while the thing it catches — a child
    #: alive and producing nothing — was previously invisible for the full 1800s and then
    #: blamed on the network. A child in one long tool call will trip this and that is
    #: fine; it is an observation, not an accusation.
    quiet_after: float = 300.0


@dataclass(frozen=True)
class AllocationRead:
    """What the server said about the mix, and whether it was in a position to say it.

    `workers: 0` has two meanings and they are opposites. With an empty roster it means
    "I cannot answer yet"; with a populated one it means "nothing more is needed". Left
    as a bare zero, the reassuring reading is the one a supervisor would act on — and it
    would be acting on it at exactly the moment the answer is meaningless.
    """

    workers: int
    reviewers: int
    rationale: str
    #: True when the server had no live agents to allocate over, so the numbers above
    #: describe its ignorance rather than the work.
    uninformative: bool

    #: **For whoever builds the continuous scaling loop.** D-j's escape hatch is a pool
    #: of seats minted up front, with the supervisor deciding how many to redeem rather
    #: than waking an LLM planner for each one. That decision needs a count of
    #: non-colliding work, and `propose_allocation` cannot give it before a child exists.
    #:
    #: The answer is `collision_clusters`, which is already an MCP tool and already
    #: ungated, so it costs nothing against the manifest — but ONLY for the cold start.
    #: It calls `clusters_for_project` directly and does NOT subtract active
    #: reservations, which `propose_allocation` filters for itself. With no agents there
    #: are no reservations and the two agree exactly; once agents hold reservations it
    #: OVERCOUNTS, and a supervisor trusting it would start workers with nothing
    #: non-colliding left to claim.
    #:
    #: So: `collision_clusters` while the roster is empty, `propose_allocation` the
    #: moment it is not. Written down here because "use the other one once warm" is
    #: exactly the kind of rule that gets simplified away by someone tidying.
    #:
    #: This is not needed by `gbfleet mcp`: there the PLANNER decides how many to run,
    #: and it holds both servers, so it can read the clusters itself.

    @classmethod
    def of(cls, payload: dict) -> "AllocationRead":
        rationale = str(payload.get("rationale") or "")
        return cls(
            workers=int(payload.get("workers") or 0),
            reviewers=int(payload.get("reviewers") or 0),
            rationale=rationale,
            uninformative=not (payload.get("mapping") or []),
        )


@dataclass
class Partition:
    """What the supervisor knows about being cut off, and what it may promise.

    PRD-22 D-i. The instinct is to keep running offline until claimed work is finished,
    and it cannot: `sign_off` and `bounce` are server acts, and leases expire
    server-side because heartbeats cannot land. Worse, the unbounded version puts **two
    agents on one item** the moment the partition is one-sided — the laptop is offline,
    the server is fine and re-hands the item — which is the collision that clustering
    exists to prevent.

    **`ceiling` is the presence TTL the server reported**, remembered from the last
    successful call, and it is the honest number rather than the one D-i names. D-i says
    "until a worker's LEASE expires the server will not give its item to anyone else" —
    but `lease_seconds` is known at *claim*, by the child, and the supervisor never sees
    it. What the supervisor is given is `presence_ttl_seconds`, on every `fleet_status`,
    and it is the number that actually decides: past its presence TTL an agent reads
    offline and its item leases lapse into the queue. That is the moment the claim dies.

    **`ceiling is None` means we never learned it, which is not "unbounded".** Treating
    an unknown ceiling as no ceiling is the absence-reads-clean defect aimed at the one
    decision this class exists to make, so it stops the children instead.
    """

    ceiling: float | None = None
    #: Monotonic time contact was lost, or None while the server is answering.
    since: float | None = None
    longest: float = 0.0
    reached_ceiling: bool = False
    #: Items an agent held before the partition and no longer holds after it. Reported
    #: rather than acted on: re-submitting a transition for work the server has already
    #: re-handed is exactly the blind replay D-i forbids.
    reclaimed: dict[str, list[str]] = field(default_factory=dict)
    #: What each agent held at the last successful read, and a snapshot of that taken
    #: the moment contact was lost. Two fields rather than one because they answer
    #: different questions: the first is "what is true now", the second is "what was
    #: true going in", and comparing the live value against itself — which an earlier
    #: version did — can only ever report nothing.
    held: dict[str, list[str]] = field(default_factory=dict)
    held_at_cutoff: dict[str, list[str]] | None = None

    @property
    def offline(self) -> bool:
        return self.since is not None

    def describe(self) -> str:
        if self.ceiling is None:
            return "never learned the server's presence TTL, so nothing could be bounded"
        return f"tolerated up to {self.ceiling:.0f}s (one presence TTL); longest gap {self.longest:.0f}s"


@dataclass
class Wave:
    """What one `up` actually did. Every field is something that happened."""

    lock: Acquired | None = None
    before: AllocationRead | None = None
    after: AllocationRead | None = None
    spawned: list[Child] = field(default_factory=list)
    reaped: list[Reaped] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    #: Seats handed in but never redeemed, because the wave stopped early. Named so a
    #: caller does not have to infer it from len(spawned) < len(seats) — an inference
    #: that reads as "nothing went wrong" when the list is short for a bad reason.
    unused_seats: int = 0
    offline: bool = False
    partition: Partition = field(default_factory=Partition)
    #: Files each worker actually changed, by branch. MEASURED, not written back —
    #: `touchpoints` on the item is the PREDICTION, and overwriting it would leave walk
    #: step 17's comparison with one operand. See `touchpoints.py`.
    touched: dict[str, list[str]] = field(default_factory=dict)
    #: Children the roster currently reads `offline` that have NOT been stopped, by agent
    #: id, with how long they have been quiet. Reported rather than acted on: a quiet
    #: child is usually one in a long tool call, and the previous version's mistake was
    #: treating that state as a verdict (GRPH-452). Surfacing it means a wave that ends
    #: with a child quiet for twenty minutes says so, instead of the operator learning it
    #: from a kill that named the wrong cause.
    quiet: dict[str, float] = field(default_factory=dict)
    #: Children that produced no output for longer than `Limits.quiet_after`, and for how
    #: long. LOCAL evidence, unlike `quiet` above, which is the server's view: this one
    #: survives a partition and needs nothing from the vendor. Reported, never acted on —
    #: a child inside one long tool call is legitimately silent, and file writes are
    #: buffered, so silence is weak evidence of anything (GRPH-579).
    silent: dict[str, float] = field(default_factory=dict)
    #: Adapters in this wave that have no debug flag, when debug was asked for. Named so
    #: `--debug` cannot quietly mean "debug for some of them".
    debug_gaps: list[str] = field(default_factory=list)
    #: Why this wave ended. `ok` is true only for the two idle reasons (P30 D6).
    #: Empty until something decides: `up` writes `idle` when the children left and
    #: nothing failed; `until` (D1) writes `idle-with-waits` when only typed human
    #: waits remain.
    reason: str = ""
    #: Child exit 75 — stuck, evidence written, item released. Visible, not a
    #: supervisor failure (P30 D6). Exit 70 is a failure and lives in `failures`.
    give_ups: list[str] = field(default_factory=list)
    #: Salvage branches this wave checked out instead of cutting from HEAD (P30 D9).
    resumed: list[str] = field(default_factory=list)
    #: Resume attempted and abandoned — spawn from HEAD, leftover ref stays listed.
    resume_misses: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only for idle and idle-with-waits (P30 D6).

        A wave that exits 0 with leftover unsigned `review` is a failed run; that
        check is `until`'s (D1). `up` writes `idle` when every child left and the
        supervisor itself did not fail — a completed one-shot, not a leftover
        backlog.
        """
        return self.reason in ("idle", "idle-with-waits")


#: Builds the argv and config for one child. GRPH-449 replaces the caller-supplied
#: version with a per-vendor registry; until then the operator names the command, which
#: is what "selection is explicit, never inferred" asks for anyway.
#: The fourth argument is where this child's vendor debug log should go, or None when
#: debug was not asked for. Positional and required rather than optional-by-duck-typing:
#: a factory that quietly ignored it would produce a fleet running without the debug
#: output its operator believes they turned on.
LaunchFactory = Callable[[Seat, Worktree, Path, "Path | None"], Launch]


def _roster(client: Graphban, partition: Partition) -> dict | None:
    """Read the roster, remembering what the server said about presence.

    Returns None when the server is unreachable. Every supervisor read of `fleet_status`
    goes through here so the ceiling and the moment of last contact are recorded
    wherever the call happens, rather than at one call site somebody remembers.
    """
    try:
        payload = client.fleet_status()
    except ServerUnreachable:
        if partition.since is None:
            partition.since = time.monotonic()
            # Snapshot on the way IN, because this is the last moment the answer is
            # knowable. Taken here rather than in the caller so there is one place that
            # decides what "before the partition" means.
            partition.held_at_cutoff = dict(partition.held)
        return None

    ttl = payload.get("presence_ttl_seconds")
    if isinstance(ttl, (int, float)) and ttl > 0:
        partition.ceiling = float(ttl)

    holdings = _holdings(payload)
    if partition.since is not None:
        partition.longest = max(partition.longest, time.monotonic() - partition.since)
        partition.since = None
        for agent, items in (partition.held_at_cutoff or {}).items():
            lost = [i for i in items if i not in holdings.get(agent, [])]
            if lost:
                # Reported, never replayed. Re-submitting a transition for work the
                # server has already re-handed is exactly the blind replay D-i forbids,
                # and §4 means the supervisor could not submit one anyway.
                partition.reclaimed.setdefault(agent, []).extend(lost)
        partition.held_at_cutoff = None

    partition.held = holdings
    return payload


def _holdings(roster: dict) -> dict[str, list[str]]:
    return {
        a["id"]: [h.get("id") for h in (a.get("holdings") or [])]
        for a in (roster.get("agents") or [])
        if a.get("id")
    }


def _read_allocation(client: Graphban, wave: Wave) -> AllocationRead | None:
    try:
        return AllocationRead.of(client.propose_allocation())
    except ServerUnreachable as exc:
        wave.offline = True
        wave.failures.append(f"server unreachable: {exc}")
        return None


def up(
    repo: Path,
    seats: Sequence[Seat],
    launch_factory: LaunchFactory,
    client: Graphban,
    *,
    wave_name: str = "wave",
    limits: Limits = Limits(),
    state: Path | None = None,
    workspace: Path | None = None,
    poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    debug: bool = False,
    items: dict | None = None,
) -> Wave:
    """Run one wave to completion and return what happened.

    Holds the repo lock for the whole wave (D-h), so a second supervisor on this
    repository refuses to start rather than exceeding `max_workers` between them.
    """
    repo = Path(repo)
    workspace = Path(workspace) if workspace else repo.parent / f"{repo.name}-gbfleet"
    wave = Wave()

    observe.configure(state)

    with hold(repo, state) as acquired:
        wave.lock = acquired
        leftover: list[Child] = []
        occupied: set[str] = set()
        if acquired.takeover:
            # A supervisor died here. Adopt live PIDs and salvage the rest (P30 D7)
            # rather than logging takeover and starting a new wave beside them.
            observe.emit("takeover", detail=acquired.takeover.describe())
            leftover, occupied, notes = adopt_mod.recover(repo, workspace, state)
            for note in notes:
                observe.emit("adopt", detail=note)
            for child in leftover:
                wave.spawned.append(child)
        wave.before = _read_allocation(client, wave)
        if wave.offline:
            # D-i: no new spawns while the server is unreachable. A child that cannot
            # register has no identity, no consumed seat and no claim — spawning one
            # spends money to produce a process nobody can account for.
            wave.unused_seats = len(seats)
            return wave

        cap = max(0, min(limits.max_workers, limits.max_children) - len(leftover))
        wanted = min(len(seats), cap)
        wave.unused_seats = len(seats) - wanted

        children = leftover + list(_start(
            wave, seats[:wanted], launch_factory, repo, workspace, wave_name, client,
            limits, debug=debug, occupied=occupied, items=items,
        ))
        roster_path = adopt_mod.children_path(repo, state)

        def persist() -> None:
            adopt_mod.persist(roster_path, children)

        persist()
        _wait_out(wave, children, limits, client, poll=poll, sleep=sleep, debug=debug,
                  persist=persist)
        _reap_all(wave, children)
        persist()

        wave.after = _read_allocation(client, wave)
        if not wave.failures and not wave.offline:
            wave.reason = "idle"

    return wave


def _tree_for(repo: Path, workspace: Path, wave_name: str, slot: str) -> Worktree:
    """One worktree on its own branch.

    D-g names the branch `gb/<wave>-<agent-short-id>`, and the agent id does not exist
    yet: it is minted server-side at `register_agent`, which cannot happen until the
    child is running, which cannot happen until it has a worktree on a branch. The slot
    stands in — deterministic, collision-free within a wave, and the roster ties agent to
    worktree once the child registers.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    return wt_mod.create(repo, workspace / f"{wave_name}-{slot}", wave_name, slot)


def start_one(
    tree: Worktree,
    seat: Seat,
    launch_factory: LaunchFactory,
    client: Graphban,
    limits: Limits,
    partition: Partition,
    *,
    workspace: Path,
    wave_name: str,
    slot: str,
    on_spawned: Callable[[Child], None] | None = None,
    debug_file: Path | None = None,
) -> Child:
    """Launch one child into one worktree and wait for it to appear on the roster.

    Shared by the wave loop and the stdio `spawn` tool, so the two cannot drift: a
    planner spawning a child by hand must get the same seat handling, the same
    registration window and the same failure text as one the deterministic loop starts.

    **`on_spawned` fires the moment the process exists, before registration is awaited**,
    and that ordering is the point. A child that never registers still ran, still spent
    money, and still needs reaping and a record — so the caller has to know about it
    before the wait that may raise. Folding the two steps into one function without this
    dropped exactly that child out of the wave, and the only symptom was one fewer line
    in a log nobody was reading yet.
    """
    launch = launch_factory(seat, tree, _instruction_file(tree, seat, wave_name), debug_file)
    child = spawn(
        launch, tree.path, tree.branch, _logs(workspace, f"{wave_name}-{slot}"), base=tree.base
    )
    if on_spawned is not None:
        on_spawned(child)
    # Through `_roster`, not straight to the client: this is where the partition ceiling
    # is first learned, and routing around it left it None for the whole wave — so the
    # very first missed poll read as "no ceiling known" and stopped every child.
    #
    # An empty roster while unreachable means the registration window still applies, and
    # a partition lasting the whole window is recorded as never-registered rather than
    # lease-lapsed. Slightly the wrong word for the right outcome: a child with no
    # identity has no claim either way (D-i).
    await_registration(
        child,
        lambda: _roster(client, partition) or {},
        window=limits.registration_window,
    )
    return child


def _start(
    wave: Wave,
    seats: Sequence[Seat],
    launch_factory: LaunchFactory,
    repo: Path,
    workspace: Path,
    wave_name: str,
    client: Graphban,
    limits: Limits,
    debug: bool = False,
    occupied: set[str] | None = None,
    items: dict | None = None,
) -> Iterable[Child]:
    """Create a worktree per seat, spawn into it, and wait for it to register.

    **A launch failure stops the wave rather than continuing down the list.** The
    failures S2 describes are adapter-shaped — a missing binary, a version mismatch, a
    child that never registers — and they are identical for every seat. Spawning three
    more children into three more worktrees to watch them fail the same way costs three
    more salvage branches and tells nobody anything new.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    started: list[Child] = []
    planned = len(seats)
    taken = set(occupied or ())
    slot_n = 1
    resumes = list(wt_mod.choose_resume(wt_mod.orphans(repo), items or {}))

    for index, seat in enumerate(seats):
        tree: Worktree | None = None
        slot = "1"
        agent_slot = f"{wave_name}-{slot}"
        try:
            if resumes:
                orphan = resumes.pop(0)
                slot = orphan.branch.rsplit("-", 1)[-1] or slot
                agent_slot = f"{wave_name}-{slot}"
                try:
                    tree = wt_mod.resume(
                        repo, workspace / f"{wave_name}-{slot}-resume", orphan,
                    )
                    wave.resumed.append(orphan.branch)
                    taken.add(orphan.branch)
                except wt_mod.ResumeFailed as exc:
                    # Do not abort. Spawn from HEAD; leave the leftover ref listed.
                    wave.resume_misses.append(str(exc))
                    observe.emit("resume_miss", detail=str(exc), branch=orphan.branch)
            while tree is None:
                slot = str(slot_n)
                slot_n += 1
                agent_slot = f"{wave_name}-{slot}"
                branch = wt_mod.branch_name(wave_name, slot)
                if branch in taken or wt_mod.branch_exists(repo, branch):
                    taken.add(branch)
                    if slot_n > 1000:
                        raise wt_mod.BranchExists(
                            f"no free gb/{wave_name}-* slot under 1000"
                        )
                    continue
                try:
                    tree = _tree_for(repo, workspace, wave_name, slot)
                except wt_mod.BranchExists:
                    taken.add(branch)
                    continue
                break
            def remember(child: Child) -> None:
                started.append(child)
                wave.spawned.append(child)
                # Asked for, and the adapter had no flag for it. Said here, once, per
                # child — the alternative is an operator reading a quiet log and
                # concluding the child is fine when nothing was ever going to be written.
                if debug and child.debug_path is None:
                    note = f"{child.adapter}: no debug flag; output sampling only"
                    if note not in wave.debug_gaps:
                        wave.debug_gaps.append(note)
                    observe.debug_gap(child.adapter, "vendor CLI has no debug flag")

            # Outside the worktree, so a vendor writing megabytes of debug output cannot
            # dirty the tree, trip salvage, or end up in a WIP commit.
            debug_file = (
                _logs(workspace, f"{wave_name}-{slot}") / "debug.log" if debug else None
            )
            start_one(
                tree, seat, launch_factory, client, limits, wave.partition,
                workspace=workspace, wave_name=wave_name, slot=slot,
                on_spawned=remember, debug_file=debug_file,
            )
        except (LaunchFailed, wt_mod.GitError, wt_mod.BranchExists) as exc:
            wave.failures.append(f"{agent_slot}: {exc}")
            if tree is not None and tree.path.exists() and not any(
                c.worktree == tree.path for c in started
            ):
                # Nothing ever ran in it, so there is nothing to salvage — but reap it
                # rather than unlinking, so the one code path that removes worktrees
                # stays the one that knows about seat files.
                wave.reaped.append(wt_mod.reap(tree))
            # The one that failed AND every one after it. Counting only the failure
            # leaves the caller to work the rest out from len(spawned), which is the
            # inference this field exists to remove — and a short list reads as
            # "nothing went wrong".
            wave.unused_seats += planned - index
            break

    return started


def _instruction_file(tree: Worktree, seat: Seat, wave_name: str) -> Path:
    """The child's marching orders, on disk rather than on the command line.

    The instruction carries the enrolment code, and argv is visible to every process on
    the machine via `ps`. D-k is clear that none of this is a security boundary, but
    putting a live credential somewhere every other user can read it is a different
    thing from declining to sandbox.
    """
    path = tree.path / ".gbfleet-instruction"
    path.write_text(instruction_for(seat, tree.path, tree.branch), encoding="utf-8")
    # This file carries the enrolment CODE, so it is the more sensitive of the two and
    # was protected by the same call that does nothing on Windows.
    if not restrict_to_owner(path):
        observe.emit(
            "credential_unrestricted",
            path=str(path),
            what="enrolment code",
            detail=(
                "could not restrict the instruction file to this user; it carries a "
                "live enrolment code and may be readable by others on this host"
            ),
        )
    return path


def _logs(workspace: Path, slot: str) -> Path:
    return workspace / "logs" / slot


def watch_tick(
    wave: Wave,
    children: list[Child],
    limits: Limits,
    client: Graphban,
    *,
    debug: bool = False,
    persist: Callable[[], None] | None = None,
) -> None:
    """One pass of the watch loop: wall-clock, output pulse, lease, disowned.

    Shared by `up`'s `_wait_out` and `gbfleet mcp` (P30 D6). Silence is a report, not
    a kill — a long `run_tests` is legitimate quiet. The kill conditions stay the
    PRD-22 four (wall-clock, never-registered, seat-gone, lease-lapsed), plus the
    planner's `stop`.
    """
    roster = _roster(client, wave.partition)

    for child in children:
        if not child.running:
            continue
        if time.monotonic() - child.started_at > limits.child_wall_clock:
            stop(child, Reason.WALL_CLOCK)
            wave.failures.append(
                f"{child.adapter} pid {child.pid}: over {limits.child_wall_clock:.0f}s, stopped"
            )

    _watch_output(wave, children, limits, debug=debug)
    _enforce_the_lease(wave, children)
    if roster is not None:
        _catch_the_disowned(wave, children, roster, limits)
    if persist is not None:
        persist()


def _wait_out(
    wave: Wave,
    children: list[Child],
    limits: Limits,
    client: Graphban,
    *,
    poll: float,
    sleep: Callable[[float], None],
    debug: bool = False,
    persist: Callable[[], None] | None = None,
) -> None:
    """Wait for children to exit on their own, stopping any that overrun or outlive their claim.

    Exiting is the normal end of a worker's life (D-c): it claims with
    `wait_seconds=0`, works what it got, and leaves when there is nothing. The
    supervisor does not tell it to stop being idle — `fleet_idle` is deliberately not a
    kill reason, because two things owning one transition is how they come to disagree.

    The partition handling is D-i, and the ceiling is one presence TTL. A child that
    cannot reach the server may keep building until its own deadline and not past it:
    that is not optimism, it is what the lease promises. Past it, the server has given
    the item to somebody else and a second agent is already working it.
    """
    while any(child.running for child in children):
        watch_tick(wave, children, limits, client, debug=debug, persist=persist)
        if any(child.running for child in children):
            sleep(poll)

    # One last read, so a partition that ended just as the children did is still
    # reconciled rather than left as the last thing that happened.
    _roster(client, wave.partition)


def _child_key(child: Child) -> str:
    """How a child is named in the live reports.

    The agent id once it has one, because that is what the roster and the tracker use.
    Before that it has no server identity at all — and a child that never registers is
    exactly the case worth reporting, so falling back to adapter and pid keeps it
    nameable instead of dropping it out of the report for want of a key.
    """
    return child.agent_id or f"{child.adapter}:{child.pid}"


def _watch_output(wave: Wave, children: list[Child], limits: Limits, *,
                  debug: bool) -> None:
    """Read how much each live child has written, and remember who has gone quiet.

    **Measured always, printed only under debug.** The measurement is one `stat` per log
    file per poll, and it is what lets the wave summary name a child that produced
    nothing for twenty minutes. Printing a line per child per second for an hour would
    bury the lines that matter inside the file meant to carry them.

    Nothing here stops anything. Silence is weak evidence: a child inside a single long
    tool call is legitimately silent — `gbagent`'s `run_tests` timeout alone is 1800s —
    and writes are buffered, so output arrives in bursts with real gaps between them.
    Acting on it would put a second owner on a transition `_catch_the_disowned` already
    owns, which is the mistake `fleet_idle` is deliberately not repeating.
    """
    now = time.monotonic()
    for child in children:
        if not child.running or child.output is None:
            continue
        reading = child.output.sample(now)
        key = _child_key(child)

        if debug:
            observe.pulse(
                key, child.adapter, child.pid,
                debug_log=str(child.debug_path) if child.debug_path else None,
                **reading.as_dict(),
            )

        silent = reading.silent_for
        if isinstance(silent, str):
            # Never wrote anything at all. Reported from the moment it passes the
            # threshold, using its age: "quiet for 400s" and "has not made a sound since
            # it started 400s ago" are different findings, and the second is worse.
            if reading.age >= limits.quiet_after:
                wave.silent[key] = reading.age
            continue
        if silent >= limits.quiet_after:
            wave.silent[key] = silent
        else:
            # Spoke again. Drop it, or the wave ends reporting a child as quiet that has
            # been talking for the last ten minutes — the same correction
            # `_catch_the_disowned` makes for `quiet`.
            wave.silent.pop(key, None)


def _catch_the_disowned(wave: Wave, children: list[Child], roster: dict,
                        limits: Limits) -> None:
    """Stop children the server has stopped counting. PRD-22 D-d, the backstop half.

    `end_wave` and `retire_wave` revoke a seat while a child is still building, and there
    is no push channel — §D-e, unchanged — so the child discovers it only on its next
    server call, which a child deep in a build may not make for a long time.

    **The planner is the primary path and it already exists**: it polls Graphban, sees a
    seat revoked, and calls `stop` on the local surface (GRPH-450). This is the backstop,
    because a planner that is idle, dead, or mid-turn notifies nobody — and "end wave is a
    hard stop" is only true if something is watching. Two paths to the same transition is
    fine here precisely because `stop` is idempotent; two paths to *deciding* it would not
    be.

    **What is actually observable, and the two observations are NOT equally strong.**
    The supervisor is not the minter, so it cannot read seat state — `fleet_status`
    returns the seats you minted, and these were minted by the planner. It sees the
    roster, and the roster says one of two things:

    - **The agent is gone from it.** Unambiguous. The server is answering and does not
      list this id, so it has been dismissed. Stopped at once.
    - **The agent reads `offline`.** Ambiguous, and this is the correction in GRPH-452.
      `offline` is derived purely from `last_seen_at`, which only `heartbeat` refreshes —
      so it means *no heartbeat within the presence TTL* and nothing more. A revoked seat
      looks like that. So does a child whose MCP client died. **So does a perfectly
      healthy child that is simply busy**, because a blocking tool call makes no server
      calls: the presence TTL is one quarter of the lease (150s by default) and one run
      of this repository's own backend suite is ~9 minutes of silence. Treating the first
      `offline` reading as proof would stop a child for running the tests it was spawned
      to run, and file it as disowned.

    So `offline` has to be SUSTAINED past `limits.disowned_after` before it is acted on,
    and the failure says how long it was quiet rather than asserting a cause. Getting
    this wrong in the safe direction costs a revoked child some extra minutes of spend;
    getting it wrong in the other direction destroys work and misattributes it. Those are
    not symmetric, which is why the bound is generous and configurable rather than clever.

    Only called when the roster was actually READ. During a partition every agent looks
    absent, and killing the fleet because the network dropped is D-i's job to prevent, not
    this one's to cause.
    """
    live = {
        a.get("id"): a for a in (roster.get("agents") or []) if a.get("id")
    }
    now = time.monotonic()
    for child in children:
        if not child.running or not child.agent_id:
            continue
        row = live.get(child.agent_id)

        if row is not None and row.get("state") != "offline":
            # It came back, or never left. Forget any quiet spell so a child that goes
            # quiet twice is not stopped on the sum of two unrelated silences — and drop
            # it from the report too, or the wave ends claiming a child is quiet that has
            # been heartbeating for twenty minutes.
            child.offline_since = None
            wave.quiet.pop(child.agent_id, None)
            continue

        if row is None:
            why = "the server no longer lists this agent"
        else:
            if child.offline_since is None:
                child.offline_since = now
                wave.quiet[child.agent_id] = 0.0
                continue
            quiet = now - child.offline_since
            wave.quiet[child.agent_id] = quiet
            if quiet < limits.disowned_after:
                continue
            why = (
                f"no heartbeat reached the server for {quiet:.0f}s, past the "
                f"{limits.disowned_after:.0f}s allowed. Its claim is gone or its client "
                "is dead; the supervisor cannot tell which, and cannot rule out a very "
                "long tool call either"
            )

        stop(child, Reason.SEAT_GONE)
        wave.quiet.pop(child.agent_id, None)
        wave.failures.append(f"{child.adapter} pid {child.pid} ({child.agent_id}): {why}")


def _enforce_the_lease(wave: Wave, children: list[Child]) -> None:
    """Stop children that have been cut off for longer than one presence TTL.

    **Worktree and branch are left intact.** The work survives; the claim does not —
    reaping happens afterwards and salvages it onto the child's own branch.
    """
    partition = wave.partition
    if not partition.offline:
        return

    elapsed = time.monotonic() - (partition.since or 0.0)
    partition.longest = max(partition.longest, elapsed)

    if partition.ceiling is None:
        reason = (
            "server unreachable and its presence TTL was never learned, so no partition "
            "could be bounded — stopping rather than guessing a ceiling"
        )
    elif elapsed >= partition.ceiling:
        partition.reached_ceiling = True
        reason = (
            f"server unreachable for {elapsed:.0f}s, past the {partition.ceiling:.0f}s "
            "presence TTL — the server has requeued this work and a second agent may "
            "already hold it"
        )
    else:
        return

    for child in children:
        if child.running:
            stop(child, Reason.LEASE_LAPSED)
            wave.failures.append(f"{child.adapter} pid {child.pid}: {reason}")


def _reap_all(wave: Wave, children: list[Child]) -> None:
    """Reap each worktree, then take away any seat that was never inside one.

    `worktree.reap` removes the seat files it knows about — the ones a vendor forced
    into the project directory. Claude Code takes `--mcp-config`, so its seat lives in a
    private temp file OUTSIDE the tree, and reaping the worktree does not touch it.

    Walk step 8 says the child's seat file is gone after reap, with no exception for the
    vendors that were tidy about where it went. Without this, the vendor that handled
    credentials BEST is the one that leaves one behind.
    """
    for child in children:
        tree = Worktree(
            path=child.worktree, branch=child.branch, repo=_repo_of(child), base=child.base
        )
        held = [i for i in (wave.partition.held.get(child.agent_id) or []) if i]
        reaped = wt_mod.reap(
            tree, message=wt_mod.salvage_message(child.adapter, held),
        )
        wave.reaped.append(reaped)

        # AFTER the reap, deliberately: salvage has just committed whatever the worker
        # left uncommitted, so the branch now holds the whole of what it did. Measuring
        # before would miss exactly the work that was most at risk.
        try:
            wave.touched[child.branch] = tp_mod.measure(tree)
        except ValueError as exc:
            # Not silently empty. "We could not measure" and "it changed nothing" are
            # different answers and only one of them is reassuring.
            wave.failures.append(f"{child.branch}: {exc}")
        if not _inside(child.seat_path, child.worktree):
            seat_mod.remove(child.seat_path)

        code = child.process.returncode
        # gbagent.loop: 70 = handoff could not be written (item still claimed);
        # 75 = stuck, evidence written, item released. P30 D6: 70 is a supervisor
        # failure; 75 is a completed give-up, visible, not a failure by itself.
        if code == 70:
            wave.failures.append(
                f"{child.adapter} pid {child.pid}: handoff-failed (exit 70); "
                "the item is still claimed"
            )
        elif code == 75:
            wave.give_ups.append(
                f"{child.adapter} pid {child.pid}: stuck (exit 75); item released, "
                "worktree salvaged"
            )

        observe.child(ChildRecord(
            adapter=child.adapter,
            binary_version=child.binary_version,
            worktree=str(child.worktree),
            branch=child.branch,
            pid=child.pid,
            seat_id=child.seat_id,
            agent_id=child.agent_id,
            # None means it never registered, and that is the whole point of the field:
            # a process that ran, spent money and produced nothing, while the roster
            # showed one agent fewer. Omitting it would read as nothing to report;
            # zeroing it would read as instant.
            registration_latency=(
                child.registration_latency
                if child.registration_latency is not None
                else NEVER_REGISTERED
            ),
            exit_code=child.process.returncode,
            stopped_because=child.stopped_because.value if child.stopped_because else None,
            reap=reaped.disposition.value,
            salvage_commit=reaped.salvage.commit if reaped.salvage else None,
            credential_in_history=(
                list(reaped.salvage.credential_in_history) if reaped.salvage else []
            ),
            touched=wave.touched.get(child.branch, []),
        ))


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def _repo_of(child: Child) -> Path:
    from .state import repo_root

    return repo_root(child.worktree)
