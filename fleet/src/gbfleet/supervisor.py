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
from . import adapters
from .adapters import explain_exit
from .client import Graphban, NotPermitted, ServerUnreachable, ToolFailed
from . import headroom as headroom_mod
from gbagent.config import SetupFailed, prepare
from .headroom import Headroom
from .hostos import restrict_to_owner
from .lock import Acquired, hold
from . import observe
from . import seat as seat_mod
from . import propose as propose_mod
from . import shim as shim_mod
from . import touchpoints as tp_mod
from .observe import NEVER_REGISTERED, ChildRecord
from .seat import Seat, instruction_for
from .spawn import (
    REGISTRATION_WINDOW, Child, Launch, LaunchFailed, Reason, VendorLimit,
    await_registration,
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
    #: Bytes charged per child by the memory gate (GRPH-842). The one limit here that is
    #: about the MACHINE rather than the wave, and it belongs beside the others for the
    #: reason the docstring gives: unlike spend, memory is something this process can
    #: actually measure. See `headroom.py` for where the number comes from.
    child_memory: int = headroom_mod.DEFAULT_CHILD_MEMORY
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
    #: What `hostos.available_memory` read when the launch loop began (GRPH-842), or
    #: None where the host cannot be asked. Reported beside `gated` because an empty
    #: `gated` has two meanings and they are opposites: nothing was refused, or nothing
    #: could be measured and the gate never bound. Same shape as `stale_unmeasured`.
    headroom_at_start: int | None = None
    #: Slots the memory gate refused, with the reading that refused them (GRPH-842).
    #: Separate from `failures` on purpose: nothing went wrong, the machine was full, and
    #: filing it as a failure would send an operator looking for a broken adapter. Also
    #: separate from `unused_seats`, which counts them but cannot say why.
    gated: list[str] = field(default_factory=list)
    offline: bool = False
    partition: Partition = field(default_factory=Partition)
    #: What reached the remote, by branch (GRPH-750). A branch that did not is named here
    #: with its reason: the reviewer reads the BRANCH, so a push that failed silently would
    #: leave them inferring an empty diff.
    published: dict = field(default_factory=dict)
    #: Draft PRs opened for reaped branches, by branch (GRPH-804). A branch that was
    #: published and NOT proposed is work nobody has been asked to merge, which is the
    #: state this exists to make visible rather than to hide.
    proposed: dict = field(default_factory=dict)
    #: What became of finishing each signed-off item's merge, by item id (GRPH-846). Only
    #: populated under `--merge`; empty otherwise means "not asked", never "nothing to
    #: merge". `Merged.final` says whether the row is settled or still being re-asked.
    merged: dict = field(default_factory=dict)
    #: Files each worker actually changed, by branch. MEASURED here, written back by a
    #: holder with standing (`gbfleet.record.measured`) — the supervisor still cannot
    #: call `update_item`. Empty is reported and is not a write. See `touchpoints.py`.
    touched: dict[str, list[str]] = field(default_factory=dict)
    #: Measured paths that no DECLARED touchpoint covers, by branch (GRPH-785). The
    #: partition that keeps two workers off the same file is computed from `touchpoints`;
    #: a worker changing a file nobody declared means the partition's input was wrong, and
    #: nothing noticed before, because the server unions measured paths into the
    #: declaration and the two stop being distinguishable the moment they are stored.
    #: REPORTED, never acted on: the supervisor holds two read tools and no authority to
    #: decide what collides.
    undeclared: dict[str, list[str]] = field(default_factory=dict)
    #: id -> declared touchpoints, as they stood when work was handed out.
    declared: dict[str, list[str]] = field(default_factory=dict)
    #: What each finished child's own result record said the run cost (GRPH-834), by branch:
    #: `{tokens_in, tokens_out, turns_used, adapter}`. Read from the same vendor record
    #: `_report_exits` already posts to the ledger — the numbers existed and reached the
    #: server, and the wave summary was the one place that never saw them.
    #:
    #: A child ABSENT from this dict said nothing, which is not the same as saying zero. The
    #: summary reports the two separately for that reason: "412k tokens" reads as the wave's
    #: total, and it is not the total if four of six children were never counted.
    spend: dict[str, dict] = field(default_factory=dict)
    #: branch -> (commits behind, the ref it was measured against). How much had landed on
    #: the trunk that this worker never had in front of it (GRPH-786). A reviewer reading a
    #: branch cut from a base the trunk has moved past is reading a diff against a world
    #: that no longer exists, and nothing said so.
    stale: dict[str, tuple] = field(default_factory=dict)
    #: Set when the trunk could not be fetched. "We could not ask" is not "nothing moved",
    #: and reporting the second for the first is the failure this whole check is about.
    stale_unmeasured: str = ""
    #: PRD-41 D21: each matrix resolution this wave made, with every stage. `until`
    #: prints the list on its report (empty = looked, none resolved — not an absence).
    resolutions: list = field(default_factory=list)
    #: Files changed on more than one branch in this wave — path -> branches. The check
    #: that needs no declaration to be right: two workers changed the same file, observed
    #: rather than predicted. If touchpoints were wrong this still fires; if they were
    #: right and the divvy was wrong this still fires.
    collided: dict[str, list[str]] = field(default_factory=dict)
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


def _remember_holdings(children: Sequence[Child], partition: Partition) -> None:
    """Keep the last non-empty holdings per child. Live roster after release is empty."""
    for child in children:
        if not child.agent_id:
            continue
        items = [i for i in (partition.held.get(child.agent_id) or []) if i]
        if items:
            child.held_items = list(items)


def item_status(client: Graphban) -> dict[str, dict]:
    """id -> {status, claimed_by, claimed_at} for `choose_resume`. Empty if this client may not read items.

    D9 bounce: CLI `up` and MCP `spawn` must resume without the caller injecting `items=`.
    `search_items` is a read; ALLOWED_TOOLS stays two. Callers that permit this extra
    read get resume; a pure supervisor client gets {}.

    `claimed_at` is included so `choose_resume` can distinguish a live lease from a stale
    one (GRPH-850): an `in_progress` item whose holder stopped heartbeating is eligible
    for resume even though `claimed_by` is still set.
    """
    try:
        payload = client.call("search_items", fields="full", limit=10_000)
    except (NotPermitted, ToolFailed, ServerUnreachable):
        return {}
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        out[str(row["id"])] = {
            "status": row.get("status") or "",
            "claimed_by": row.get("claimed_by") or "",
            # GRPH-850: `choose_resume` uses this to decide whether the lease is stale.
            # `item_dict` emits unix seconds (or "" when nobody holds).
            "claimed_at": row.get("claimed_at") or "",
            # The DECLARATION the partition was computed from (GRPH-785). Kept here rather
            # than re-read at reap on purpose: the question is whether the input to the
            # divvy was right, and that input is this snapshot, not whatever the item says
            # after the worker has been editing it.
            "touchpoints": list(row.get("touchpoints") or []),
        }
    return out


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


def _rooted(repo: Path | str, workspace: Path | str | None) -> tuple[Path, Path]:
    """The tree children are cut from, and the workspace they are cut into.

    **Two different questions, and only one of them wants the main working tree** (GRPH-784).

    The WORKSPACE is per-repository, because the lock is: `hold()` resolves through
    `repo_root` internally, so a workspace that forked per worktree would put children where
    the next supervisor does not look while both hold the same lock. It is also why this is
    one function for `up` and `until` — `mcp` already resolved and those two did not, and
    nothing compared the three.

    The REPO is the tree the operator launched in, resolved and nothing more. `--repo`
    defaults to `.`, and `Path(".").name` is the EMPTY STRING, so deriving the workspace from
    it produced `-gbfleet` and `git worktree add` read the leading dash as a switch — that is
    the bug this function was added for, and it is fixed by resolving the workspace's root,
    not by moving the repo.

    Sweeping `repo` to the main tree as well moved the BASE COMMIT every child is cut from,
    silently: `worktree.create` runs `rev-parse HEAD` with `cwd=repo`, so a wave launched
    from a linked worktree would have built its children on the main tree's HEAD instead of
    the branch the operator was standing on. On a machine whose main checkout is detached and
    behind, that is every child built on a stale commit, with no error — it surfaces as
    reviewers reading diffs against the wrong base. Caught in review of the fix above.
    """
    from .state import repo_root

    launched_in = Path(repo).resolve()
    root = repo_root(launched_in)
    return launched_in, (Path(workspace) if workspace
                         else root.parent / f"{root.name}-gbfleet")


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
    merger: "Merger | None" = None,
    base: str = "",
) -> Wave:
    """Run one wave to completion and return what happened.

    Holds the repo lock for the whole wave (D-h), so a second supervisor on this
    repository refuses to start rather than exceeding `max_workers` between them.
    """
    repo, workspace = _rooted(repo, workspace)
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
            recovered = adopt_mod.recover(repo, workspace, state)
            leftover, occupied, notes = recovered
            for note in notes:
                observe.emit("adopt", detail=note)
            for child in leftover:
                wave.spawned.append(child)
            # GRPH-830: both takeover paths publish, not just `until`'s. A salvage that
            # depends on which command happened to run next is a salvage the operator cannot
            # rely on.
            publish_salvaged(wave, repo, recovered.salvaged, client=client)
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

        children: list[Child] = list(leftover)
        roster_path = adopt_mod.children_path(repo, state)

        def persist() -> None:
            adopt_mod.persist(roster_path, children)

        # Before `_start`, and again on every spawn (P30 D7 bounce). Waiting until
        # the whole start loop returns means a crash in `await_registration` leaves
        # a live pid with no JSON record.
        persist()
        # Built HERE rather than inside `_start`, so its baseline reading is taken with the
        # adopted children already resident: a takeover that inherited three workers must
        # not read the machine as though it were empty.
        room = Headroom(limits.child_memory)
        wave.headroom_at_start = room.baseline
        if items is None:
            items = item_status(client)
        # The declaration snapshot the divvy used, kept for the reap-time comparison.
        _declared_into(wave, items)
        _start(
            wave, seats[:wanted], launch_factory, repo, workspace, wave_name, client,
            limits, debug=debug, occupied=occupied, items=items,
            into=children, persist=persist,
            room=room, base=base,
        )
        persist()
        _wait_out(wave, children, limits, client, poll=poll, sleep=sleep, debug=debug,
                  persist=persist, merger=merger)
        _reap_all(wave, children)
        persist()
        if merger is not None:
            # Once more after the reap: the last child's sign-off may have landed between
            # the final tick and its exit, and a wave that ends there would leave the one
            # merge it was asked for to the next run.
            merger.tick(wave)

        wave.after = _read_allocation(client, wave)
        if not wave.failures and not wave.offline:
            wave.reason = "idle"

    return wave


def _tree_for(repo: Path, workspace: Path, wave_name: str, slot: str,
              base: str = "") -> Worktree:
    """One worktree on its own branch.

    D-g names the branch `gb/<wave>-<agent-short-id>`, and the agent id does not exist
    yet: it is minted server-side at `register_agent`, which cannot happen until the
    child is running, which cannot happen until it has a worktree on a branch. The slot
    stands in — deterministic, collision-free within a wave, and the roster ties agent to
    worktree once the child registers.

    `base` (GRPH-847) is the ref the worktree is cut from. Empty means HEAD (the default
    `wt_mod.create` behaviour); a resolved remote ref like `origin/integration` cuts
    children from that instead.
    """
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        probe = workspace / ".gbfleet-spawn"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise wt_mod.WorkspaceUnwritable(
            f"workspace {workspace} is not writable ({exc}). "
            "Pass --workspace at a path this process can write "
            "(Grok's sandbox allows ~/.grok/ and the repository, not a sibling)."
        ) from exc
    try:
        return wt_mod.create(repo, workspace / f"{wave_name}-{slot}", wave_name, slot,
                             base=base or "HEAD")
    except wt_mod.GitError as exc:
        text = str(exc).lower()
        if "operation not permitted" in text or "permission denied" in text:
            raise wt_mod.WorkspaceUnwritable(
                f"workspace {workspace} rejected a worktree ({exc}). "
                "Pass --workspace at a path this process can write."
            ) from exc
        raise


def start_one(
    tree: Worktree,
    seat: Seat,
    launch_factory: LaunchFactory,
    client: Graphban,
    limits: Limits,
    partition: Partition,
    *,
    workspace: Path,
    deny: list[str] | None = None,
    allow: list[str] | None = None,
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
    # GRPH-870: run [setup].commands for EVERY adapter, not only gbagent. A fresh
    # `git worktree` has no generated client, no node_modules, no .venv — a vendor
    # child that starts in an unbuilt tree and reports green tests is the worse
    # outcome. `prepare` no-ops when `.gbagent.toml` has no [setup]; a setup that
    # fails refuses the spawn (SetupFailed ⊂ ConfigRefused).
    prepare(tree.path)
    launch = launch_factory(seat, tree, _instruction_file(tree, seat, wave_name), debug_file)
    child = spawn(
        launch, tree.path, tree.branch, _logs(workspace, f"{wave_name}-{slot}"), base=tree.base,
        # Built per wave under the workspace, not the worktree: a stub directory inside the
        # tree would be salvaged into the child's own branch (GRPH-818).
        shim_dir=shim_mod.build(workspace / "shim", deny=deny, allow=allow),
    )
    child.role = seat.role
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
    into: list[Child] | None = None,
    persist: Callable[[], None] | None = None,
    room: Headroom | None = None,
    base: str = "",
) -> Iterable[Child]:
    """Create a worktree per seat, spawn into it, and wait for it to register.

    **A launch failure stops the wave rather than continuing down the list.** The
    failures S2 describes are adapter-shaped — a missing binary, a version mismatch, a
    child that never registers — and they are identical for every seat. Spawning three
    more children into three more worktrees to watch them fail the same way costs three
    more salvage branches and tells nobody anything new.

    **The memory gate stops the loop the same way** (GRPH-842), and for the same reason:
    if the machine has no room for this child it has none for the next one either. It is
    checked HERE, once per seat, rather than as a cap computed before the loop — a single
    reading taken up front is stale by the third child, and the sequential launch is the
    boundary where the question is actually asked.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    started: list[Child] = into if into is not None else []
    planned = len(seats)
    taken = set(occupied or ())
    slot_n = 1
    resumes = list(wt_mod.choose_resume(wt_mod.orphans(repo), items or {}))

    for index, seat in enumerate(seats):
        if room is not None:
            verdict = room.allow(len(started))
            if not verdict.allowed:
                wave.gated.append(verdict.reason)
                wave.unused_seats += planned - index
                observe.emit(
                    "memory_gated",
                    running=len(started),
                    seats_unused=planned - index,
                    available=verdict.available,
                    fits=verdict.fits,
                    per_child=limits.child_memory,
                    detail=verdict.reason,
                )
                break
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
                    tree = _tree_for(repo, workspace, wave_name, slot, base=base)
                except wt_mod.BranchExists:
                    taken.add(branch)
                    continue
                break
            def remember(child: Child) -> None:
                started.append(child)
                wave.spawned.append(child)
                if room is not None:
                    room.spawned()
                if persist is not None:
                    persist()
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
        except (LaunchFailed, wt_mod.GitError, wt_mod.BranchExists, SetupFailed) as exc:
            wave.failures.append(f"{agent_slot}: {exc}")
            vendor_limit = isinstance(exc, VendorLimit)
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
            if vendor_limit:
                # GRPH-829. Not this slot's problem: the account is out of quota, so the next
                # child would fail identically in under a second and the one after that too.
                # Re-raised AFTER the cleanup above so the worktree is still reaped and the
                # failure still recorded — the caller decides what a wave does about it, and
                # a supervisor that quietly kept spawning would burn its whole child budget
                # on a wall it has already hit.
                raise exc
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
    _remember_holdings(children, wave.partition)

    for child in children:
        if not child.running:
            continue
        cap = child.wall_clock_cap if child.wall_clock_cap is not None else limits.child_wall_clock
        if time.monotonic() - child.started_at > cap:
            stop(child, Reason.WALL_CLOCK)
            wave.failures.append(
                f"{child.adapter} pid {child.pid}: over {cap:.0f}s, stopped"
            )

    _watch_output(wave, children, limits, debug=debug)
    _enforce_the_lease(wave, children)
    if roster is not None:
        _catch_the_disowned(wave, children, roster, limits)
    # Reap first so the exit post can carry the diff shape computed against the base
    # after salvage. Reporting first would store a null shape that a later post can
    # fill, but the ordinary path should not need two posts to say what the child did.
    #
    # Safe for the spend reading below (GRPH-834), whose own comment used to claim the
    # opposite: `stdout_text` reads `<workspace>/logs/<slot>/stdout.log`, and `reap`
    # removes `<workspace>/<slot>` — the worktree, a SIBLING of the log directory rather
    # than its parent. Measured on a real wave: both children's logs were still readable
    # after their worktrees were gone.
    _reap_exited(wave, children, client)
    _report_exits(children, client, wave)
    if persist is not None:
        persist()


def _reap_exited(wave: Wave, children: list[Child], client: Graphban | None = None) -> None:
    """Salvage a child's work onto its branch as soon as it exits (PRD-38 walk finding).

    `_reap_all` has always done this — `worktree.reap` salvages whatever the worker left
    uncommitted, and the comment there says so — but it runs at the END of a wave, which only
    the `up` surface has. On `gbfleet mcp` a child could exit, be stopped, and leave its
    worktree sitting there uncommitted forever: the branch stayed at its base, the reviewer
    (who reads the BRANCH, PRD-17 D3) saw nothing, and `touchpoints.measure` diffed to empty.

    Measured on the deployed instance: six children across two harnesses produced exactly the
    right text and not one commit, and the first telemetry cell to cross the sample floor read
    0/5 because of it. The children were doing their job. This surface was not doing its half.

    Same shape as the fix P30 D6 made for `watch_tick` itself: a thing that existed only in
    `up` and was missing from the surface a planner actually drives.
    """
    for child in children:
        if child.running or child.reaped:
            continue
        child.reaped = True
        try:
            # Resolving the repo is inside the guard with the reap itself: a Child rebuilt
            # around a pid (adopt) or assembled by hand may name a worktree that is gone, and
            # the supervisor must report that rather than die inside its own watch loop.
            tree = Worktree(path=child.worktree, branch=child.branch, repo=_repo_of(child),
                            base=child.base)
            reaped = wt_mod.reap(tree, message=wt_mod.salvage_message(
                child.adapter, list(child.held_items)))
        except Exception as exc:  # noqa: BLE001 — a failed reap is reported, never fatal
            wave.failures.append(f"{child.branch}: reap failed ({exc})")
            continue
        wave.reaped.append(reaped)
        child.diff_shape = reaped.diff_shape
        # Measured AFTER the salvage, deliberately, exactly as `_reap_all` does: measuring
        # first would miss the work that was most at risk of being lost.
        try:
            git_paths = tp_mod.measure(tree)
        except (ValueError, OSError) as exc:
            wave.failures.append(f"{child.branch}: {exc}")
            git_paths = None
        if git_paths is not None:
            wave.touched[child.branch] = tp_mod.including_stream(
                child.adapter, git_paths, child.stdout_text())
        _note_touchpoints(wave, child)
        _note_staleness(wave, tree)
        _publish(wave, tree, client=client, child=child)


def _declared_into(wave: Wave, items: dict) -> dict:
    """Stash the declaration snapshot on the wave and hand the map straight back.

    A pass-through so `until` records the same operand `up` does at the same moment —
    two call sites reading items and only one remembering what they said is how the
    check would quietly cover half the fleet.
    """
    wave.declared.update({k: list((v or {}).get("touchpoints") or [])
                          for k, v in (items or {}).items()})
    return items


def _note_staleness(wave: Wave, tree: Worktree) -> None:
    """How far the trunk moved while this child worked (GRPH-786).

    The supervisor is the only party that can answer this. The server has no git; the
    reviewer sees a branch and a diff and no indication of what the diff is against; and the
    child was cut from HEAD at spawn and never looked again. Two agents can each be green on
    their own base and conflict on merge — measured here three times in one afternoon, twice
    as a red trunk and once as a branch testing a role that no longer existed.

    Fetches the trunk once per wave before measuring, because a remote-tracking ref is only
    as fresh as the last fetch and a check that never fetched would report every branch as
    current. REPORTED, never acted on: rebasing somebody's work is not the supervisor's call.
    """
    if wave.stale_unmeasured or not tree.base:
        return
    remote = wt_mod.remote_for(tree.repo)
    ref = wt_mod.default_ref(tree.repo, remote)
    if not ref:
        wave.stale_unmeasured = "no remote default branch to compare against"
        return
    if not wave.stale and not wt_mod.refresh_ref(tree.repo, remote, ref):
        wave.stale_unmeasured = f"could not fetch {ref}"
        return
    behind = wt_mod.behind_ref(tree.repo, tree.base, ref)
    if behind:
        wave.stale[tree.branch] = (behind, ref)


def _note_touchpoints(wave: Wave, child: Child) -> None:
    """Compare what this child CHANGED against what its items DECLARED (GRPH-785).

    Runs at reap, where the measurement is taken and both operands are already in hand:
    `search_items(fields="full")` returns `touchpoints`, and it is a read the supervisor
    already makes. No new permission, no write, and no new call.

    Both findings are recorded on the wave and printed by `report`. The supervisor decides
    nothing with them: it holds `fleet_status` and `propose_allocation` and cannot call
    `update_item`, and what "collides" means belongs to the server.
    """
    measured = wave.touched.get(child.branch) or []
    if not measured:
        return
    declared: list[str] = []
    # `held_items`, not live roster holdings: `_roster` is overwritten after `release_item`,
    # so at reap the live view is empty and the comparison would have no items to look up.
    for item_id in (child.held_items or []):
        declared += list(wave.declared.get(item_id) or [])
    if declared:
        missing = tp_mod.undeclared(measured, declared)
        if missing:
            wave.undeclared[child.branch] = missing
    # Recomputed over every branch measured so far, so the last reap holds the whole wave's
    # answer — a pairwise check at each reap would miss the pair reaped either side of it.
    wave.collided = tp_mod.overlaps(wave.touched)


def _publish(wave: Wave, tree: Worktree, *, client: Graphban | None = None,
             child: Child | None = None, propose_prs: bool = True) -> None:
    """Put the child's branch where the reviewer can read it (GRPH-750).

    A step AFTER the reap rather than part of salvage. Salvage commits, which the supervisor
    owes the work; this reaches outside the machine, which is a different kind of act and is
    reported separately.

    Found by re-walking PRD-38 criterion 17 after #639: the child's work was committed at
    `6df78dc7` on its branch and the reviewer still bounced it — correctly — because that
    branch existed only in the checkout the supervisor ran against. Nothing in this package
    had ever run `git push`. On one machine that is invisible; with a reviewer on another
    machine reading the remote, the PRD-17 D3 handoff pointed somewhere it could not reach.
    """
    # Takes the `Worktree`, not the Child, because the reap has already REMOVED the
    # directory by the time this runs — resolving the repo from a path that no longer
    # exists is how the first version of this failed every wave.
    try:
        pushed = wt_mod.push_branch(tree.repo, tree.branch, tree.base)
    except Exception as exc:  # noqa: BLE001 — a failed publish is reported, never fatal
        wave.failures.append(f"{tree.branch}: publish failed ({exc})")
        return
    wave.published[tree.branch] = pushed
    if not pushed.ok and not pushed.skipped:
        wave.failures.append(f"{tree.branch}: {pushed.reason}")
    # GRPH-754: tell the server the work is now readable. Until this lands the server withholds
    # the item from `claim_review`, so a reviewer can no longer be handed an item whose branch
    # it will fetch a 404 for — measured on the deployed instance, twice. Only on a real push:
    # saying "published" about a branch that skipped would re-open the window it closes.
    if pushed.ok and client is not None and child is not None and child.seat_id:
        client.post_attempt(enrolment_id=child.seat_id, branch_published=True)
    if pushed.ok and propose_prs:
        _propose(wave, tree, client=client, child=child)


def _propose(wave: Wave, tree: Worktree, *, client: Graphban | None,
             child: Child | None, base_override: str = "") -> None:
    """Open a draft PR for the branch just published (GRPH-804).

    The other half of "done does not mean merged". GRPH-798 HOLDS an item whose dependency is
    not in the base; nothing made that hold clear, because nothing proposed the merge. On the
    reported wave the item that reached `done` had no PR while the one that only reached
    `review` did — the more complete item was the one that went missing.

    After the push and never instead of it. A PR for a branch that is not on the remote is a
    PR for nothing, and the push is the step that can actually fail.
    """
    propose_branch(wave, tree.repo, tree.branch,
                   [i for i in ((child.held_items if child else []) or []) if i],
                   client=client, base_override=base_override)


def propose_branch(wave: Wave, repo: Path, branch: str, items: list[str], *,
                   client: Graphban | None,
                   base_override: str = "") -> None:
    """The branch, the items it served, and a draft PR joining them.

    Split out of `_propose` for the salvage path (GRPH-830), which has a repo, a branch and a
    list of items but no `Worktree` — by the time a takeover runs, the tree is gone — and no
    `Child`, because the process it belonged to is dead.

    `base_override` (GRPH-847) targets the PR at a non-default base (e.g. an integration
    branch for stacked slices). Empty means the remote's default ref.
    """
    remote = wt_mod.remote_for(repo)
    base = base_override or (wt_mod.default_ref(repo, remote) if remote else "")
    title, body = propose_mod.describe(branch, items,
                                       propose_mod.subject(repo, branch, base))
    got = propose_mod.propose(repo, branch, base, title=title, body=body)
    wave.proposed[branch] = got
    if got.reason and not got.url:
        # Reported, never fatal: the work is committed and pushed by now, and a PR nobody
        # could open is a thing for a person to finish rather than a broken wave.
        wave.failures.append(f"{branch}: {got.reason}")
    if not (got.url and client is not None and items):
        return
    # Recorded on the ITEM, because that is where a reviewer looks and where the ledger's own
    # PR-cooldown reads from (`items.pr_linked_at`). Starting that clock is the intended
    # effect: `done` should not be claimable the same minute the PR appeared.
    for item_id in items:
        try:
            client.call("update_item", id=item_id, evidence=[{
                "kind": "url",
                "detail": f"draft PR opened by gbfleet for {branch}",
                "url": got.url,
            }])
        except Exception as exc:  # noqa: BLE001 — a wave is not broken by a missing receipt
            wave.failures.append(f"{item_id}: PR opened but not recorded ({exc})")


def publish_salvaged(wave: Wave, repo: Path, salvaged: list, *,
                     client: Graphban | None) -> None:
    """Push what a takeover recovered, and say on the item that it exists (GRPH-830).

    Adopting a stranded worktree already worked — the commit is made and the note is printed.
    What did not work was the step after: one measured takeover salvaged 614 insertions across
    exactly one item's touchpoints and left them on a LOCAL branch nothing pointed at. The item
    was re-delegated minutes later, branched from `main`, and rebuilt every line. The work was
    recovered and lost in the same move, and only somebody reading local refs could tell.

    Deliberately the SAME two steps a finished child gets — push, then a draft PR carrying the
    item — rather than a quieter salvage-only path. A reviewer looking for the work has one
    place to look either way, and the PR body already says the branch was salvaged because the
    commit subject does.

    Never fatal. This runs at the very start of a wave, on the crash path, and a takeover that
    refused to proceed because a push failed would strand the next wave too.
    """
    for row in salvaged:
        try:
            pushed = wt_mod.push_branch(repo, row.branch, row.base)
        except Exception as exc:  # noqa: BLE001
            wave.failures.append(f"{row.branch}: salvaged but not published ({exc})")
            continue
        wave.published[row.branch] = pushed
        if not pushed.ok:
            if not pushed.skipped:
                wave.failures.append(f"{row.branch}: salvaged but not published "
                                     f"({pushed.reason})")
            continue
        observe.emit("adopt", detail=f"{row.branch}: published salvaged work"
                                     + (f" for {', '.join(row.items)}" if row.items else ""))
        propose_branch(wave, repo, row.branch, list(row.items or []), client=client)


#: How long a watched item waits before its merge is re-asked. Each ask is a ledger read and
#: a `gh` call against the forge; once a second, for the length of a wave, is a rate limit
#: spent on a question whose answer changes on the scale of a CI run.
MERGE_RECHECK_S = 60.0


@dataclass
class Merger:
    """Finishes the merge for items that reach `done` (GRPH-846). Off unless asked.

    WHAT IT WATCHES. An item is a candidate the moment it LEAVES `review` — the loop reads
    the review rows every tick anyway, and an id that was there and is not has either been
    signed off or bounced; `get_item_details` says which. And an item the GRPH-798 hold
    names as an absent dependency is a candidate too: it is `done` by definition, its PR is
    the click the held item is waiting on, and clearing that hold is the whole point.

    WHAT IT DOES. `propose.merge`, with the commit the sign-off attestation names and the
    commits CI attested, both read from the item — never derived from a branch. A merge
    is recorded on the item as a `url` receipt carrying the MERGE COMMIT, because a squash
    rewrites the SHA: the reviewed commit is never an ancestor of the trunk afterwards, and
    without the receipt the dependency check would hold every dependant forever on a
    merge that happened.

    WHAT IT NEVER DOES. Decide. Every precondition miss leaves the item alone and says
    which check failed; a merge the forge refuses is `skipped`. Branch protection is the
    backstop and this does not go around it.
    """

    repo: Path
    client: Graphban
    enabled: bool = False
    #: Item ids seen in `review` at the last read, so a departure is visible.
    in_review: set[str] = field(default_factory=set)
    #: id -> branch as last seen in review, kept so a departed item still has its hint.
    branches: dict[str, str] = field(default_factory=dict)
    #: Candidates: item id -> the branch it was last seen on (a selector hint; the item's
    #: own PR and receipts are preferred once it is read).
    watching: dict[str, str] = field(default_factory=dict)
    #: When each candidate was last asked, for `MERGE_RECHECK_S`.
    asked_at: dict[str, float] = field(default_factory=dict)
    #: The last reason reported per item, so a wave's log says a thing once, not per tick.
    said: dict[str, str] = field(default_factory=dict)
    #: The ref children are cut from, and the remote it lives on — re-fetched after a
    #: merge so the GRPH-798 hold clears on the next tick without a restart.
    remote: str = ""
    base: str = ""
    #: Set once the client turned out not to hold a tool this needs. Reported once and the
    #: merger stops asking, rather than a refusal per tick for the length of the wave.
    disabled_because: str = ""

    def note_review(self, rows: list[dict]) -> None:
        """Rows currently in `review`. Whatever was there last time and is not now is a
        candidate — signed off or bounced, and `tick` reads the item to tell which."""
        if not self.enabled:
            return
        now = {str(r.get("id")): str(r.get("branch") or "") for r in rows
               if isinstance(r, dict) and r.get("id")}
        for item_id in self.in_review - set(now):
            self.watching.setdefault(item_id, self.branches.get(item_id, ""))
        self.branches = now
        self.in_review = set(now)

    def note_hold(self, absent: list[dict]) -> None:
        """Dependencies the GRPH-798 check found finished-but-not-on-the-base. Each is a
        merge that would clear a hold, which is the merge most worth finishing."""
        if not self.enabled:
            return
        for row in absent:
            item_id = str(row.get("id") or "")
            if item_id:
                self.watching.setdefault(item_id, "")

    def tick(self, wave: Wave, *, rows: list[dict] | None = None,
             now: Callable[[], float] = time.monotonic) -> bool:
        """Ask once per candidate that is due. Returns True when something MERGED this tick,
        so the caller knows the base moved and any hold keyed on it should be re-asked."""
        if not self.enabled or self.disabled_because:
            return False
        if rows is None:
            rows = self._review_rows()
            if rows is None:
                return False
        self.note_review(rows)
        merged_any = False
        for item_id in list(self.watching):
            stamp = now()
            if stamp - self.asked_at.get(item_id, -1e9) < MERGE_RECHECK_S:
                continue
            self.asked_at[item_id] = stamp
            got = self._attempt(wave, item_id)
            if got is None:
                continue
            wave.merged[item_id] = got
            self._say(item_id, got)
            if got.ok:
                merged_any = True
            if got.final:
                self.watching.pop(item_id, None)
        if merged_any and self.remote and self.base:
            # The trunk moved. A remote-tracking ref is only as fresh as the last fetch, so
            # without this the dependency check would go on measuring the merge as absent
            # — the same absence-reads-as-clean failure the check itself exists to avoid.
            wt_mod.refresh_ref(self.repo, self.remote, self.base)
        return merged_any

    def _review_rows(self) -> list[dict] | None:
        try:
            payload = self.client.call("search_items", status="review", fields="full",
                                       limit=10_000)
        except NotPermitted as exc:
            self._disable(f"cannot read review rows: {exc}")
            return None
        except (ToolFailed, ServerUnreachable):
            return None
        rows = payload.get("results") if isinstance(payload, dict) else None
        return [r for r in rows if isinstance(r, dict) and r.get("id")] \
            if isinstance(rows, list) else None

    def _attempt(self, wave: Wave, item_id: str) -> "propose_mod.Merged | None":
        try:
            item = self.client.call("get_item_details", id=item_id) or {}
        except NotPermitted as exc:
            self._disable(f"cannot read {item_id}: {exc}")
            return None
        except (ToolFailed, ServerUnreachable) as exc:
            observe.emit("merge_unread", item=item_id, detail=str(exc))
            return None
        status = str(item.get("status") or "")
        if status != "done":
            # Bounced, or still moving. Not a candidate until it is done — and a bounced
            # item that later returns to review is picked up again by `note_review`.
            self.watching.pop(item_id, None)
            return None
        selector = propose_mod.pr_selector(item) or self.watching.get(item_id, "")
        got = propose_mod.merge(
            self.repo, item_id, selector,
            reviewed=propose_mod.reviewed_commit(item),
            green=propose_mod.green_commits(item),
        )
        if got.ok and got.commit:
            got = self._record(wave, got)
        return got

    def _record(self, wave: Wave, got: "propose_mod.Merged") -> "propose_mod.Merged":
        """The receipt. `commit` is on the row so `deps._commits` reads the merge commit as
        one of the item's attested commits — the squash SHA is the only one the trunk has.

        A `url` row, not an attestation: this credential holds no `gate` scope, so an
        attestation is refused to it, and nobody ran anything at the squash SHA anyway — it
        was observed to land. The server keeps `commit` on a `url` for exactly this row
        (items.normalize_evidence, pinned in backend/tests/test_merge_receipt.py); it was
        bounced once for writing a field the server stripped while the fake ledger here kept
        it, so the fakes in the tests now drop what the server drops."""
        try:
            self.client.call("update_item", id=got.item, evidence=[{
                "kind": "url",
                "detail": f"merged by gbfleet: squash of {got.selector or 'the PR'} landed as "
                          f"{got.commit[:12]}",
                "url": got.url,
                "commit": got.commit,
            }])
        except NotPermitted as exc:
            self._disable(f"merged {got.item} but cannot record it: {exc}")
            wave.failures.append(f"{got.item}: merged as {got.commit[:12]} but not recorded "
                                 f"({exc})")
        except (ToolFailed, ServerUnreachable) as exc:
            wave.failures.append(f"{got.item}: merged as {got.commit[:12]} but not recorded "
                                 f"({exc})")
        return got

    def _say(self, item_id: str, got: "propose_mod.Merged") -> None:
        key = f"{got.ok}|{got.pending}|{got.skipped}|{got.reason}"
        if self.said.get(item_id) == key:
            return
        self.said[item_id] = key
        observe.emit("merge", item=item_id, ok=got.ok, pending=got.pending,
                     skipped=got.skipped, commit=got.commit, url=got.url,
                     checked=list(got.checked), detail=got.reason)

    def _disable(self, why: str) -> None:
        self.disabled_because = why
        observe.emit("merge_disabled", detail=why)


def _report_exits(children: list[Child], client: Graphban, wave: "Wave | None" = None) -> None:
    """PRD-38 D3, the exit report: what only this process saw about a child that has ended.

    Here rather than in `_reap_all` because reaping is the END of a wave and a child that
    exits in minute two of an hour would otherwise be reported an hour late, or not at all if
    the supervisor dies first. `watch_tick` runs in both loops, so both get it.

    Addressed by the SEAT's row id, which is what the roster gives this process; the
    delegation id is the server's business and it binds the two itself. A child that never
    registered has no seat id and is skipped — there is no attempt to report, and that
    silence is already the load-bearing signal `registration_latency` exists to carry.

    **Turns and tokens are deliberately absent.** They live in each vendor's own result
    record, in a shape this repository has not measured for any vendor but one, and a parser
    written from memory would put invented numbers in a table whose whole purpose is to be
    checkable. The columns exist and stay null, which the page renders as "not reported".
    """
    for child in children:
        if child.running or child.reported:
            continue
        # Marked before the post, not after: a post that fails returns None by design, and
        # retrying it every tick for the life of the wave would turn one lost measurement
        # into a loop.
        child.reported = True
        code = child.process.poll()
        # What the vendor's own result record says this run cost (PRD-38 D3). A vendor that
        # prints nothing contributes nothing here and its token fields stay NULL, which the
        # page renders as "not reported" — never as zero.
        facts = adapters.result_facts(child.adapter, child.stdout_text())
        if wave is not None and facts:
            # Kept HERE rather than recomputed at the end of the wave: the vendor's record
            # lives only in this child's stdout and the wave summary has no other route to
            # it. It does NOT depend on running before the reap — the log sits in the
            # workspace's `logs/` directory and the reap removes the worktree beside it, so
            # the two are ordered by what the POST needs, not by what this reading needs.
            wave.spend[child.branch] = {"adapter": child.adapter, **facts}
        if not child.seat_id:
            # No seat, so there is no attempt row to address — but the run still COST
            # something, and the wave summary is entitled to it (GRPH-834). The seat guard
            # used to sit at the top of this loop and skipped the reading as well as the
            # posting, so a child whose registration never landed spent tokens that nothing
            # counted.
            continue
        client.post_attempt(
            enrolment_id=child.seat_id,
            adapter=child.adapter,
            binary_version=child.binary_version or None,
            wall_seconds=int(time.monotonic() - child.started_at),
            turn_budget=child.turn_budget,
            exit_meaning=_exit_meaning(child, code),
            diff_shape=child.diff_shape,
            **facts,
        )


def _exit_meaning(child: Child, code: int | None) -> str:
    """What the adapter says its exit code means, or the reason this supervisor stopped it.

    The supervisor's own reason wins when it has one: a child killed for running past the
    wall clock exits with whatever the signal produced, and reporting that number as the
    vendor's verdict would attribute the supervisor's decision to the harness.
    """
    if child.stopped_because is not None:
        return f"stopped: {child.stopped_because.value}"
    if code is None:
        return ""
    return explain_exit(child.adapter, code) or ("ok" if code == 0 else f"exit {code}")


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
    merger: "Merger | None" = None,
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
        if merger is not None:
            merger.tick(wave)
        if any(child.running for child in children):
            sleep(poll)

    # One last read, so a partition that ended just as the children did is still
    # reconciled rather than left as the last thing that happened.
    _roster(client, wave.partition)
    _remember_holdings(children, wave.partition)


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
        if child.reaped:
            # Already salvaged on exit. Reaping again would find no worktree and report a
            # failure for work that is safely on its branch.
            continue
        child.reaped = True
        tree = Worktree(
            path=child.worktree, branch=child.branch, repo=_repo_of(child), base=child.base
        )
        held = list(child.held_items) or [
            i for i in (wave.partition.held.get(child.agent_id) or []) if i
        ]
        reaped = wt_mod.reap(
            tree, message=wt_mod.salvage_message(child.adapter, held),
        )
        wave.reaped.append(reaped)
        child.diff_shape = reaped.diff_shape

        # AFTER the reap, deliberately: salvage has just committed whatever the worker
        # left uncommitted, so the branch now holds the whole of what it did. Measuring
        # before would miss exactly the work that was most at risk.
        try:
            git_paths = tp_mod.measure(tree)
        except ValueError as exc:
            # Not silently empty. "We could not measure" and "it changed nothing" are
            # different answers and only one of them is reassuring.
            wave.failures.append(f"{child.branch}: {exc}")
            git_paths = None
        if git_paths is not None:
            # GRPH-215 CALL: union vendor stream writes (Cursor stream-json) onto
            # the git-diff measurement. Skipping this would leave the parser correct
            # and the reap blind to it.
            wave.touched[child.branch] = tp_mod.including_stream(
                child.adapter, git_paths, child.stdout_text(),
            )
        _note_touchpoints(wave, child)
        _note_staleness(wave, tree)
        _publish(wave, tree)
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
            exit_meaning=explain_exit(child.adapter, child.process.returncode),
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
