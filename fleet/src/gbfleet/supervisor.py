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

from . import worktree as wt_mod
from .client import Graphban, ServerUnreachable
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

    @property
    def ok(self) -> bool:
        return not self.failures and not self.offline


#: Builds the argv and config for one child. GRPH-449 replaces the caller-supplied
#: version with a per-vendor registry; until then the operator names the command, which
#: is what "selection is explicit, never inferred" asks for anyway.
LaunchFactory = Callable[[Seat, Worktree, Path], Launch]


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
) -> Wave:
    """Run one wave to completion and return what happened.

    Holds the repo lock for the whole wave (D-h), so a second supervisor on this
    repository refuses to start rather than exceeding `max_workers` between them.
    """
    repo = Path(repo)
    workspace = Path(workspace) if workspace else repo.parent / f"{repo.name}-gbfleet"
    wave = Wave()

    wanted = min(len(seats), limits.max_workers, limits.max_children)
    wave.unused_seats = len(seats) - wanted

    observe.configure(state)

    with hold(repo, state) as acquired:
        wave.lock = acquired
        if acquired.takeover:
            # A supervisor died here. Said before anything else happens, because
            # everything after it may be running beside children nobody is watching.
            observe.emit("takeover", detail=acquired.takeover.describe())
        wave.before = _read_allocation(client, wave)
        if wave.offline:
            # D-i: no new spawns while the server is unreachable. A child that cannot
            # register has no identity, no consumed seat and no claim — spawning one
            # spends money to produce a process nobody can account for.
            wave.unused_seats = len(seats)
            return wave

        children = list(_start(
            wave, seats[:wanted], launch_factory, repo, workspace, wave_name, client, limits
        ))
        _wait_out(wave, children, limits, client, poll=poll, sleep=sleep)
        _reap_all(wave, children)

        wave.after = _read_allocation(client, wave)

    return wave


def _start(
    wave: Wave,
    seats: Sequence[Seat],
    launch_factory: LaunchFactory,
    repo: Path,
    workspace: Path,
    wave_name: str,
    client: Graphban,
    limits: Limits,
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

    for index, seat in enumerate(seats):
        # D-g names the branch `gb/<wave>-<agent-short-id>`, and the agent id does not
        # exist yet: it is minted server-side at `register_agent`, which cannot happen
        # until the child is running, which cannot happen until it has a worktree on a
        # branch. The slot index stands in — still deterministic, still collision-free
        # within a wave, and the roster ties agent to worktree once the child registers.
        slot = str(index + 1)
        agent_slot = f"{wave_name}-{slot}"
        tree: Worktree | None = None
        try:
            tree = wt_mod.create(repo, workspace / agent_slot, wave_name, slot)
            launch = launch_factory(seat, tree, _instruction_file(tree, seat, wave_name))
            child = spawn(
                launch, tree.path, tree.branch, _logs(workspace, agent_slot), base=tree.base
            )
            started.append(child)
            wave.spawned.append(child)
            # Through `_roster`, not straight to the client: this is where the ceiling
            # is first learned, and routing around it left `partition.ceiling` None for
            # the whole wave — so the very first missed poll read as "no ceiling known"
            # and stopped every child. `_roster`'s docstring said every read went
            # through it; this is what makes that true.
            #
            # An empty roster while unreachable means the registration window still
            # applies, and a partition lasting the whole window is recorded as
            # never-registered rather than lease-lapsed. Slightly the wrong word for the
            # right outcome: a child with no identity has no claim either way (D-i).
            await_registration(
                child,
                lambda: _roster(client, wave.partition) or {},
                window=limits.registration_window,
            )
        except (LaunchFailed, wt_mod.BranchExists, wt_mod.GitError) as exc:
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
    path.chmod(0o600)
    return path


def _logs(workspace: Path, slot: str) -> Path:
    return workspace / "logs" / slot


def _wait_out(
    wave: Wave,
    children: list[Child],
    limits: Limits,
    client: Graphban,
    *,
    poll: float,
    sleep: Callable[[float], None],
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
        _roster(client, wave.partition)

        for child in children:
            if not child.running:
                continue
            if time.monotonic() - child.started_at > limits.child_wall_clock:
                stop(child, Reason.WALL_CLOCK)
                wave.failures.append(
                    f"{child.adapter} pid {child.pid}: over {limits.child_wall_clock:.0f}s, stopped"
                )

        _enforce_the_lease(wave, children)

        if any(child.running for child in children):
            sleep(poll)

    # One last read, so a partition that ended just as the children did is still
    # reconciled rather than left as the last thing that happened.
    _roster(client, wave.partition)


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
        reaped = wt_mod.reap(tree, message=f"WIP: salvaged by gbfleet ({child.adapter})")
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
