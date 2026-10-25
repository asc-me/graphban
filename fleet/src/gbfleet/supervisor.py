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
from .client import Graphban, NotPermitted, ProtocolError, ServerUnreachable, ToolFailed
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
    await_registration, build_holdings,
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


@dataclass(frozen=True)
class WaveBase:
    """The ref this wave's children are cut from, and which rule named it (GRPH-1012).

    Three rules, in order: the operator's `--base`, the project's MEASURED
    `gitops.base_branch`, and the remote's own default ref. `source` says which one
    answered, because "cut from `origin/main`" does not tell an operator whether the
    project chose that or nobody did — and the second is the reading that has to stay
    visible, since it is the one that looks like a decision.

    `ref` is empty only when this repository has no remote to cut from. That is a fact
    about the repo, not a failure to ask, and `source` is then `head`: children are cut
    from whatever the checkout happens to be standing on.
    """

    ref: str
    #: `flag` | `gitops` | `remote_default` | `head`.
    source: str
    #: One line for the operator. Carries what gitops said even when gitops was NOT the
    #: answer, because "the project stated no base" is the half that would otherwise be
    #: inferred from a branch name that looks like everybody's default.
    note: str

    def describe(self) -> str:
        return f"{self.ref or 'the checkout HEAD'} — {self.note}"


@dataclass
class Wave:
    """What one `up` actually did. Every field is something that happened."""

    lock: Acquired | None = None
    #: The base this wave cut its children from, resolved once at startup (GRPH-1012).
    #: None means this wave never resolved one — which is not the same as `WaveBase(ref="")`,
    #: a repo with no remote. Reported by `cli.report` so the summary names the rule.
    base: "WaveBase | None" = None
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
    #: The supervisor still decides nothing on the ledger with it, but it does refuse to
    #: open a PR for such a branch (GRPH-949, `_refuse_proposal`).
    undeclared: dict[str, list[str]] = field(default_factory=dict)
    #: id -> declared touchpoints, as they stood when work was handed out. NOT the operand
    #: `_note_touchpoints` compares against: that is the item as it stands at reap (GRPH-1025).
    #: This is the fallback for when the item cannot be read then.
    declared: dict[str, list[str]] = field(default_factory=dict)
    #: branch -> why its items' declarations could not be re-read at reap, so the undeclared
    #: check compared against the delegation-time snapshot instead (GRPH-1025). Empty means
    #: every comparison used the item as it stood at reap — not that none was made.
    declared_unread: dict[str, str] = field(default_factory=dict)
    #: What each finished child's own result record said the run cost (GRPH-834), by branch:
    #: `{tokens_in, tokens_out, turns_used, adapter}`. Read from the same vendor record
    #: `_report_exits` already posts to the ledger — the numbers existed and reached the
    #: server, and the wave summary was the one place that never saw them.
    #:
    #: A child ABSENT from this dict said nothing, which is not the same as saying zero. The
    #: summary reports the two separately for that reason: "412k tokens" reads as the wave's
    #: total, and it is not the total if four of six children were never counted.
    spend: dict[str, dict] = field(default_factory=dict)
    #: branch -> (asked, answered) where a child ran a model nobody chose (GRPH-982). Present
    #: and empty by default: "no substitution was observed" and "no stream could be read" are
    #: different, and `spend` already says which children reported at all.
    substituted: dict[str, tuple] = field(default_factory=dict)
    #: branch -> (commits behind, the ref it was measured against). How much had landed on
    #: the trunk that this worker never had in front of it (GRPH-786). A reviewer reading a
    #: branch cut from a base the trunk has moved past is reading a diff against a world
    #: that no longer exists, and nothing said so. The count is the measurement; whether it
    #: blocks a proposal is `behind_clean` / `behind_block` (GRPH-1087).
    stale: dict[str, tuple] = field(default_factory=dict)
    #: Set when the trunk could not be fetched. "We could not ask" is not "nothing moved",
    #: and reporting the second for the first is the failure this whole check is about.
    stale_unmeasured: str = ""
    #: branch -> "N behind ref, no shared files, merges clean", and ONLY when both checks
    #: ran and neither fired (GRPH-1087). Present means the branch may be proposed despite
    #: `stale`. Absent does not mean that: a stale branch with no entry here is refused.
    behind_clean: dict[str, str] = field(default_factory=dict)
    #: branch -> why being behind blocks the proposal, when the check ran and found a
    #: shared file, a merge-tree conflict, or could not be asked. The unclassified stale
    #: case (no entry here either) still refuses, with the older "rebase before proposing".
    behind_block: dict[str, str] = field(default_factory=dict)
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
    #: Names this wave refused because the remote already had them (GRPH-1088). The
    #: next free slot is cut instead. Recorded as the refusal `create` raised, so the
    #: report says which name and which commit were left alone.
    remote_taken: list[str] = field(default_factory=list)
    #: Items a reaped child no longer held, so reap did not release them (GRPH-949). INFO,
    #: not `failures`: the child delivered to review, or its lease lapsed and moved on, and
    #: the server would rightly refuse the release. One line per item.
    lease_moved: list[str] = field(default_factory=list)
    #: Branches pushed but NOT proposed as a PR, with why (GRPH-949): a change outside every
    #: declared touchpoint, or a base the trunk has moved past in a way that matters — a
    #: shared file, a merge conflict, or a behind-check that could not be asked (GRPH-1087).
    #: Also in `proposed` as skipped.
    unproposed: dict[str, str] = field(default_factory=dict)
    #: branch -> the head this wave pushed while its child was STILL RUNNING, because one of
    #: the child's items had already reached `review` (GRPH-987). Kept apart from `published`,
    #: which is the reap's: an early push is the one that closes the window a reviewer outside
    #: this checkout loses, and it happens before there is any diff shape, staleness or
    #: touchpoint measurement to propose against. Empty means no item reached review ahead of
    #: its child, which is the ordinary case and not a failure to publish.
    published_in_review: dict[str, str] = field(default_factory=dict)
    #: branch -> the items this wave saw in `review` while their branch was NOT readable off
    #: this machine (GRPH-987). A reviewer handed one of these can only bounce for a reason
    #: that is not the diff, and that bounce lands on the builder's vendor and model, so the
    #: exposure is recorded rather than left to be inferred from a bounce_reason. Empty means
    #: every item seen in review was published before it could be claimed.
    review_unreadable: dict[str, list[str]] = field(default_factory=dict)
    #: Set when the ledger could not be asked what is in `review`. "We could not ask" is not
    #: "nothing was awaiting review", and an empty `review_unreadable` reads as the second —
    #: the same shape as `stale_unmeasured`, for the same reason.
    review_unmeasured: str = ""
    #: branch -> the head already put to the remote by the early publish, whatever it answered
    #: (GRPH-987). The retry guard, and deliberately not folded into `published_in_review`:
    #: that one says what a reviewer can read, this one says what has already been asked. A
    #: watch tick runs every poll interval — every second on `gbfleet mcp` — so without it a
    #: refused push becomes a `git push` per tick against a remote that already said no, inside
    #: the loop everything else waits on. One attempt per REVISION: the reap pushes again at the
    #: end of the wave, so a transient refusal costs the early publish and not the work.
    review_publish_tried: dict[str, str] = field(default_factory=dict)
    #: Set when this wave could not read an item's evidence, so it could not tell whether a
    #: commit of that item is already in the trunk (GRPH-1053). Empty means the check ran,
    #: or no push needed it. A push that went ahead anyway is not "nothing had landed".
    landed_unmeasured: str = ""
    #: Original branch -> (side branch, why). A salvage or rework that must not move the
    #: ref a landed fix already uses (GRPH-1053).
    diverted: dict[str, tuple[str, str]] = field(default_factory=dict)

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
            # GRPH-1079: remembered, not replaced. The next reading drops an item the child
            # moved to review, and that item's work is still on this branch.
            seen = child.ever_held
            seen += [i for i in items if i not in seen]


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
            # The DECLARATION the partition was computed from (GRPH-785). Only the FALLBACK
            # for the reap-time check since GRPH-1025: comparing against this snapshot
            # refused children for touchpoints they had correctly added mid-build.
            "touchpoints": list(row.get("touchpoints") or []),
            # GRPH-1031: a bounced item's recorded branch is the head the next spawn
            # resumes. Absent from the lean projection; `fields=full` carries both.
            "branch": row.get("branch") or "",
            "bounce_reason": row.get("bounce_reason") or "",
        }
    return out


def _holdings(roster: dict) -> dict[str, list[str]]:
    """Build leases per agent. Review claims are excluded — see `build_holdings`.

    This feeds `partition.held`, `_remember_holdings` and from there the salvage subject, so
    a review claim arriving here would end up as an item a reaped reviewer's branch is filed
    against (GRPH-1001 review bounce).
    """
    return {
        a["id"]: [h.get("id") for h in build_holdings(a)]
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
    base_branch: str = "",
    wave_base: "WaveBase | None" = None,
) -> Wave:
    """Run one wave to completion and return what happened.

    Holds the repo lock for the whole wave (D-h), so a second supervisor on this
    repository refuses to start rather than exceeding `max_workers` between them.

    `base_branch` is the operator's `--base`, and `wave_base` an already-resolved answer
    from a caller that needed the ref itself (`cli` builds `--merge`'s Merger out of it).
    Given neither, this resolves one — so there is no route from here to `_tree_for` that
    does not pass a FETCHED base, which is the guarantee GRPH-1012 exists for.
    """
    repo, workspace = _rooted(repo, workspace)
    wave = Wave()

    observe.configure(state)

    chosen = wave_base if wave_base is not None else resolve_wave_base(
        repo, client, base_branch=base_branch)
    wave.base = chosen
    observe.emit("base", detail=chosen.note)
    base = chosen.ref

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
            publish_salvaged(wave, repo, recovered.salvaged, client=client,
                             base_branch=base)
        # GRPH-1011. The same sweep `gc` runs, over EVERY repository this state directory
        # names rather than only this one: the trees that need it belong to the clone
        # nobody starts a supervisor on again, and `recover` above only ever reaches the
        # trees of the lock we just took. Inside the lock, so this repository answers
        # "held" to the sweep's own try-acquire and is skipped by the rule that protects
        # every other live supervisor. Notes rather than failures — `sweep` does not raise.
        for line in adopt_mod.sweep(state).lines:
            observe.emit("gc", detail=line)
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
        # The declaration snapshot the divvy used: the reap-time fallback (GRPH-1025).
        _declared_into(wave, items)
        _start(
            wave, seats[:wanted], launch_factory, repo, workspace, wave_name, client,
            limits, debug=debug, occupied=occupied, items=items,
            into=children, persist=persist,
            room=room, base=base,
        )
        persist()
        _wait_out(wave, children, limits, client, poll=poll, sleep=sleep, debug=debug,
                  persist=persist, merger=merger, base_branch=base)
        _reap_all(wave, children, client=client, base_branch=base)
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


def gitops_base_branch(client) -> tuple[str, str]:
    """The project's MEASURED `base_branch`, or "" plus the reason it is not one.

    Read, never inferred. `source: unmeasured` means the project has stated no base —
    which is not `main`, and is not "no requirements" either. Inventing a trunk here
    would be the same move `gbagent.orient.INSTRUCTION` warns a child off ("do not guess
    main"), made by the one party that can act on it silently: the supervisor cuts the
    worktrees, so a guess becomes every child's history.

    An UNREADABLE `get_context` is reported as unreadable rather than folded into
    unmeasured. Both fall through to the remote's default ref, but they are different
    facts about the project and the note is the only place either one survives.
    """
    if client is None:
        return "", "gitops.base_branch not asked (no ledger client on this call path)"
    try:
        ctx = client.call("get_context") or {}
    except Exception as exc:  # noqa: BLE001 - a base we could not ask for is reported, not fatal
        return "", f"gitops.base_branch unreadable ({str(exc)[:80]})"
    gitops = ctx.get("gitops") if isinstance(ctx, dict) else None
    row = gitops.get("base_branch") if isinstance(gitops, dict) else None
    row = row if isinstance(row, dict) else {}
    source = str(row.get("source") or "unmeasured")
    value = row.get("value")
    if source == "unmeasured" or not isinstance(value, str) or not value.strip():
        return "", f"gitops.base_branch unmeasured (source {source})"
    return value.strip(), f"gitops.base_branch is {value.strip()!r} (source {source})"


def resolve_wave_base(repo: Path, client=None, *, base_branch: str = "",
                      remote: str = "") -> WaveBase:
    """THE ref this wave cuts children from — resolved once, and FETCHED here (GRPH-1012).

    Three rules, first answer wins:

    1. `base_branch`, the operator's `--base`. Refuses when the remote does not have it,
       which is `wt_mod.resolve_base`'s existing behaviour and stays it.
    2. `gitops.base_branch`, when `get_context` reports it MEASURED. A project that has
       stated its trunk is stating where its work lands, and a supervisor that cut from
       the remote default instead would be overruling it without saying so.
    3. The remote's own default ref (`wt_mod.default_ref`), read from the recorded
       `origin/HEAD` symref and never guessed from a list of likely names.

    Every one of them is fetched before this returns, because a remote-tracking ref is
    only as fresh as the last fetch: cutting a child from a stale `origin/main` builds it
    on a world that no longer exists, and the diff a reviewer reads is against that world.
    Rules 1 and 2 fetch inside `resolve_base`; rule 3 fetches here, and a fetch that
    FAILED is said so in the note rather than reported as a current base.

    **The checkout's HEAD is not one of the three.** That was the defect: `up` and MCP
    `spawn` cut from `HEAD`, so a supervisor standing on a feature branch built every
    child on that branch, and a separate clone whose `origin/main` had never been fetched
    looked current to itself. HEAD is reached now only when the repository has no remote
    at all, and then `source` says `head` out loud.
    """
    remote = remote or wt_mod.remote_for(repo)
    if not remote:
        return WaveBase(
            ref="", source="head",
            note="this repository has no remote, so there is nothing to fetch and cut "
                 "from — children are cut from the checkout HEAD",
        )
    if base_branch:
        ref = wt_mod.resolve_base(repo, remote, base_branch)
        return WaveBase(ref=ref, source="flag",
                        note=f"--base {base_branch} → {ref}, fetched")

    branch, gitops_note = gitops_base_branch(client)
    if branch:
        try:
            ref = wt_mod.resolve_base(repo, remote, branch)
        except wt_mod.BaseBranchNotFound as exc:
            # Refused, not fallen past. A measured project rule naming a branch this
            # remote does not have is a misconfiguration the operator has to see: quietly
            # cutting from the default ref instead would put every child of the wave on a
            # base the project never chose, and the wave summary would read as normal.
            raise wt_mod.BaseBranchNotFound(
                f"{gitops_note}, but {exc}. Pass --base to name the ref this wave cuts "
                "from, or correct the project's git process."
            ) from exc
        return WaveBase(ref=ref, source="gitops", note=f"{gitops_note} → {ref}, fetched")

    ref = wt_mod.default_ref(repo, remote)
    if not ref:
        return WaveBase(
            ref="", source="head",
            note=f"{gitops_note}; and {remote} has no default ref recorded here, so "
                 "children are cut from the checkout HEAD",
        )
    fetched = wt_mod.refresh_ref(repo, remote, ref)
    return WaveBase(
        ref=ref, source="remote_default",
        note=f"{gitops_note}, so this cuts from {remote}'s default ref {ref}"
             + (", fetched" if fetched else
                " — THE FETCH FAILED, so this ref is only as fresh as the last one"),
    )


def _tree_for(repo: Path, workspace: Path, wave_name: str, slot: str,
              base: str = "") -> Worktree:
    """One worktree on its own branch.

    D-g names the branch `gb/<wave>-<agent-short-id>`, and the agent id does not exist
    yet: it is minted server-side at `register_agent`, which cannot happen until the
    child is running, which cannot happen until it has a worktree on a branch. The slot
    stands in — deterministic, collision-free within a wave, and the roster ties agent to
    worktree once the child registers.

    `base` (GRPH-847) is the ref the worktree is cut from, and every caller now arrives
    here with one resolved by `resolve_wave_base` (GRPH-1012) — so it is a fetched remote
    ref whenever this repository has a remote, and never the checkout's HEAD by accident.
    Empty means HEAD (the default `wt_mod.create` behaviour), which is what a repository
    with no remote falls back to.
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
            # GRPH-1031. A bounce of a pushed PR is not a salvage: choose_resume skips
            # anything whose subject is not `WIP: salvaged by gbfleet`, so the next
            # spawn used to cut a sibling from main and the builder rebuilt reviewed
            # work. Bound seats know the item; resume THAT branch, merged with the
            # wave base. Unbound seats still take salvage, then a fresh cut.
            bounced = wt_mod.bounce_branch_of(
                (items or {}).get(seat.item) if seat.item else None
            )
            if bounced and bounced not in taken:
                slot = bounced.rsplit("-", 1)[-1] or slot
                agent_slot = f"{wave_name}-{slot}"
                try:
                    tree = wt_mod.resume_bounced(
                        repo, workspace / f"{wave_name}-{slot}-bounce", bounced,
                        base=base,
                    )
                    wave.resumed.append(bounced)
                    taken.add(bounced)
                except wt_mod.ResumeFailed as exc:
                    wave.resume_misses.append(str(exc))
                    observe.emit("resume_miss", detail=str(exc), branch=bounced)
            if tree is None:
                # Bound seats resume only the salvage that names their item. An unbound
                # seat still walks the list, skipping branches this wave already took.
                orphan = None
                while True:
                    candidate = wt_mod.take_resume(resumes, seat.item)
                    if candidate is None:
                        break
                    if candidate.branch in taken:
                        if seat.item:
                            break
                        continue
                    orphan = candidate
                    break
                if orphan is not None:
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
                except wt_mod.BranchExists as exc:
                    taken.add(branch)
                    if isinstance(exc, wt_mod.RemoteBranchTaken):
                        wave.remote_taken.append(str(exc))
                    # The pre-check above caps a local collision at 1000. A remote
                    # that already has the name raises here instead, and an uncapped
                    # continue would spin for as long as every name is taken.
                    if slot_n > 1000:
                        raise wt_mod.BranchExists(
                            f"no free gb/{wave_name}-* slot under 1000"
                        )
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
            # BEFORE the child starts, so the base is on the record before anything it does
            # can attest against it (GRPH-970).
            _record_cut_from(wave, client, seat.item, tree.branch, tree.base)
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
    base_branch: str = "",
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
    # Before the reap, so a child whose item is already done does not get another tick
    # in which to push. The reap that follows still salvages what it left; the push of
    # that salvage is the guarded one (GRPH-1053).
    _stop_landed_children(wave, children, client)
    # Reap first so the exit post can carry the diff shape computed against the base
    # after salvage. Reporting first would store a null shape that a later post can
    # fill, but the ordinary path should not need two posts to say what the child did.
    #
    # Safe for the spend reading below (GRPH-834), whose own comment used to claim the
    # opposite: `stdout_text` reads `<workspace>/logs/<slot>/stdout.log`, and `reap`
    # removes `<workspace>/<slot>` — the worktree, a SIBLING of the log directory rather
    # than its parent. Measured on a real wave: both children's logs were still readable
    # after their worktrees were gone.
    _reap_exited(wave, children, client, base_branch=base_branch)
    # And then publish for the children the reap skipped because they are STILL ALIVE
    # (GRPH-987). A bound seat keeps running after it moves its item to review, so waiting for
    # the reap left the branch local for the rest of that run — long enough for a reviewer in
    # any other checkout to be handed the item and bounce it for a reason that is not the diff.
    # A bound_only seat (GRPH-1089) is told to exit at that moment instead of reviewing. This
    # publish still has to land before that exit: the flag changes what the child does next,
    # not whether its branch is on the remote when a reviewer arrives.
    #
    # The order is not cosmetic. `_reap_exited` owns a child that has EXITED and does the fuller
    # job — salvage, touchpoints, receipts — while `_reap_all` at the end of a wave writes the
    # give-up line and the observe record only for children it reaped itself. Giving this the
    # first turn put three local git calls ahead of the reap, which was enough wall time for a
    # child to die mid-tick and be taken by the wrong one of the two: fifteen tests lost their
    # records. A child that exits during this tick belongs to the reap, exactly as it did before.
    _publish_in_review(wave, children, client)
    _report_exits(children, client, wave)
    if persist is not None:
        persist()


def _release_held_items(wave: Wave, child: Child, client: Graphban | None) -> None:
    """Release items the child held so `choose_resume` can pick up the salvage branch (GRPH-850).

    A child that dies (wall_clock / reap / stop) leaves its items `in_progress` / `claimed_by`
    set to the dead agent. `choose_resume` skips `in_progress` unconditionally, so the salvage
    branch sits orphaned while a fresh spawn cuts from main — losing the work just salvaged.

    Called from `_reap_exited` after the git salvage, so the branch is committed before the
    ledger row is freed. `until`'s planner client and mcp/`up`'s `SPAWN_READS` both include
    `release_item`. A pure `ALLOWED_TOOLS` supervisor client does not, and the call is
    skipped — the row stays stuck until `fleet_status` calls `requeue_offline_items`.

    Idempotent: a second call finds nothing to release. A failed release is reported, never
    fatal — the salvage is already committed, and the row will be freed by the offline requeue.
    """
    if not client or not child.held_items:
        return
    if "release_item" not in client.allowed:
        return
    agent_id = child.agent_id
    if not agent_id:
        return
    for item_id in child.held_items:
        # GRPH-949: read the lease BEFORE releasing it. `held_items` is what the roster said
        # at registration; by reap the child may have handed the item to review, or the
        # lease lapsed and a sibling claimed it. Releasing someone else's lease is refused by
        # the server, and one wave logged seven FAILED lines for leases that had simply
        # moved on — noise that buried the real failures beside them.
        holder = _lease_holder(client, item_id)
        if holder is not None and holder != agent_id:
            _note_lease_moved(wave, item_id, agent_id, holder)
            continue
        try:
            client.call("release_item", id=item_id, agent_id=agent_id)
        except ToolFailed as exc:
            # The read above can be unavailable (no `get_item_details` on this client) or
            # stale by a heartbeat. The server's own refusal says the same thing it would
            # have: the lease is not ours, which is information, not a failure.
            if "not the lease holder" in str(exc):
                _note_lease_moved(wave, item_id, agent_id, "")
            else:
                wave.failures.append(f"{child.branch}: release_item {item_id} failed ({exc})")
        except (NotPermitted, ServerUnreachable) as exc:
            wave.failures.append(f"{child.branch}: release_item {item_id} failed ({exc})")


def _lease_holder(client: Graphban, item_id: str) -> str | None:
    """Who holds `item_id` now: an agent id, "" for nobody, None when this client cannot say."""
    if "get_item_details" not in client.allowed:
        return None
    try:
        got = client.call("get_item_details", id=item_id)
    except (NotPermitted, ToolFailed, ServerUnreachable):
        return None
    if not isinstance(got, dict):
        return None
    return str(got.get("claimed_by") or "")


def _note_lease_moved(wave: Wave, item_id: str, agent_id: str, holder: str) -> None:
    """Record, once per item, that a reaped child no longer held what it registered with."""
    if any(line.startswith(f"{item_id}:") for line in wave.lease_moved):
        return
    where = f"held by {holder}" if holder else "no longer held by it"
    wave.lease_moved.append(f"{item_id}: not released for {agent_id}; lease {where}")


def _reap_exited(wave: Wave, children: list[Child], client: Graphban | None = None,
                 *, base_branch: str = "") -> None:
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
            held = _salvage_items(child)
            reaped = wt_mod.reap_held(tree, child.adapter, held)
        except Exception as exc:  # noqa: BLE001 — a failed reap is reported, never fatal
            wave.failures.append(f"{child.branch}: reap failed ({exc})")
            continue
        wave.reaped.append(reaped)
        child.diff_shape = reaped.diff_shape
        # GRPH-850: release the child's held items so `choose_resume` can pick up the
        # salvage branch. Without this, the row stays `in_progress` / `claimed_by=<dead>`
        # and a fresh spawn cuts from main, losing the work just salvaged. mcp/`up` pass
        # SPAWN_READS (includes release_item); until passes the planner client.
        _release_held_items(wave, child, client)
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
        _note_touchpoints(wave, child, client)
        _note_staleness(wave, tree)
        salvaged = reaped.disposition is wt_mod.Disposition.SALVAGED
        _publish(wave, tree, client=client, child=child,
                 propose_prs=not salvaged, base_branch=base_branch)
        if salvaged:
            commit = reaped.salvage.commit if reaped.salvage else ""
            target, why = wave.diverted.get(tree.branch, (tree.branch, ""))
            _note_salvage(wave, client, tree.repo, target, held, reaped.siblings,
                          commit or "", base=tree.base, reason=why)


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
    current. Rebasing somebody's work is not the supervisor's call. Since GRPH-949 a branch
    that is behind is still pushed; since GRPH-1087 it is proposed unless the missed commits
    share a file with it or `git merge-tree --write-tree` conflicts (`_classify_behind`).
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
        _classify_behind(wave, tree, behind, ref)


def _classify_behind(wave: Wave, tree: Worktree, behind: int, ref: str) -> None:
    """Whether being `behind` commits behind `ref` should withhold the PR (GRPH-1087).

    The count in `wave.stale` stays either way — the BEHIND line is how far the trunk
    moved, and that fact does not go away because the merge is clean. What changes is
    the proposal. Refuse when either check says the behind-ness matters:

    1. the missed commits (`base`..`ref`) change a file the branch also changes, or
    2. `git merge-tree --write-tree ref branch` reports a conflict.

    Otherwise record "N behind ref, no shared files, merges clean" for the PR body and
    the BEHIND line. A check that cannot be asked is a refusal, not a clean note: an
    empty file list we failed to compute is how "1 behind, overlapping" would be proposed.
    """
    shared = wt_mod.shared_with_missed(tree.repo, tree.base, ref, tree.branch)
    if shared is None:
        wave.behind_block[tree.branch] = (
            f"{behind} commit(s) behind {ref}; could not list the files those "
            "commits change, so not proposing"
        )
        return
    conflicts = wt_mod.merge_tree_conflicts(tree.repo, ref, tree.branch)
    if conflicts is None:
        wave.behind_block[tree.branch] = (
            f"{behind} commit(s) behind {ref}; merge-tree could not be asked, "
            "so not proposing"
        )
        return
    if conflicts:
        wave.behind_block[tree.branch] = (
            f"{behind} commit(s) behind {ref}; merge-tree conflicts"
        )
        return
    if shared:
        shown = ", ".join(shared[:5]) + (
            f" (+{len(shared) - 5} more)" if len(shared) > 5 else ""
        )
        wave.behind_block[tree.branch] = (
            f"{behind} commit(s) behind {ref}; rebase before proposing — "
            f"missed commits also change {shown}"
        )
        return
    wave.behind_clean[tree.branch] = (
        f"{behind} behind {ref}, no shared files, merges clean"
    )


def _note_touchpoints(wave: Wave, child: Child, client: Graphban | None = None) -> None:
    """Compare what this child CHANGED against what its items DECLARE (GRPH-785).

    The declaration is RE-READ here, per item on the branch (`_branch_items`, GRPH-1079), with
    `get_item_details` (GRPH-1025).
    Comparing against the delegation-time snapshot punished a child for widening its own
    declaration: `update_item(touchpoints=...)` unions into the item, the snapshot never saw
    it, and on wave p48c every branch was refused — one on its own declared test files. The
    snapshot is the fallback when the read fails, and the branch is then named in
    `declared_unread` so the report says which operand was used: an unreadable item must not
    turn silently into either a pass or a refusal with no reason.

    Known consequence, accepted: a child that writes its measured paths back before reap
    (gbagent's `coord.record_measured` does, and the worker instruction's DECLARE asks every
    child to) can never trip this. That is fine. Once a path is on the item the server's
    partition sees it, and `touchpoints.overlaps` — which needs no declaration at all — still
    catches two branches changing one file. What this check is left to catch is the case it
    exists for: a child that changed a file nobody declared and did not say so.

    Both findings are recorded on the wave and printed by `report`. The supervisor writes
    nothing to the ledger with them — what "collides" means belongs to the server — but an
    UNDECLARED branch is not proposed as a PR (GRPH-949, `_refuse_proposal`).
    """
    measured = wave.touched.get(child.branch) or []
    if not measured:
        return
    # Before the comparison, so an older roster's missing history is part of the operand
    # and not a note written after the branch was already judged against too little.
    history = _restore_authored(child, client)
    declared: list[str] = []
    unread: list[str] = []
    if history:
        unread.append(history)
    for item_id in _branch_items(child):
        now, why = _declared_now(client, item_id)
        if now is None:
            unread.append(f"{item_id}: {why}")
            now = list(wave.declared.get(item_id) or [])
        declared += now
    if unread:
        wave.declared_unread[child.branch] = "; ".join(unread)
    if declared:
        missing = tp_mod.undeclared(measured, declared)
        if missing:
            wave.undeclared[child.branch] = missing
    # Recomputed over every branch measured so far, so the last reap holds the whole wave's
    # answer — a pairwise check at each reap would miss the pair reaped either side of it.
    wave.collided = tp_mod.overlaps(wave.touched)


def _restore_authored(child, client) -> str:
    """Fill `ever_held` from the ledger when the roster never recorded it (GRPH-1085).

    An older children file has no `ever_held`. The live roster only knows what the child
    holds now, so items it already handed to review are gone from that reading and the
    branch is judged against too few declarations. Those items name this agent as
    `built_by`. That read is the fallback, and only the fallback: a roster that recorded
    the list, even an empty one, is the history and is not second-guessed.

    Returns "" when the history is known. A non-empty string is why it is not, for the
    caller to record — an unreadable authorship must not look like a child that held
    only what it holds now.
    """
    if getattr(child, "ever_held_known", True):
        return ""
    agent = str(getattr(child, "agent_id", None) or "").strip()
    authored, why = _authored_by(client, agent)
    if authored is None:
        return f"ever_held was not in the roster and authorship could not be read ({why})"
    seen = getattr(child, "ever_held", None)
    if not isinstance(seen, list):
        child.ever_held = []
        seen = child.ever_held
    for item_id in authored:
        if item_id not in seen:
            seen.append(item_id)
    child.ever_held_known = True
    return ""


def _authored_by(client, agent_id: str) -> tuple[list[str] | None, str]:
    """Item ids whose `built_by` is `agent_id`, or (None, why that list was not read).

    `[]` means the listing was read and this agent authored nothing in it. None means
    it was not read, or it does not report authorship. A lean page carries no `built_by`,
    and a missing field is not "nobody built this".
    """
    if not agent_id:
        return None, "the adopted child has no agent id"
    if client is None:
        return None, "no client to read items with"
    allowed = getattr(client, "allowed", None)
    if allowed is not None and "search_items" not in allowed:
        return None, "this client may not list items"
    try:
        payload = client.call("search_items", fields="full", limit=10_000)
    except Exception as exc:  # noqa: BLE001 — any failed read is reported, not treated as none
        return None, str(exc) or type(exc).__name__
    if not isinstance(payload, dict):
        return None, "item listing was not a record"
    if payload.get("has_more"):
        return None, "item listing was truncated"
    fields = payload.get("fields")
    if isinstance(fields, list) and "built_by" not in fields:
        return None, "the listing does not report built_by"
    rows = payload.get("results")
    if not isinstance(rows, list):
        return None, "item listing carried no results list"
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or "built_by" not in row:
            return None, "a row did not report built_by"
        if str(row.get("built_by") or "") != agent_id:
            continue
        item_id = str(row.get("id") or "").strip()
        if item_id and item_id not in out:
            out.append(item_id)
    return out, ""


def _branch_items(child) -> list[str]:
    """Every item whose work can be on this child's branch, bound item first (GRPH-1079).

    Not `held_items` alone: that is the roster's LAST non-empty reading. A bound child builds
    X, moves it to `review` (so it no longer holds X), and keeps claiming — at reap it holds
    Y and Z, and X's own files were compared against Y's and Z's declarations and refused.
    So: the seat's bound item, every item the roster ever read it holding, and what it holds
    now. Not live roster holdings either: `_roster` is overwritten after `release_item`, so
    at reap the live view is empty.

    `getattr` because a Child rebuilt by hand may carry only `held_items`. `adopt`
    restores `ever_held` from the roster; a file that predates the field is filled by
    `_restore_authored` before this runs, from items this agent authored.
    """
    ids: list[str] = []
    assigned = getattr(child, "assigned", None)
    if isinstance(assigned, dict):
        ids.append(str(assigned.get("item") or "").strip())
    ids += [str(i) for i in (getattr(child, "ever_held", None) or [])]
    ids += [str(i) for i in (child.held_items or [])]
    out: list[str] = []
    for i in ids:
        if i and i not in out:
            out.append(i)
    return out


def _declared_now(client: Graphban | None, item_id: str) -> tuple[list[str] | None, str]:
    """`item_id`'s touchpoints as the ledger holds them now, or (None, why it could not say).

    `[]` is an answer — the item was read and declares nothing — and None is not, which is the
    whole point of returning both: the caller falls back to the snapshot only on None.
    """
    if client is None:
        return None, "no client to read the item with"
    allowed = getattr(client, "allowed", None)
    if allowed is not None and "get_item_details" not in allowed:
        return None, "this client may not read items"
    try:
        got = client.call("get_item_details", id=item_id)
    except Exception as exc:  # noqa: BLE001 — any failed read falls back, and says so
        return None, str(exc) or type(exc).__name__
    if not isinstance(got, dict):
        return None, "item details were not a record"
    return [str(p) for p in (got.get("touchpoints") or [])], ""


#: What `exit_meaning` says when this supervisor saw an item in `review` whose branch it could
#: not make readable (GRPH-987). A bounce for "not on origin" is not a verdict on the diff, but
#: it is recorded as one against the builder's vendor and model, and the builder's own attempt
#: row is the one place this process can say so.
REVIEW_UNREADABLE = "review unreadable"


def _ids_in_review(client: Graphban | None) -> set[str] | None:
    """Every item id the ledger reads `review`, or None when it could not be asked.

    None is NOT the empty set, and the difference is the whole point: both decide "publish
    nothing" here, but only one is a fact about the board. A supervisor whose client may not
    read items, or whose server stopped answering, would otherwise report a wave in which
    nothing was ever awaiting review.

    `fields="lean"` on purpose. This runs on every watch tick beside `_roster`, and the only
    thing the early publish needs is the id — `until` already reads the full review rows for
    its own purposes and this is not that read.
    """
    if client is None or "search_items" not in client.allowed:
        return None
    try:
        payload = client.call("search_items", status="review", fields="lean", limit=10_000)
    except (NotPermitted, ToolFailed, ServerUnreachable, ProtocolError):
        # All four of `client`'s errors, because this runs on every watch tick: a reply this
        # process cannot parse must cost one observation, not the wave.
        return None
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    return {str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")}


def _salvage_items(child: Child) -> list[str]:
    """The items this child's salvage belongs to, originating item first (GRPH-1053).

    A bound seat's `assigned.item` is the item the seat was minted for. `held_items` is
    what the roster last said, which can be a cluster, empty, or a neighbour's id that
    the seat happened to be holding. The worker branch is that originating item's. The
    others keep the sibling branches `reap_held` already cuts, and they are not renamed
    onto the first one.
    """
    held = [str(i) for i in (child.held_items or []) if i]
    origin = ""
    assigned = child.assigned if isinstance(child.assigned, dict) else None
    if assigned is not None:
        origin = str(assigned.get("item") or "").strip()
    if not origin:
        return held
    return [origin, *[i for i in held if i != origin]]


def _remember_landed_unread(wave: Wave, item: str, reason: str) -> None:
    """One line per wave. A push that could not be checked is not "nothing had landed"."""
    if wave.landed_unmeasured or not reason:
        return
    wave.landed_unmeasured = (
        f"{item}: landed-commit check did not run ({reason})" if item else reason
    )


def _landed_commits(repo: Path, client: Graphban | None,
                    item_id: str) -> tuple[list[str], str]:
    """Commits of `item_id` that are already in the trunk, and why that could not be read.

    `([], "")` is a real answer: the item was read, or there is no trunk to be in, and
    nothing of it has landed. A non-empty reason means the list is not that answer.
    The caller must not hand it to `salvage_push_target` as an empty list. An empty
    list is what an unfetched sha becomes after a read that did happen (`reaches`
    returns None and the sha is left out). A refused read is not that, and treating
    it as one pushes a rework onto the branch a fix already uses.

    Only an evidence entry that carries a `commit` counts. A cut-from note names a sha
    in prose and is not a landed fix. An entry that names a different branch is someone
    else's attestation. A commit this clone has never fetched is not "missing": `reaches`
    returns None and it is left out, so a fresh clone does not divert every salvage.
    """
    if client is None or not item_id:
        return [], ""
    allowed = getattr(client, "allowed", None)
    if allowed is not None and "get_item_details" not in allowed:
        return [], "this client may not read items"
    try:
        got = client.call("get_item_details", id=item_id)
    except NotPermitted:
        return [], "not permitted to read the item"
    except (ToolFailed, ServerUnreachable, ProtocolError) as exc:
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001 — a stand-in client, or a failure this list does not name
        return [], str(exc)
    if not isinstance(got, dict):
        return [], "item details were not a record"
    remote = wt_mod.remote_for(repo)
    trunk = wt_mod.default_ref(repo, remote) if remote else ""
    if not trunk:
        return [], ""
    branch = str(got.get("branch") or "").strip()
    evidence = got.get("evidence") or []
    if not isinstance(evidence, list):
        return [], ""
    seen: list[str] = []
    landed: list[str] = []
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        commit = str(entry.get("commit") or "").strip()
        if not commit or commit in seen:
            continue
        named = str(entry.get("branch") or "").strip()
        if named and branch and named != branch:
            continue
        seen.append(commit)
        if wt_mod.reaches(repo, trunk, commit) is True:
            landed.append(commit)
    return landed, ""


def _push_target(wave: Wave, repo: Path, branch: str, base: str, item: str,
                 client: Graphban | None) -> tuple[str, str]:
    """Where `branch` may be pushed. An empty reason means `branch` itself is safe.

    A refused or unreachable `get_item_details` is not "nothing has landed". Passing
    `landed=[]` in that case is the path an unfetched sha takes, and it is how `up`
    pushed a non-salvage rework onto the branch the fix already uses (GRPH-1053).
    `None` is the other argument: the read did not happen.
    """
    if not item:
        return branch, ""
    landed, unread = _landed_commits(repo, client, item)
    if unread:
        _remember_landed_unread(wave, item, unread)
        target, reason = wt_mod.salvage_push_target(repo, branch, base, None, item=item)
        # In `failures` only when the push was turned aside. An ordinary publish that
        # could not be checked is `LANDED unmeasured` in the report, not a failed wave.
        # The dangerous one — a tip that would move a ref already in the trunk — is
        # both, because that is the push that overwrites a landed fix.
        if reason and wave.landed_unmeasured and wave.landed_unmeasured not in wave.failures:
            wave.failures.append(wave.landed_unmeasured)
        return target, reason
    return wt_mod.salvage_push_target(repo, branch, base, landed, item=item)


def _ids_with_status(client: Graphban | None, status: str) -> set[str] | None:
    """Item ids the ledger reads as `status`, or None when it could not be asked.

    None is not the empty set. The fake that ignores the status argument and always
    returns `review` is filtered here, so a child in review is not treated as done.
    """
    if client is None or "search_items" not in getattr(client, "allowed", ()):
        return None
    try:
        payload = client.call("search_items", status=status, fields="lean", limit=10_000)
    except (NotPermitted, ToolFailed, ServerUnreachable, ProtocolError):
        return None
    except Exception:  # noqa: BLE001 — one unread tick, not a dead wave
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("results") or []
    if not isinstance(rows, list):
        return None
    return {
        str(row["id"]) for row in rows
        if isinstance(row, dict) and row.get("id") and row.get("status") == status
    }


def _stop_landed_children(wave: Wave, children: list[Child],
                          client: Graphban | None) -> None:
    """Stop a running child whose originating item is already `done` (GRPH-1053).

    `review` is not this. The child stays up to publish the revision (GRPH-987) and to
    review other work (PRD-39). A push that would move a branch past a commit already
    in the trunk is refused by the push guard, not by killing the process.

    Unreadable is not "nothing is done". The child is left running and the wave says
    the check did not run.
    """
    running = [child for child in children if child.running]
    if not running:
        return
    done = _ids_with_status(client, "done")
    if done is None:
        _remember_landed_unread(
            wave, "",
            "could not read which items are done, so a child whose fix has landed "
            "was not stopped",
        )
        return
    if not done:
        return
    for child in running:
        ids = _salvage_items(child)
        hit = [i for i in ids if i in done]
        if not hit:
            continue
        stop(child, Reason.ITEM_LANDED)
        wave.failures.append(
            f"{child.branch}: stopped; {hit[0]} is done and a later push would "
            "move its branch"
        )


def _publish_in_review(wave: Wave, children: Sequence[Child],
                       client: Graphban | None) -> None:
    """Push a RUNNING child's branch the moment one of its items reaches `review` (GRPH-987).

    `_publish` runs at reap, and reap runs when the child EXITS. A bound seat does not exit
    when it finishes building: it is told to review other work in the same process (PRD-39
    D-h), so the branch it committed stayed local for the rest of that run while its item sat
    in `review` advertising it. Wave p47f lost exactly that race — `gb/p47f-1` at `63b46358`
    was bounced by a cursor-agent reviewer for "not on origin", and minutes later
    `origin/gb/p47f-1` was the same sha and the reviewer's own clone fetched it fine.

    The item's status is the signal, not the existence of commits. Pushing a branch a child is
    still building would publish half-work; a child says it is done by moving the item, and
    that is what this waits for.

    **A push here is not a proposal.** `_propose` stays at reap because the refusals that
    decide whether a branch may be proposed at all — UNDECLARED files (GRPH-949) and a base
    the trunk has moved past (GRPH-786) — are measured there, and neither measurement exists
    yet. An early push puts the work where a reviewer can read it and asks nobody to merge it.

    Cheap by construction: the ledger is not asked until some running child has a commit beyond
    its base that this wave has not already published, so an idle tick costs two local git calls
    and no network.
    """
    pending = [c for c in children
               if c.running and c.base and c.branch and c.worktree is not None]
    if not pending:
        return
    unpublished = []
    for child in pending:
        try:
            repo = _repo_of(child)
            # Nothing beyond the base is nothing to publish, so it is also nothing to ask the
            # ledger about. Without this gate a wave of freshly cut children read the review
            # queue on every poll interval and found nothing to do with it.
            if not wt_mod.commits_beyond_base(repo, child.branch, child.base):
                continue
            head = _branch_head(repo, child.branch)
        except Exception as exc:  # noqa: BLE001 — reported, never fatal to the watch loop
            _note_once(wave, f"{child.branch}: could not resolve its tree ({exc})")
            continue
        if not head or wave.review_publish_tried.get(child.branch) == head:
            continue
        unpublished.append((child, repo, head))
    if not unpublished:
        return

    in_review = _ids_in_review(client)
    if in_review is None:
        wave.review_unmeasured = ("this supervisor may not read items, or the server did not "
                                  "answer, so an item awaiting review could not be published "
                                  "early")
        return
    if not in_review:
        return

    for child, repo, head in unpublished:
        items = [i for i in (child.held_items or []) if i and i in in_review]
        if not items:
            continue
        wave.review_publish_tried[child.branch] = head
        origin = _salvage_items(child)
        item = next((i for i in origin if i in items), items[0])
        try:
            target, divert_reason = _push_target(
                wave, repo, child.branch, child.base, item, client)
        except wt_mod.GitError as exc:
            _note_once(wave, f"{child.branch}: not pushed ({exc})")
            wave.review_unreadable[child.branch] = list(items)
            continue
        if divert_reason:
            # The original ref is the one a landed fix already uses. Pushing this tip
            # onto it moves that ref. The rework goes to a side branch, which is not a
            # proposal and does not release the item: a reviewer fetching the original
            # must still see the landed commit.
            try:
                pushed = wt_mod.push_branch(repo, target, child.base)
            except Exception as exc:  # noqa: BLE001 — a failed publish is reported, never fatal
                _note_once(wave, f"{child.branch}: publish on review failed ({exc})")
                wave.review_unreadable[child.branch] = list(items)
                continue
            wave.published[target] = pushed
            wave.diverted[child.branch] = (target, divert_reason)
            if pushed.ok:
                _record_salvage_receipt(
                    wave, client, [item], target, _branch_head(repo, target),
                    reason=divert_reason)
                _note_once(wave, f"{child.branch}: diverted to {target} ({divert_reason})")
            else:
                wave.review_unreadable[child.branch] = list(items)
                if not pushed.skipped:
                    _note_once(wave, f"{child.branch}: {pushed.reason}")
            continue
        try:
            pushed = wt_mod.push_branch(repo, child.branch, child.base)
        except Exception as exc:  # noqa: BLE001 — a failed publish is reported, never fatal
            _note_once(wave, f"{child.branch}: publish on review failed ({exc})")
            wave.review_unreadable[child.branch] = list(items)
            continue
        wave.published[child.branch] = pushed
        if pushed.ok:
            wave.published_in_review[child.branch] = head
            wave.review_unreadable.pop(child.branch, None)
            # The release, and only on a real push: `publish_pending` withholds the item from
            # `claim_review` until this lands, so posting it for a branch that did not reach
            # the remote would hand a reviewer exactly the 404 it exists to prevent.
            if client is not None and child.seat_id:
                client.post_attempt(enrolment_id=child.seat_id, branch_published=True)
        else:
            # `skipped` is a real outcome and not a failure — a branch at its base has nothing
            # to publish, and there is no remote is the honest answer where there is none. Both
            # still leave an item in `review` that only this machine can read, which is the
            # exposure worth recording and is not the same claim as a clean publish.
            wave.review_unreadable[child.branch] = list(items)
            if not pushed.skipped:
                _note_once(wave, f"{child.branch}: {pushed.reason}")


def _note_once(wave: Wave, line: str) -> None:
    """Append a failure line unless this BRANCH already has one.

    Deduped per branch rather than per message, and that is a deliberate loss: the watch tick
    runs every poll interval and these conditions do not change between ticks, so an unguarded
    append turns one refused push into a page of identical FAILED lines — which is how the real
    failure beside them gets buried. `_note_lease_moved` dedupes per item for the same reason.
    A second, different complaint about one branch's publish waits for the report.
    """
    branch = line.split(":", 1)[0]
    if any(f.startswith(f"{branch}:") for f in wave.failures):
        return
    wave.failures.append(line)


def _publish(wave: Wave, tree: Worktree, *, client: Graphban | None = None,
             child: Child | None = None, propose_prs: bool = True,
             base_branch: str = "") -> str:
    """Put the child's branch where the reviewer can read it (GRPH-750).

    Returns the ref that was pushed — `tree.branch`, or a side branch when pushing the
    original would move a commit that is already in the trunk (GRPH-1053). "" when the
    push did not happen. The caller receipts a salvage onto that ref, not onto every
    item the child held.

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
    target = tree.branch
    divert_reason = ""
    item = ""
    if child is not None:
        ids = _salvage_items(child)
        item = ids[0] if ids else ""
    if item:
        try:
            target, divert_reason = _push_target(
                wave, tree.repo, tree.branch, tree.base, item, client)
        except wt_mod.GitError as exc:
            wave.failures.append(f"{tree.branch}: not pushed ({exc})")
            return ""
    try:
        pushed = wt_mod.push_branch(tree.repo, target, tree.base)
    except Exception as exc:  # noqa: BLE001 — a failed publish is reported, never fatal
        wave.failures.append(f"{target}: publish failed ({exc})")
        return ""
    wave.published[target] = pushed
    if not pushed.ok and not pushed.skipped:
        wave.failures.append(f"{target}: {pushed.reason}")
    # A diverted push is not the branch the seat advertised, and it is not a PR. Saying
    # "published" about the original ref would hand a reviewer a tip that moved past the
    # fix, which is the push this guard exists to refuse.
    if divert_reason:
        wave.diverted[tree.branch] = (target, divert_reason)
        wave.failures.append(f"{tree.branch}: diverted to {target} ({divert_reason})")
        if propose_prs and item:
            _record_salvage_receipt(
                wave, client, [item], target, _branch_head(tree.repo, target),
                reason=divert_reason)
        return target
    # GRPH-754: tell the server the work is now readable. Until this lands the server withholds
    # the item from `claim_review`, so a reviewer can no longer be handed an item whose branch
    # it will fetch a 404 for — measured on the deployed instance, twice. Only on a real push:
    # saying "published" about a branch that skipped would re-open the window it closes.
    if pushed.ok and client is not None and child is not None and child.seat_id:
        client.post_attempt(enrolment_id=child.seat_id, branch_published=True)
    if pushed.ok and propose_prs:
        _propose(wave, tree, client=client, child=child, base_override=base_branch)
    return target if pushed.ok else ""


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


def _branch_head(repo: Path, branch: str) -> str:
    try:
        return wt_mod._git(repo, "rev-parse", branch).strip()
    except Exception:  # noqa: BLE001 — receipt is best-effort
        return ""


def _observe(wave: Wave, client: Graphban | None, item_id: str, failure: str,
             **payload) -> None:
    """Post one fact to the observations route (GRPH-975). Never fatal.

    The supervisor client may not call `update_item` (PRD-22 §4). These receipts used to,
    and every wave logged `not recorded` while `sign_off` reported the base uncompared.
    `post_observation` raises on a refusal — telemetry's post swallows one, and a swallowed
    base is the defect — so this except is what puts the reason on the wave. There is no
    fallback call: a fallback would be the forbidden tool, and the test that drives the
    real client would stay green while production kept failing.
    """
    if client is None or not item_id:
        return
    try:
        client.post_observation(item_id=item_id, **payload)
    except Exception as exc:  # noqa: BLE001 — a wave is not broken by a missing receipt
        wave.failures.append(f"{item_id}: {failure} ({exc})")


def _record_salvage_receipt(wave: Wave, client: Graphban | None, items: list[str],
                            branch: str, commit: str, *, reason: str = "") -> None:
    """Name the salvage on the item without opening a PR (GRPH-926).

    GRPH-830 still holds: the work must be findable from the item. A `/pull/` URL was
    the wrong receipt — those drafts were mergeable and titled `WIP: salvaged by gbfleet`.
    `reason` is set when the push went to a side branch so the original ref was not moved.

    The server composes the note. This sends the branch, the commit and the reason.
    """
    if client is None or not items:
        return
    for item_id in items:
        _observe(wave, client, item_id, "salvage not recorded",
                 kind="salvage", branch=branch, commit=commit or None,
                 reason=reason or None)


def _record_cut_from(wave: Wave, client, item_id: str | None, branch: str, base: str) -> None:
    """Tell the ledger which commit this branch started at (GRPH-970).

    The one fact about a child that the child is the worst possible source for. A reviewer
    that attests the base of its own branch has vouched for a tree containing none of the
    item's work, and on the p47-ui wave one did exactly that: GRPH-954 reached `done` on a
    sign_off naming `7ea90ebe`, its own base, with every gate predicate passing and no commit
    for the item on any ref. `sign_off` refuses that now — but only if somebody recorded the
    base, and only the supervisor that cut the tree knows it.

    A NOTE, and not the attestation it wants to be. Writing `kind: attestation` needs the
    `gate` scope, and a supervisor runs on whatever `GBFLEET_API_KEY` holds — `["read",
    "write"]` on every wave measured here. The attestation would have been refused, the
    wave would have logged a failure nobody reads, and the gate would have found no base
    and reported "not compared" for ever. The server writes the note from the facts posted
    here (`fleet.record_observation`); this client still may not call `update_item`.
    `items.CUT_FROM_MARKER` is the other half of the pair.

    Never fatal. A wave is not broken by a missing receipt — but unlike the salvage note, the
    absence is visible: `sign_off` reports that no base was compared rather than passing
    silently, so a lost receipt degrades to the old behaviour and says so.
    """
    if client is None or not item_id or not base:
        return
    _observe(wave, client, item_id, "base not recorded",
             kind="cut_from", branch=branch, base=base)


def _refuse_proposal(wave: Wave, branch: str) -> str:
    """Why this branch must not be proposed as a PR, or "" (GRPH-949, GRPH-1087).

    UNDECLARED and BEHIND were recorded and printed and never acted on, so one wave proposed
    a branch that changed nine undeclared files (App.tsx among them) and another three
    commits behind the trunk. A PR is a request to merge; asking for one on a branch whose
    partition was wrong hands the reviewer a question the supervisor already had the answer
    to. Rebasing is not the supervisor's call.

    Being behind is not itself that answer (GRPH-1087). A trunk that landed unrelated
    commits during the build merges clean and shares no files, and refusing it made the
    guard mostly refuse work that needed no rebase. Refuse the behind-ness only when
    `_classify_behind` recorded a block, or when staleness was recorded and nobody
    classified it — an unclassified stale mark is not a clean merge.
    """
    reasons = []
    missing = wave.undeclared.get(branch) or []
    if missing:
        shown = ", ".join(missing[:5]) + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
        reasons.append(f"changed file(s) outside every declared touchpoint: {shown}")
    stale = wave.stale.get(branch)
    if stale and branch not in wave.behind_clean:
        block = wave.behind_block.get(branch)
        if block:
            reasons.append(block)
        else:
            behind, ref = stale
            reasons.append(f"{behind} commit(s) behind {ref}; rebase before proposing")
    return "; ".join(reasons)


def propose_branch(wave: Wave, repo: Path, branch: str, items: list[str], *,
                   client: Graphban | None,
                   base_override: str = "") -> None:
    """The branch, the items it served, and a draft PR joining them.

    Split out of `_propose` for the salvage path (GRPH-830), which has a repo, a branch and a
    list of items but no `Worktree` — by the time a takeover runs, the tree is gone — and no
    `Child`, because the process it belonged to is dead.

    `base_override` (GRPH-847) targets the PR at a non-default base (e.g. an integration
    branch for stacked slices). Empty means the remote's default ref.

    A salvage subject is not proposed (GRPH-926). Push already happened; the item gets a
    note naming the commit. Opening a PR here is how `WIP: salvaged by gbfleet` drafts
    became mergeable.
    """
    remote = wt_mod.remote_for(repo)
    base = base_override or (wt_mod.default_ref(repo, remote) if remote else "")
    subj = propose_mod.subject(repo, branch, base)
    if wt_mod.is_salvage_subject(subj):
        wave.proposed[branch] = propose_mod.Proposed(
            branch=branch, skipped=True,
            reason="salvage commit is not proposed as a PR")
        for n, item in enumerate(items):
            # GRPH-948: every item after the first was salvaged onto a branch of its own by
            # `reap_held`, and that is the branch its receipt names and its resume reads.
            own = wt_mod.item_branch(branch, item)
            if n and wt_mod.branch_exists(repo, own):
                _publish_item_branch(wave, repo, own, base)
            else:
                own = branch
            _record_salvage_receipt(wave, client, [item], own, _branch_head(repo, own))
        return
    refused = _refuse_proposal(wave, branch)
    if refused:
        # Pushed already, so the work is readable; only the merge request is withheld.
        wave.proposed[branch] = propose_mod.Proposed(branch=branch, skipped=True, reason=refused)
        wave.unproposed[branch] = refused
        wave.failures.append(f"{branch}: not proposed — {refused}")
        _record_withheld(wave, client, items, branch, refused)
        return
    if len(items) > 1:
        _propose_per_item(wave, repo, branch, items, base, client=client)
        return
    _open_pr(wave, repo, branch, items, base, subj, client=client)


def _open_pr(wave: Wave, repo: Path, branch: str, items: list[str], base: str, subj: str, *,
             client: Graphban | None, attest: bool = False) -> None:
    """One draft PR for one branch carrying at most one item, and the receipt on that item.

    `attest` writes `Attests: <item>` into the body. The per-item split asks for it; a
    single-item branch does not, and its body stays the one it had.
    """
    title, body = propose_mod.describe(branch, items, subj, attest=attest)
    note = wave.behind_clean.get(branch)
    if note:
        # The same sentence the BEHIND line prints (GRPH-1087). The reviewer reads the
        # PR, not the supervisor's stdout, so a clean-but-behind branch that says so
        # only in the log is a branch whose staleness the reviewer never sees.
        body = body.rstrip() + "\n\n" + note + "\n"
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
        # The server writes the url receipt and starts `pr_linked_at`. Sending the URL as
        # an evidence array would be `update_item`, which this client may not call.
        _observe(wave, client, item_id, "PR opened but not recorded",
                 kind="pr_opened", branch=branch, url=got.url)


def _record_withheld(wave: Wave, client: Graphban | None, items: list[str], branch: str,
                     reason: str) -> None:
    """Say on the ITEM that its branch was pushed and its PR withheld, and why (GRPH-1025).

    The reason used to exist only in the wave's end-of-run failures, so a whole wave went by
    with items reaching `done` and nothing on main, and the one line that explained it was
    invisible until the supervisor exited. The item is where a reviewer and a planner look.
    """
    if client is None or not items:
        return
    for item_id in items:
        _observe(wave, client, item_id, "withheld PR not recorded",
                 kind="withheld", branch=branch, reason=reason)


def _publish_item_branch(wave: Wave, repo: Path, name: str, base: str) -> bool:
    try:
        pushed = wt_mod.push_branch(repo, name, base)
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        wave.failures.append(f"{name}: publish failed ({exc})")
        return False
    wave.published[name] = pushed
    if not pushed.ok and not pushed.skipped:
        wave.failures.append(f"{name}: {pushed.reason}")
    return pushed.ok


def _refuse_mixed(wave: Wave, branch: str, items: list[str], why: str, *,
                  client: Graphban | None) -> None:
    """The branch stays pushed and no PR is opened. The reason names the commit when
    one commit is why (GRPH-1086)."""
    reason = (f"carries {len(items)} items ({', '.join(items)}) and {why}; "
              "one PR per item, so split it by hand")
    wave.proposed[branch] = propose_mod.Proposed(branch=branch, skipped=True, reason=reason)
    wave.unproposed[branch] = reason
    wave.failures.append(f"{branch}: not proposed — {reason}")
    _record_withheld(wave, client, items, branch, reason)


def _propose_per_item(wave: Wave, repo: Path, branch: str, items: list[str], base: str, *,
                      client: Graphban | None) -> None:
    """One branch and one PR per item, each cut from the base (GRPH-948, GRPH-1086).

    A cluster claim is a scheduling unit, not a delivery unit. The reported child built
    four items on one branch and the one PR it became carried all of them. Stacking the
    PRs still left the later branch containing the earlier item's commits, so one merge
    carried unreviewed work.

    Each item's own commits are replayed onto the base and proposed against the base.
    The body carries `Attests:` for that item alone. A commit that names no item, names
    two, or will not apply without another item's commit refuses the whole branch:
    opening the ones that did apply would merge part of a history that does not separate.
    """
    blocks, why = propose_mod.split_by_item(repo, branch, base, items)
    if why:
        _refuse_mixed(wave, branch, items, why, client=client)
        return
    cut: list[tuple[str, str, str]] = []
    for item, shas in blocks:
        sha, err = propose_mod.replay_onto(repo, base, shas)
        if err:
            _refuse_mixed(wave, branch, items, err, client=client)
            return
        name = wt_mod.item_branch(branch, item)
        if wt_mod.branch_exists(repo, name) and _branch_head(repo, name) != sha:
            # Never moved: an existing branch at another commit is somebody's work.
            _refuse_mixed(wave, branch, items, f"{name} exists at another commit", client=client)
            return
        cut.append((item, name, sha))
    for item, name, sha in cut:
        if wt_mod.branch_exists(repo, name):
            continue
        try:
            wt_mod._git(repo, "branch", name, sha)
        except wt_mod.GitError as exc:
            wave.failures.append(f"{name}: could not be cut ({exc}); {item} not proposed")
            return
    wave.proposed[branch] = propose_mod.Proposed(
        branch=branch, skipped=True,
        reason=f"split into {len(cut)} per-item branches, one PR each")
    for item, name, _sha in cut:
        if not _publish_item_branch(wave, repo, name, base):
            wave.failures.append(f"{name}: not published, so {item} was not proposed")
            return
        _open_pr(wave, repo, name, [item], base, propose_mod.subject(repo, name, base),
                 client=client, attest=True)


def _note_salvage(wave: Wave, client: Graphban | None, repo: Path, branch: str,
                  items: list[str], siblings, commit: str, *, base: str = "",
                  reason: str = "") -> None:
    """One receipt per item, on that item's own branch.

    Later items never inherit the first item's branch. A sibling that was not cut is a
    failure, not a fallback onto the primary — that fallback is how one salvage was
    attached to every id the child held (GRPH-1053).
    """
    if not items:
        return
    sibling = {item: name for item, name in (siblings or ())}
    for index, item in enumerate(items):
        if index == 0:
            name, sha, why = branch, commit, reason
        else:
            name = sibling.get(item, "")
            if not name:
                wave.failures.append(
                    f"{item}: not recorded on {branch}; that branch is {items[0]}'s salvage"
                )
                continue
            sha = _branch_head(repo, name)
            why = ""
            try:
                pushed = wt_mod.push_branch(repo, name, base)
            except Exception as exc:  # noqa: BLE001 — the commit is local; the push is the report
                wave.failures.append(f"{name}: publish failed ({exc})")
            else:
                wave.published[name] = pushed
                if not pushed.ok and not pushed.skipped:
                    wave.failures.append(f"{name}: {pushed.reason}")
        _record_salvage_receipt(wave, client, [item], name, sha or "", reason=why)


def publish_salvaged(wave: Wave, repo: Path, salvaged: list, *,
                     client: Graphban | None, base_branch: str = "") -> None:
    """Push what a takeover recovered, and say on the item that it exists (GRPH-830).

    Adopting a stranded worktree already worked — the commit is made and the note is printed.
    What did not work was the step after: one measured takeover salvaged 614 insertions across
    exactly one item's touchpoints and left them on a LOCAL branch nothing pointed at. The item
    was re-delegated minutes later, branched from `main`, and rebuilt every line. The work was
    recovered and lost in the same move, and only somebody reading local refs could tell.

    Push is still required. A draft PR is not (GRPH-926): salvage subjects were landing as
    mergeable `WIP: salvaged by gbfleet` PRs. The item gets a note naming the branch and
    commit instead. A PR is for shipped work with a real title.

    Never fatal. This runs at the very start of a wave, on the crash path, and a takeover that
    refused to proceed because a push failed would strand the next wave too.

    `base_branch` is accepted and unused: the salvage path no longer proposes a PR, and
    callers still pass it. Dropping the argument would be a different kind of break.
    """
    del base_branch  # kept on the signature; salvage is not a PR
    for row in salvaged:
        items = [str(i) for i in (row.items or []) if i]
        branch = row.branch
        target = branch
        divert_reason = ""
        item = items[0] if items else ""
        if len(items) > 1:
            wave.failures.append(
                f"{branch}: salvage names {', '.join(items)}; not stamped onto one branch"
            )
        if item:
            try:
                target, divert_reason = _push_target(
                    wave, Path(repo), branch, row.base, item, client)
            except wt_mod.GitError as exc:
                wave.failures.append(f"{branch}: not pushed ({exc})")
                continue
        try:
            pushed = wt_mod.push_branch(repo, target, row.base)
        except Exception as exc:  # noqa: BLE001
            wave.failures.append(f"{branch}: salvaged but not published ({exc})")
            continue
        wave.published[target] = pushed
        if not pushed.ok:
            if not pushed.skipped:
                wave.failures.append(f"{target}: salvaged but not published "
                                     f"({pushed.reason})")
            continue
        if divert_reason:
            wave.diverted[branch] = (target, divert_reason)
            wave.failures.append(f"{branch}: diverted to {target} ({divert_reason})")
        named = target if divert_reason else branch
        observe.emit("adopt", detail=f"{named}: published salvaged work"
                                     + (f" for {item}" if item else ""))
        # One item on this ref. The rest were not built onto it, and a receipt that
        # names them here is the wrong-item attachment this exists to stop.
        _record_salvage_receipt(
            wave, client, items[:1], named, _branch_head(repo, named),
            reason=divert_reason)


#: How long a watched item waits before its merge is re-asked. Each ask is a ledger read and
#: a `gh` call against the forge; once a second, for the length of a wave, is a rate limit
#: spent on a question whose answer changes on the scale of a CI run.
MERGE_RECHECK_S = 60.0


@dataclass
class Merger:
    """Finishes the merge for items that reach `done` (GRPH-846, GRPH-880). Off unless asked.

    WHAT IT WATCHES (GRPH-880). An item is a candidate when the `fleet.sign_off` attestation
    names a commit — the reviewer's judgement is the handoff, not the moment this process
    observed the item leave a list. The candidacy predicate is:

        candidate ≡ item is in (this wave's scope ∪ GRPH-798 hold deps)
                AND status == done
                AND last fleet.sign_off attestation names a commit
                AND the item names a PR (or the propose url receipt does)

    `scope` is the PRD this wave was asked about (if any), else the project this key writes
    to. Hold deps stay a second source so a finished-but-not-on-base dependency outside the
    PRD can still land and clear the hold. A bounce (status != done) is never a candidate:
    leaving `review` for `next` does not enqueue a merge.

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
    #: The PRD this wave is scoped to (if any). Candidates outside this scope are not merged
    #: unless a GRPH-798 hold names them.
    prd_id: str = ""
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
    #: Item ids already discovered as candidates, so we do not re-scan them every tick.
    _seen: set[str] = field(default_factory=set, repr=False)

    def note_review(self, rows: list[dict]) -> None:
        """Rows currently in `review`. Kept for interface compatibility; candidacy is now
        read from the sign_off attestation, not from observing departures (GRPH-880)."""
        # No-op: the set-difference that used to live here was the two-phase miss.
        # An item already `done` when the `--merge` wave starts was never in this process's
        # `in_review`, so it was invisible. The attestation is the handoff now.

    def note_hold(self, absent: list[dict]) -> None:
        """Dependencies the GRPH-798 check found finished-but-not-on-the-base. Each is a
        merge that would clear a hold, which is the merge most worth finishing."""
        if not self.enabled:
            return
        for row in absent:
            item_id = str(row.get("id") or "")
            if item_id:
                self.watching.setdefault(item_id, "")

    def tick(self, wave: Wave, *, now: Callable[[], float] = time.monotonic) -> bool:
        """Ask once per candidate that is due. Returns True when something MERGED this tick,
        so the caller knows the base moved and any hold keyed on it should be re-asked."""
        if not self.enabled or self.disabled_because:
            return False
        # GRPH-880: discover candidates from the attestation, not from observing departures.
        self._discover_candidates()
        merged_any = False
        release: str | None = None
        for item_id in list(self.watching):
            stamp = now()
            if stamp - self.asked_at.get(item_id, -1e9) < MERGE_RECHECK_S:
                continue
            if release is None:
                # GRPH-P57 D1: read per tick, so unsetting the ref (the manual override) or
                # setting it takes effect on the next tick. Unreadable is not "unset" — an
                # unread rule must not read as an absent one — so nothing is asked.
                release = self._release()
                if release is None:
                    break
            self.asked_at[item_id] = stamp
            got = self._attempt(wave, item_id, release)
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

    def _discover_candidates(self) -> None:
        """Scan for done items with a fleet.sign_off attestation naming a commit.

        GRPH-880: candidacy is a READ of a named fact, not an observation of departure.
        An item already `done` when the `--merge` wave starts is a candidate on the first
        tick — no requirement that this process saw it leave `review`.

        Scope: if `prd_id` is set, only items belonging to that PRD are candidates.
        Hold deps (added via `note_hold`) bypass the scope filter — they are merges that
        would clear a hold, which is the merge most worth finishing.
        """
        if not self.enabled or self.disabled_because:
            return
        try:
            payload = self.client.call("search_items", status="done", fields="full",
                                       limit=10_000)
        except NotPermitted as exc:
            self._disable(f"cannot read done items: {exc}")
            return
        except (ToolFailed, ServerUnreachable):
            return
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("id") or "")
            if not item_id or item_id in self._seen:
                continue
            # Scope filter: if prd_id is set, skip items not belonging to it.
            # Hold deps bypass this (they were added explicitly via note_hold).
            if self.prd_id:
                item_prd = str(row.get("prd_id") or "")
                if item_prd != self.prd_id and item_id not in self.watching:
                    continue
            # Check for fleet.sign_off attestation naming a commit.
            if not propose_mod.reviewed_commit(row):
                continue
            # Check for a PR (or propose url receipt).
            if not propose_mod.pr_selector(row):
                continue
            self._seen.add(item_id)
            self.watching.setdefault(item_id, str(row.get("branch") or ""))

    def _release(self) -> str | None:
        """The project's release ref ("" when unset), or None when it could not be read."""
        try:
            roster = self.client.call("fleet_status")
        except NotPermitted as exc:
            self._disable(f"cannot read the release ref: {exc}")
            return None
        except (ToolFailed, ServerUnreachable) as exc:
            observe.emit("merge_unread", item="", detail=f"release ref: {exc}")
            return None
        return propose_mod.release_ref(roster if isinstance(roster, dict) else None)

    def _attempt(self, wave: Wave, item_id: str,
                 release: str = "") -> "propose_mod.Merged | None":
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
            # Bounced, or still moving. Not a candidate — a bounce is a verdict, not a
            # merge miss (GRPH-880).
            self.watching.pop(item_id, None)
            return None
        selector = propose_mod.pr_selector(item) or self.watching.get(item_id, "")
        got = propose_mod.merge(
            self.repo, item_id, selector,
            reviewed=propose_mod.reviewed_commit(item),
            green=propose_mod.green_commits(item),
            paths=item.get("touchpoints") or [],
            release=release,
            scans=propose_mod.scans_clean_verdicts(item),
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
        key = f"{got.ok}|{got.pending}|{got.skipped}|{got.reason}|{got.release}"
        if self.said.get(item_id) == key:
            return
        self.said[item_id] = key
        observe.emit("merge", item=item_id, ok=got.ok, pending=got.pending,
                     skipped=got.skipped, commit=got.commit, url=got.url,
                     checked=list(got.checked), release=got.release, detail=got.reason)

    def _disable(self, why: str) -> None:
        self.disabled_because = why
        observe.emit("merge_disabled", detail=why)


def substitution(asked: str | None, answered: str | None) -> tuple[str, str] | None:
    """`(asked, answered)` when a child ran a model nobody chose, else None (GRPH-982).

    THREE INPUTS THAT ARE NOT SUBSTITUTIONS, and keeping them out is the whole care here:

    * the two agree — the ordinary case;
    * nothing was asked for (the vendor default was accepted), so there is no claim to break;
    * nothing answered — a truncated or absent stream. "We could not read it" is not "it ran
      something else", and reporting it as one would manufacture a finding out of an absence,
      which is the defect this item belongs to.

    Only the fourth case — both known, and different — is a substitution. `-m` is passed
    through unchecked by at least one vendor and an unknown name is replaced by the configured
    default with no warning anywhere, so this is the only place a wave can learn it ran
    something the operator did not pick.
    """
    asked, answered = str(asked or ""), str(answered or "")
    if not asked or not answered or asked == answered:
        return None
    return asked, answered


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
            # GRPH-982. The model that ANSWERED against the one that was asked for. `-m` is
            # passed through unchecked by at least one vendor and an unknown name is replaced
            # by the configured default with no warning anywhere — so a wave can run a model
            # nobody chose and report the one they typed. Recorded only when both are known
            # and they DIFFER: "we could not read the stream" is not a substitution, and
            # flagging it as one would manufacture a finding out of an absence.
            swap = substitution(child.model, facts.get("model"))
            if swap is not None:
                wave.substituted[child.branch] = swap
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
            exit_meaning=_exit_meaning(
                child, code,
                unreadable=(wave.review_unreadable.get(child.branch)
                            if wave is not None else None)),
            diff_shape=child.diff_shape,
            **facts,
        )


def _exit_meaning(child: Child, code: int | None, *,
                  unreadable: list[str] | None = None) -> str:
    """What the adapter says its exit code means, or the reason this supervisor stopped it.

    The supervisor's own reason wins when it has one: a child killed for running past the
    wall clock exits with whatever the signal produced, and reporting that number as the
    vendor's verdict would attribute the supervisor's decision to the harness.

    `unreadable` appends the one thing only this process knows (GRPH-987): that an item this
    child built was in `review` while its branch could not be read off this machine. A
    reviewer handed that item can only bounce for a reason that is not the diff, and the
    bounce lands on the builder's vendor and model as a failed attempt. Nothing here can
    un-record it — the outcome is the server's, derived from the delegation — but the attempt
    row is this process's to describe, and a reader who finds the bounce there can now tell an
    environmental one from a verdict. Truncated to the column's width: `exit_meaning` is
    VARCHAR(256) on Postgres, and a long item list must not turn a measurement into a failed
    post.
    """
    if child.stopped_because is not None:
        meaning = f"stopped: {child.stopped_because.value}"
    elif code is None:
        meaning = ""
    else:
        meaning = explain_exit(child.adapter, code) or ("ok" if code == 0 else f"exit {code}")
    if unreadable:
        names = ", ".join(list(unreadable)[:3])
        note = f"{REVIEW_UNREADABLE}: {names} in review before its branch was readable"
        meaning = f"{meaning}; {note}" if meaning else note
    return meaning[:256]


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
    base_branch: str = "",
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
        # GRPH-1056. Waiting on children is the work. Without this, a wave longer
        # than the idle threshold is reclaimed out from under its own children.
        if wave.lock is not None:
            wave.lock.touch()
        watch_tick(wave, children, limits, client, debug=debug, persist=persist,
                   base_branch=base_branch)
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


def _reap_all(wave: Wave, children: list[Child], *, client: Graphban | None = None,
              base_branch: str = "") -> None:
    """Reap each worktree, then take away any seat that was never inside one.

    `worktree.reap` removes the seat files it knows about — the ones a vendor forced
    into the project directory. Claude Code takes `--mcp-config`, so its seat lives in a
    private temp file OUTSIDE the tree, and reaping the worktree does not touch it.

    Walk step 8 says the child's seat file is gone after reap, with no exception for the
    vendors that were tidy about where it went. Without this, the vendor that handled
    credentials BEST is the one that leaves one behind.

    **`client` is the one that records PR receipts on the item** (GRPH-869). The planner
    in `until`, the single client in `up`. Those receipts are an observations post
    (GRPH-975), which a supervisor-allowlist client may make — they are not `update_item`.
    The merge receipt further down still calls `update_item`, and that call stays on a
    client whose tool allowlist includes it.
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
        held = _salvage_items(child)
        if not held:
            # `child.held_items` is the last non-empty roster reading. The live partition
            # is empty after release, and reading only that would salvage with no item.
            held = list(child.held_items) or [
                i for i in (wave.partition.held.get(child.agent_id) or []) if i
            ]
        reaped = wt_mod.reap_held(tree, child.adapter, held)
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
        _note_touchpoints(wave, child, client)
        _note_staleness(wave, tree)
        salvaged = reaped.disposition is wt_mod.Disposition.SALVAGED
        _publish(wave, tree, client=client, child=child,
                 propose_prs=not salvaged, base_branch=base_branch)
        if salvaged:
            commit = reaped.salvage.commit if reaped.salvage else ""
            target, why = wave.diverted.get(tree.branch, (tree.branch, ""))
            _note_salvage(wave, client, tree.repo, target, held, reaped.siblings,
                          commit or "", base=tree.base, reason=why)
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
