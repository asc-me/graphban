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
from .seat import Seat, instruction_for
from .spawn import Child, Launch, LaunchFailed, Reason, await_registration, spawn, stop
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

    @property
    def ok(self) -> bool:
        return not self.failures and not self.offline


#: Builds the argv and config for one child. GRPH-449 replaces the caller-supplied
#: version with a per-vendor registry; until then the operator names the command, which
#: is what "selection is explicit, never inferred" asks for anyway.
LaunchFactory = Callable[[Seat, Worktree, Path], Launch]


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

    with hold(repo, state) as acquired:
        wave.lock = acquired
        wave.before = _read_allocation(client, wave)
        if wave.offline:
            # D-i: no new spawns while the server is unreachable. A child that cannot
            # register has no identity, no consumed seat and no claim — spawning one
            # spends money to produce a process nobody can account for.
            wave.unused_seats = len(seats)
            return wave

        children = list(_start(wave, seats[:wanted], launch_factory, repo, workspace, wave_name, client))
        _wait_out(wave, children, limits, poll=poll, sleep=sleep)
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
            child = spawn(launch, tree.path, tree.branch, _logs(workspace, agent_slot))
            started.append(child)
            wave.spawned.append(child)
            await_registration(child, client.fleet_status)
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
    *,
    poll: float,
    sleep: Callable[[float], None],
) -> None:
    """Wait for children to exit on their own, stopping any that run too long.

    Exiting is the normal end of a worker's life (D-c): it claims with
    `wait_seconds=0`, works what it got, and leaves when there is nothing. The
    supervisor does not tell it to stop being idle — `fleet_idle` is deliberately not a
    kill reason, because two things owning one transition is how they come to disagree.
    """
    while any(child.running for child in children):
        for child in children:
            if not child.running:
                continue
            if time.monotonic() - child.started_at > limits.child_wall_clock:
                stop(child, Reason.WALL_CLOCK)
                wave.failures.append(
                    f"{child.adapter} pid {child.pid}: over {limits.child_wall_clock:.0f}s, stopped"
                )
        if any(child.running for child in children):
            sleep(poll)


def _reap_all(wave: Wave, children: list[Child]) -> None:
    for child in children:
        wave.reaped.append(
            wt_mod.reap(
                Worktree(path=child.worktree, branch=child.branch, repo=_repo_of(child)),
                message=f"WIP: salvaged by gbfleet ({child.adapter})",
            )
        )


def _repo_of(child: Child) -> Path:
    from .state import repo_root

    return repo_root(child.worktree)
