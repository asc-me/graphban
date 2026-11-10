"""`gbfleet until` — planner-mode loop, not a thicker supervisor (P30 D1).

Same binary, in-process: `start_one` / the watch loop / reap as Python calls, not MCP
and not a socket. Two Graphban clients share one key and split by allowlist. The
supervisor client stays `{fleet_status, propose_allocation}`. Minting lives on the
planner client. Mixing those into `ALLOWED_TOOLS` is the widening G5 forbids.

Idle is not "the last child exited." Idle is: no ready non-colliding work, no unsigned
`review`, and no live worker still holding a lease. A running child that holds nothing
is not that lease. Typed human waits (D11) are not ready work; a wave that has only
those left is `idle-with-waits`. A wave that exits 0 with leftover `review` is a failed
run (`review-unsigned`).
"""

from __future__ import annotations

import os
import re
import subprocess

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import adopt as adopt_mod
from . import deps
from . import worktree as wt_mod
from . import observe
from . import waits as wait_mod
from . import watchdog as watchdog_mod
from .client import ALLOWED_TOOLS, Graphban, NotPermitted, OBSERVATION_PROBE, ObservationRefused, ServerUnreachable, ToolFailed
from .lock import hold
from .seat import Seat
from .tiers import TierTable
from . import matrix as matrix_mod
from .spawn import Child, Reason, build_holdings, stop
from . import spend as spend_mod
from . import touchpoints as tp_mod
from .spawn import LaunchFailed, VendorLimit
from . import entitlements as ent_mod
from .adapters import AdapterError
from .headroom import Headroom
from .supervisor import (
    _declared_into,
    DEFAULT_MAX_WORKERS, LaunchFactory, Limits, Merger, Wave, WaveBase,
    _reap_all, _rooted, _report_exits, _start, item_status, publish_salvaged,
    resolve_wave_base, watch_tick,
)

#: Planner-held tools. `register_agent` is how this process gets an `agent_id` to mint
#: against (two terminals on one key are two agents). `search_items` is how idle sees
#: `review` and typed waits — a read, not a supervisor write. Neither belongs in
#: `ALLOWED_TOOLS`.
PLANNER_TOOLS: frozenset[str] = frozenset({
    "propose_allocation",
    "collision_clusters",
    "mint_enrolment",
    "retire_wave",
    "fleet_status",
    "register_agent",
    # PRD-62 S1↔S3 (GRPH-1258). The wave's own ledger events for the entitlements it declared
    # at `register_agent`; the server refuses a reporter that did not declare the id.
    "report_entitlement_event",
    "search_items",
    # GRPH-1012: the base this wave cuts children from. Step 2 of the three is the
    # project's MEASURED `gitops.base_branch`, and `get_context` is the only way to ask
    # for it — a read, exactly like `search_items` above. It stays OFF `ALLOWED_TOOLS`:
    # the supervisor's own two reads still decide how many children of an already
    # authorised kind to run, and nothing else.
    "get_context",
    # PRD-35 D12: the delegation is written BEFORE the seat is minted, for the seed of the
    # next free cluster; `get_item_details` is where the brief (lane/tier suggestion) lives.
    "get_item_details",
    "delegate",
    # GRPH-846, and a repair on the way in. `related_work` is what the GRPH-798 dependency
    # check reads — and it was never in this set, so the check raised `NotPermitted` before
    # any request left the process, `deps.check` swallowed it, and the hold this loop
    # documents had never once fired. `update_item` is the receipt: the PR the supervisor
    # proposes (GRPH-804) and the merge it finishes are recorded on the item, and both
    # writes failed the same way. The planner has the standing (`record.py`: "`until`
    # (planner) after a reap"); the server bounds what an `update_item` may write by role,
    # not this set. The supervisor's own `ALLOWED_TOOLS` stays two.
    "related_work",
    "update_item",
    # GRPH-850: when a child dies (wall_clock / reap / stop), the planner releases its
    # held items so `choose_resume` can pick up the salvage branch. Without this, the row
    # stays `in_progress` / `claimed_by=<dead agent>` until the agent goes offline and
    # `fleet_status` calls `requeue_offline_items` — but by then a fresh spawn has already
    # cut from main and lost the work. The planner has the standing (it minted the seat);
    # the server bounds what `release_item` may do by role, not this set.
    "release_item",
    # GRPH-1190: `--prd` is resolved ONCE to the PRD's canonical key. A read, like the rest.
    "get_prd",
    # GRPH-1273: `review.staff_every_row`, read at the start of each cycle. A read.
    "get_rule_toggles",
})

#: The library rule that widens review staffing past `--prd` (GRPH-1273). Level `checked`:
#: the server refuses nothing here, this loop is the only thing that honours it.
STAFF_EVERY_ROW = "review.staff_every_row"

EMPTY_TICKS = 3
MINT_TRIES = 3
MINT_BUDGET_S = 30.0
#: S6 (PRD-39 D-i): consecutive spawns that exited against unheld review rows before
#: until stops trying. Re-keyed off the fact it measures (a child exited and the
#: review rows are still unheld) rather than off a role that no longer exists.
REVIEWER_FAILS = 3
#: GRPH-1401 bounce. Seconds a registered child that has never held anything still
#: counts as busy. Registration is not a claim: a reviewer spends several LLM turns on
#: search_items and claim_review before the roster shows its claim, and `empty_ticks`
#: polls of one second would stop it first. A constant, not a setting. A child empty
#: for longer than this (the GRPH-P72 drain: tens of minutes) is not busy.
CLAIM_WINDOW = 300.0

#: 4xx-shaped server refusals that will not change this process. Quota is config, not idle.
_CONFIG_CODES = frozenset({
    "forbidden", "not_permitted", "unauthorized", "unauthorised",
    "quota", "revoked", "validation", "role",
})


class ConfigError(RuntimeError):
    """Operator/credential/adapter. Exit 2. Not a cap and not idle."""


class CapError(RuntimeError):
    """A stated limit with ready work left. Exit 1. `reason` names which."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


@dataclass
class Report:
    """What `until` prints as JSON. Machines key on `reason` + `exit`."""

    ok: bool
    reason: str
    exit: int
    spawned: int = 0
    minted: int = 0
    waits: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)
    review_outside_prd: list[str] = field(default_factory=list)
    detail: str = ""
    wave: Wave | None = None

    def as_json(self) -> dict:
        payload = {
            # GRPH-1346. A wave whose records were refused is not clean, whatever the loop said.
            "ok": self.ok and not (self.wave.observations_refused if self.wave else 0),
            "reason": self.reason,
            "exit": self.exit,
            "spawned": self.spawned,
            "minted": self.minted,
            "waits": list(self.waits),
            # GRPH-1374. Only when non-empty: they lived on the Wave and reached no summary, so
            # a review row held up by a live child, or a lease that lapsed under a live child,
            # was visible only in a log.
            **({"review_waits": list(self.wave.review_waits)}
               if self.wave and self.wave.review_waits else {}),
            **({"lease_lapsed_live": list(self.wave.lease_lapsed_live)}
               if self.wave and self.wave.lease_lapsed_live else {}),
            "review": list(self.review),
            # GRPH-1188: with `--prd`, items in review that belong to another PRD or to none.
            # Informational only — this run neither spawns for them nor fails on them, since
            # the PRD-scoped seat it would mint cannot claim them. Listed so a person sees
            # they are waiting. Always empty without `--prd`.
            "review_outside_prd": list(self.review_outside_prd),
            # GRPH-834. Always present, including when nothing was measured: an absent key
            # reads as "this build has no spend reporting", and a zeroed block with
            # `reported: 0` reads as "nobody told us", which is the true statement.
            "spend": spend_mod.totals(self.wave.spend if self.wave else {}, self.spawned),
            # GRPH-842. Both keys, always, and the second is what makes the first readable:
            # an empty `gated` means "nothing was refused" only when `headroom_bytes` is a
            # number. Null means the host could not be asked and the gate never bound, which
            # is the reading that would otherwise pass for a roomy machine.
            "gated": list(self.wave.gated) if self.wave else [],
            "headroom_bytes": self.wave.headroom_at_start if self.wave else None,
            # PRD-41 D21 / criterion 24: every stage of every resolution. Always present;
            # empty means this run resolved nothing, not that the record was not kept.
            "resolutions": list(self.wave.resolutions) if self.wave else [],
            # GRPH-867: the diagnostic surface for a wave that hit cap with duplicates.
            # All four are computed on the Wave whether or not the wave ended `cap`; an
            # empty value reads as "nothing of this kind happened", not "we did not look".
            # `collided`: files changed on more than one branch (path → branches).
            "collided": dict(self.wave.collided) if self.wave else {},
            # `give_ups`: children that exited 75 (stuck, evidence written, item released).
            # A slot spent here is a slot that produced nothing a reviewer can read.
            "give_ups": list(self.wave.give_ups) if self.wave else [],
            # `proposed`: branches for which a draft PR was opened (branch → Proposed).
            # A branch that was published but NOT proposed is work nobody has been asked
            # to merge — the state this exists to make visible.
            "proposed": {b: {"url": getattr(p, "url", ""), "ok": getattr(p, "ok", False)}
                         for b, p in (self.wave.proposed.items() if self.wave else {})},
            # `undeclared`: measured paths that no DECLARED touchpoint covers (GRPH-785).
            # The partition's input was wrong; a worker changed a file nobody declared.
            "undeclared": dict(self.wave.undeclared) if self.wave else {},
            # GRPH-1025: branches whose declaration could not be re-read at reap, so the
            # check above used the delegation-time snapshot. Empty: every one was re-read.
            "declared_unread": dict(self.wave.declared_unread) if self.wave else {},
            # GRPH-949: branches pushed but refused a PR (undeclared files / behind trunk),
            # and leases a reaped child no longer held (info, not failures).
            "unproposed": dict(self.wave.unproposed) if self.wave else {},
            "lease_moved": list(self.wave.lease_moved) if self.wave else [],
            # GRPH-987: what this wave pushed while the child was STILL RUNNING because its item
            # had already reached `review`, and the items it saw in review with nothing readable
            # off this machine. Both always present and empty by default, so "no item reached
            # review early" cannot be read as "the check did not run" — and `review_unmeasured`
            # is the third answer, for a ledger that could not be asked at all.
            "published_in_review": dict(self.wave.published_in_review) if self.wave else {},
            "review_unreadable": dict(self.wave.review_unreadable) if self.wave else {},
            "review_unmeasured": self.wave.review_unmeasured if self.wave else "",
            # GRPH-1053. Empty means the landed-commit check ran, or no push needed it.
            # A refused read is this string, not an empty one — "nothing had landed" is
            # the reading that pushed a rework onto a branch the fix already used.
            "landed_unmeasured": self.wave.landed_unmeasured if self.wave else "",
            # GRPH-982: branches whose child ran a model nobody asked for. Always present and
            # empty by default, so "none observed" cannot be read as "not checked".
            "substituted": {k: list(v) for k, v in (self.wave.substituted.items()
                                                    if self.wave else [])},
        }
        # GRPH-1247. Every exhausted entitlement with the vendor's own reset text, quoted.
        # GRPH-1346. Always present: 0 means every record landed, not "not counted".
        refused = self.wave.observations_refused if self.wave else 0
        payload["supervision_unrecorded"] = refused
        payload["exhausted"] = (list(self.wave.exhausted.values()) if self.wave else [])
        # GRPH-1372. Who the wave continued on, and who it did not. A wave that failed over
        # reads as clean unless both are named: the server's reviewer-diversity gate can
        # still fail later for a vendor that never got a child here.
        launched = list(self.wave.vendors_launched) if self.wave else []
        payload["vendors_launched"] = launched
        payload["vendors_not_launched"] = [
            v for v in (self.wave.vendors_tried if self.wave else []) if v not in launched]
        if self.detail:
            payload["detail"] = self.detail
        return payload


def run(
    repo: Path,
    launch_factory: LaunchFactory,
    planner: Graphban,
    supervisor: Graphban,
    *,
    api_key: str,
    server: str,
    adapter: str,
    seats: list[Seat] | None = None,
    wave_name: str = "wave",
    limits: Limits = Limits(),
    state: Path | None = None,
    workspace: Path | None = None,
    poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    debug: bool = False,
    empty_ticks: int = EMPTY_TICKS,
    mint_tries: int = MINT_TRIES,
    mint_budget: float = MINT_BUDGET_S,
    request: str | None = None,
    prd: str | None = None,
    budget: int | None = None,
    shared: dict | None = None,
    tiers: TierTable | None = None,
    launch_for: Callable[..., LaunchFactory] | None = None,
    matrix: "matrix_mod.Matrix | None" = None,
    merge: bool = False,
    base_branch: str | None = None,
    entitlements: dict[str, list[str]] | None = None,
    records: "list[ent_mod.Entitlement] | None" = None,
    stall_after: float = watchdog_mod.STALL_AFTER_S,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    """Hold the repo lock and run until idle, a cap, or a config refusal.

    `merge` (GRPH-846, default OFF) makes the loop finish the merge of each item it sees
    reach `done` — see `supervisor.Merger` for the preconditions, every one of them checked.

    `request` is what every delegation this loop writes will REQUEST (PRD-35 D5). None means
    follow the brief's suggestion — a stated policy of this program, not a server default.
    `tiers` is the operator's tier table (PRD-36 D6): when the requested tier is mapped,
    `launch_for(adapter, model)` builds the child's launch; otherwise `launch_factory` does.

    `base_branch` (GRPH-847) is the operator's `--base`, and cuts children from
    `origin/<branch>` for PRDs whose slices are sequential. Refuses at startup when the
    branch does not exist on the remote — no silent fallback. Without it, the wave takes
    the project's MEASURED `gitops.base_branch` when `get_context` reports one, and the
    remote's own default ref otherwise (GRPH-1012). It never takes this checkout's HEAD:
    a supervisor standing on a feature branch used to build every child on it. The wave
    report says which of the three named the base.
    """
    if planner.allowed & {"mint_enrolment"} and "mint_enrolment" in ALLOWED_TOOLS:
        raise ConfigError("ALLOWED_TOOLS must not include mint_enrolment")
    if planner.allowed == ALLOWED_TOOLS:
        raise ConfigError("until needs a planner client, not the supervisor allowlist")

    repo, workspace = _rooted(repo, workspace)
    pool = list(seats or [])
    minted = 0
    wave = Wave()

    from . import observe
    observe.configure(state)
    # GRPH-1179. `kill -USR1 <pid>` dumps every thread's stack to the service log, and the
    # watchdog turns a silent stall into a logged one and a non-zero exit (KeepAlive restarts).
    watchdog_mod.install_stack_dump()
    watchdog_mod.current = watchdog_mod.Watchdog(stall_after, clock)

    try:
        if budget:
            check_budget_can_be_enforced(adapter, budget)
        if prd and pool:
            # GRPH-827. A pre-minted seat carries whatever scope it was minted with, and a
            # `--seats` file has none. Mixing them would produce a wave that reports as scoped
            # while the children holding those seats can claim the whole project — the finding
            # this scope exists to close, walked back in through a flag combination.
            #
            # INSIDE the try, so it comes back as the same `{"ok": false, "reason": "config"}`
            # every other refusal does. Raised two lines earlier it escaped `run` entirely and
            # the operator got a traceback, which is a worse answer to a config mistake.
            raise ConfigError(
                f"--prd {prd} cannot be combined with pre-minted seats: a seat from --seats "
                "carries no scope, so those children could claim past the wave. Drop --seats "
                "and let the loop mint scoped seats, or drop --prd and accept an unscoped wave")
        if pool and limits.max_workers <= 0:
            # GRPH-988. `--max-workers 0` pins `need` at 0, so the delegation branch can never
            # fire and the review branch is this supervisor's ONLY spawner — every child it
            # starts is a reviewer, and every one needs a seat that cannot claim build work.
            # A pre-minted seat cannot be that: a `--seats` file carries codes, and only the
            # server can grant the limit. Refused rather than worked around, because the
            # alternative is an operator who typed "build nothing" watching a child build.
            #
            # Inside the try for the same reason as the `--prd` refusal above.
            raise ConfigError(
                "--max-workers 0 cannot be combined with pre-minted seats: every child this "
                "supervisor spawns is a reviewer, and only the server can mint a seat that may "
                "review but not claim. Drop --seats and let the loop mint review-only seats")
        # GRPH-1190. An old key after a retag, a legacy key, or the frozen internal id all
        # name the same PRD to the server, but a review row's `prd_id` is its CURRENT key.
        # Resolved once here and used for every comparison below, so the PRD's own rows are
        # not read as outside it. Inside the try: a refusal is a config error, never a
        # fallback to the raw string.
        if prd:
            prd = resolve_prd(planner, prd)
            observe.emit("prd", detail=prd)
        # GRPH-1012. Resolved ONCE for the wave, before anything is cut from it or
        # published against it: the operator's `--base`, else the project's MEASURED
        # gitops base, else the remote's default ref — fetched, and never this checkout's
        # HEAD. Inside the try, so a base the remote does not have comes back as the same
        # `{"ok": false, "reason": "config"}` every other startup refusal does.
        chosen_base = resolve_wave_base(repo, planner, base_branch=base_branch or "")
        wave.base = chosen_base
        observe.emit("base", detail=chosen_base.note)
        with hold(repo, state) as acquired:
            wave.lock = acquired
            leftover: list[Child] = []
            occupied: set[str] = set()
            if acquired.takeover:
                recovered = adopt_mod.recover(repo, workspace, state)
                leftover, occupied, notes = recovered
                for note in notes:
                    observe.emit("adopt", detail=note)
                wave.spawned.extend(leftover)
                # GRPH-830: what was salvaged with real work in it gets the same two steps a
                # finished child gets — pushed, and named on the item it belongs to. Before
                # this the commit stayed local and the item was re-delegated and rebuilt from
                # `main`, so the recovery and the loss were the same event.
                #
                # The SAME resolved base, not a second resolution (GRPH-1012): a salvage
                # published against a different ref than the wave's children are cut from
                # is a PR opened against the wrong trunk.
                publish_salvaged(wave, repo, recovered.salvaged, client=planner,
                                 base_branch=chosen_base.ref)

            # GRPH-1011. The same sweep `gc` runs, over EVERY repository this state
            # directory names rather than only this one — the trees that need it belong to
            # the clone nobody starts a supervisor on again. Inside the lock, so this
            # repository answers "held" to the sweep's own try-acquire and is skipped by the
            # rule that protects every other live supervisor.
            for line in adopt_mod.sweep(state).lines:
                observe.emit("gc", detail=line)

            children: list[Child] = list(leftover)
            roster_path = adopt_mod.children_path(repo, state)

            def persist() -> None:
                adopt_mod.persist(roster_path, children)

            persist()
            wave.entitlements = {a: list(ids) for a, ids in (entitlements or {}).items() if ids}
            # GRPH-1372. The accounts this machine can launch, read once. Only read: the
            # exhausted mark lives on `wave`, and nothing this wave learns is written back.
            try:
                wave.records = list(ent_mod.load() if records is None else records)
            except (ent_mod.EntitlementError, ValueError) as exc:
                raise ConfigError(f"the entitlement registry could not be read: {exc}") from exc
            identity = _identify(planner, repo, adapter, wave)
            _preflight_observations(supervisor)
            try:
              result = _loop(
                wave, children, occupied, persist,
                repo, workspace, wave_name, api_key, server,
                launch_factory, planner, supervisor, identity, pool,
                limits, poll, sleep, debug,
                empty_ticks=empty_ticks,
                mint_tries=mint_tries,
                mint_budget=mint_budget,
                minted_start=minted,
                request=request,
                prd=prd,
                budget=budget,
                shared=shared or {},
                tiers=tiers or TierTable(),
                launch_for=launch_for,
                matrix=matrix,
                adapter=adapter,
                merge=merge,
                wave_base=chosen_base,
              )
            except CapError as exc:
                # GRPH-1438. A cap exit stops SPAWNING; it must not abandon a child that is
                # still running. Design chosen: wait here, keep reaping, and exit only after
                # the last live child is reaped and its branch published (not a hand-off to
                # a successor through the children file).
                waited = _drain_live(wave, children, persist, planner, supervisor,
                                     limits, poll, sleep, debug, chosen_base.ref)
                if waited:
                    raise CapError(exc.reason,
                                   f"{exc} [waited for {waited} live child(ren) to finish "
                                   "and be reaped before exiting]") from exc
                raise
            result.wave = wave
            if wave.observations_refused:
                result.ok = False
            minted = result.minted
            persist()
            return result
    except ConfigError as exc:
        wave.reason = "config"
        return Report(ok=False, reason="config", exit=2, detail=str(exc), wave=wave,
                      minted=minted, spawned=len(wave.spawned))
    except wt_mod.BaseBranchNotFound as exc:
        wave.reason = "config"
        return Report(ok=False, reason="config", exit=2, detail=str(exc), wave=wave,
                      minted=minted, spawned=len(wave.spawned))
    except VendorLimit as exc:
        # GRPH-829. Its own reason, because "cap" is what this wave reported before and it is
        # the wrong instruction: `cap` says the operator's own `--max-children` was reached and
        # invites raising it, while this says the vendor account is spent and the only thing
        # that helps is the clock. The wave ended on the wrong reason AND burned three of six
        # child slots getting there.
        wave.reason = "vendor_limit"
        return Report(ok=False, reason="vendor_limit", exit=1, detail=str(exc), wave=wave,
                      minted=minted, spawned=len(wave.spawned))
    except CapError as exc:
        wave.reason = exc.reason
        return Report(ok=False, reason=exc.reason, exit=1, detail=str(exc), wave=wave,
                      minted=minted, spawned=len(wave.spawned))
    except watchdog_mod.Stalled as exc:
        wave.reason = "stall"
        return Report(ok=False, reason="stall", exit=1, detail=str(exc), wave=wave,
                      minted=minted, spawned=len(wave.spawned))


def _drain_live(wave, children, persist, planner, supervisor, limits, poll, sleep, debug,
                base) -> int:
    """After a cap exit, keep reaping until no spawned child is running (GRPH-1438).

    Returns how many live children it waited for. Reaping publishes each child's branch, the
    same path the main loop uses, so a commit made after the cap was hit still reaches the
    remote. Bounded by `watch_tick`, which stops a child past its wall-clock limit.
    """
    waited = sum(1 for c in children if c.running)
    while any(c.running for c in children):
        try:
            watch_tick(wave, children, limits, planner, debug=debug, persist=persist,
                       base_branch=base)
        except (ToolFailed, ServerUnreachable, NotPermitted) as exc:
            observe.emit("drain_retry", detail=str(exc))
        finished = [c for c in children if not c.running]
        if finished:
            _report_exits(finished, supervisor, wave)
            _reap_all(wave, finished, client=planner, base_branch=base)
            children[:] = [c for c in children if c.running]
            persist()
        if any(c.running for c in children):
            sleep(poll)
    return waited


def _identify(planner: Graphban, repo: Path, adapter: str = "",
              wave: Wave | None = None) -> dict:
    """Register this process. A key that cannot mint is refused here, not after a wave.

    When the wave names entitlements the supervisor declares them too (GRPH-1258): the
    ledger accepts an event only from an agent that declared the id. A plan without capacity
    routing refuses the declaration with 402; the wave then registers plain and reports
    nothing, exactly as S1 does.
    """
    label = f"gbfleet until ({adapter})" if adapter else "gbfleet until"
    ids = sorted({i for v in (wave.entitlements if wave else {}).values() for i in v})
    try:
        try:
            payload = planner.call(
                "register_agent",
                label=label,
                role_hint="planner",
                worktree=str(repo),
                **({"entitlements": ids} if ids else {}),
            )
        except ToolFailed as exc:
            if not ids or exc.code != "quota_exceeded":
                raise
            wave.entitlements = {}
            observe.emit("ledger_off", detail=f"capacity routing is not on this plan: {exc}")
            payload = planner.call(
                "register_agent",
                label=label,
                role_hint="planner",
                worktree=str(repo),
            )
    except NotPermitted as exc:
        raise ConfigError(str(exc)) from exc
    except ToolFailed as exc:
        raise ConfigError(f"register_agent: {exc}") from exc
    except ServerUnreachable as exc:
        raise ConfigError(f"server unreachable: {exc}") from exc

    off = set(payload.get("tools_off_limits") or [])
    if "mint_enrolment" in off:
        raise ConfigError("this key cannot mint_enrolment — until needs a planner credential")
    role = payload.get("active_role") or ""
    eligible = set(payload.get("eligible_roles") or [])
    if role != "planner" and "planner" not in eligible:
        raise ConfigError(
            f"this key is {role or 'unscoped'}, not planner — until refuses rather than "
            "widening the supervisor"
        )
    agent_id = payload.get("agent_id") or payload.get("id")
    if not agent_id:
        raise ConfigError("register_agent returned no agent_id")
    return payload


def _preflight_observations(supervisor: Graphban) -> None:
    """Refuse to start on a key the observations route refuses (GRPH-1346).

    A wave on such a key still builds, reviews and merges; it just loses every base, lease
    and PR record, one refusal at a time, and used to report `ok: true`. Checked once, before
    a seat is minted, and named by the conditions the server states.
    """
    # The probe must be a body the route validates (GRPH-1346 bounce): `item_id` is required,
    # and a body without it is 422 before the credential is ever checked. With a real kind and
    # an item no key resolves, 401/403 is the refusal and 404 "no such item" is the pass.
    # 2xx would be a pass too. Anything else means the check did not run, and a check that did
    # not run must not read as passed: refuse, saying so. An unreachable server is the other
    # checks' business, so a transport failure (no status) is left to them.
    try:
        supervisor.post_observation(**OBSERVATION_PROBE)
    except ObservationRefused as exc:
        if exc.status == 404:
            return
        if exc.status in (401, 403):
            raise ConfigError(
                "the supervisor key cannot record observations, so this wave would lose every "
                "base, lease and PR record. Recording needs the planner role among the key's "
                "roles AND either a fleet wave or a project-scoped key; at least one is "
                f"missing. Server said: {exc}"
            ) from exc
        if exc.status is None:
            return
        raise ConfigError(
            "could not tell whether the supervisor key can record observations: the "
            f"observations route answered the start-up probe with HTTP {exc.status}, not "
            f"403 or 404. Server said: {exc}"
        ) from exc


def _item_brief(client, item_id: str | None) -> dict | None:
    """Capabilities, spend and mix counts for this item, when the planner can read it.

    Failures are silence: resolving without capabilities is the pre-S2 path, not a crash.
    Mix MUST come from this per-spawn read, not from fleet_status at launch: that payload
    is frozen for the process and a 20-child wave would keep picking the same under-target
    harness.
    """
    if not item_id or client is None:
        return None
    try:
        details = client.call("get_item_details", id=item_id)
    except Exception:  # noqa: BLE001
        return None
    brief = details.get("brief") if isinstance(details, dict) else None
    return brief if isinstance(brief, dict) else None


def _skip_exhausted(wave: Wave, check):
    """Wrap an `installed` checker so adapters the wave stopped walking resolve to nothing."""
    def wrapped(row):
        if row.harness in wave.vendor_out:
            return False, f"exhausted: {wave.vendor_out[row.harness]}"[:160]
        return check(row)
    return wrapped


def _records_for(wave: Wave, adapter: str, matrix) -> list:
    """The stored records an adapter can launch: vendor text naming the adapter or its row's vendor."""
    names = {adapter}
    if matrix is not None:
        names |= {r.vendor for r in matrix.rows if r.harness == adapter}
    return [r for r in wave.records if r.vendor in names]


def _next_record(wave: Wave, adapter: str, matrix):
    """(record, registered): the first record for `adapter` not yet exhausted this wave.

    `registered` False means this machine holds no record for the adapter, and the launch is
    the default login as it always was. True with no record means every one is spent.
    """
    held = _records_for(wave, adapter, matrix)
    for rec in held:
        if rec.name not in wave.exhausted:
            return rec, True
    return None, bool(held)


def _mark_exhausted(wave: Wave, exc: VendorLimit, chosen: str, record=None) -> None:
    adapter = chosen or exc.adapter
    key = record.name if record is not None else adapter
    # The vendor's words exactly as printed. The reset clause is not parsed into a time.
    wave.exhausted.setdefault(key, {
        "entitlement": record.name if record is not None else "",
        "adapter": adapter,
        "vendor": record.vendor if record is not None else adapter,
        "said": exc.said, "seen_at": int(time.time()),
    })


def _selecting(factory, record):
    """A factory whose refusal to select `record` is a launch failure, not a crash.

    `select_entitlement` raises before any process exists. Surfacing it as `LaunchFailed`
    lets `_start` reap the worktree and record the refusal; the default login is never used.
    """
    def wrapped(*args, **kwargs):
        try:
            return factory(*args, **kwargs)
        except AdapterError as exc:
            raise LaunchFailed(f"entitlement {record.name!r} refused at launch: {exc}") from exc
    return wrapped


def _capacity_detail(wave: Wave, last: str) -> str:
    names = ", ".join(sorted(wave.exhausted)) or "none recorded"
    return f"no entitlement with headroom is left (exhausted: {names}). {last}"


def _loop(
    wave: Wave,
    children: list[Child],
    occupied: set[str],
    persist: Callable[[], None],
    repo: Path,
    workspace: Path,
    wave_name: str,
    api_key: str,
    server: str,
    launch_factory: LaunchFactory,
    planner: Graphban,
    supervisor: Graphban,
    identity: dict,
    pool: list[Seat],
    limits: Limits,
    poll: float,
    sleep: Callable[[float], None],
    debug: bool,
    *,
    empty_ticks: int,
    mint_tries: int,
    mint_budget: float,
    minted_start: int,
    request: str | None = None,
    prd: str | None = None,
    budget: int | None = None,
    shared: dict | None = None,
    tiers: TierTable | None = None,
    launch_for: Callable[..., LaunchFactory] | None = None,
    matrix: "matrix_mod.Matrix | None" = None,
    adapter: str = "",
    merge: bool = False,
    wave_base: "WaveBase | None" = None,
) -> Report:
    from .mcp import _runner_up, read_preferences
    profile, policy, pref_note, measured, cap_measured, tier_map = read_preferences(supervisor)
    observe.emit("preferences", detail=pref_note)
    # GRPH-1003: this deployment's tier map layers onto the packaged matrix at WAVE START, on
    # the fleet_status call already made — not per spawn, and not inferred from the wheel. Only
    # when a matrix was handed in: `matrix=None` means "no matrix resolution, --tier only", and
    # loading one here would quietly give this run a resolver its caller did not ask for.
    # An unreachable server is already spelled out in pref_note, and what this wave then routes
    # on is the packaged matrix — reported, not read as "no override".
    if matrix is not None:
        matrix, tier_notes = tier_map.apply(matrix)
        for note in tier_notes:
            observe.emit("preferences", detail=note)
    agent_id = str(identity.get("agent_id") or identity.get("id"))
    empty = 0
    delegated: set[str] = set()
    # GRPH-1315: which child each delegated item was handed to. `delegated` stops THIS loop
    # offering an item while a child has it; once that child exits the server's state is the
    # answer again. Without this, a bounced item back in `next` was never offered by the
    # running loop — it read idle, and only a restart (a fresh, empty set) re-delegated it.
    owners: dict[str, object] = {}
    tiers = tiers or TierTable()
    minted = minted_start
    mint_deadline = time.monotonic() + mint_budget
    mint_left = mint_tries
    review_fails = 0
    ci_stuck: list[dict] = []
    ci_logged: set[tuple[str, str, str]] = set()  # GRPH-1392: events already logged
    review_spawned: list = []  # GRPH-1374: children this loop spawned for review rows
    # GRPH-842. One gate for the whole run, not one per spawn: a child spawned seconds ago
    # is not in the kernel's numbers yet, and a fresh reading per seat cannot know that. Its
    # charges expire, so an hour-long loop does not slowly refuse everything.
    room = Headroom(limits.child_memory)
    wave.headroom_at_start = room.baseline

    # GRPH-798: the ref children are cut from, resolved once and FETCHED once. A
    # remote-tracking ref is only as fresh as the last fetch, so skipping this would measure
    # every dependency as already merged — the same absence-reads-as-clean failure the stale
    # check in `supervisor` exists to avoid, on the check built to stop it.
    if prd:
        check_scope_is_honoured(planner, prd)
    # GRPH-988: a wave that builds nothing must be able to mint seats that cannot claim.
    if limits.max_workers <= 0:
        check_review_only_is_honoured(planner)
    remote = wt_mod.remote_for(repo)
    # GRPH-847, GRPH-1012. Handed in by `run`, which resolved and FETCHED it once for the
    # whole wave. Deliberately not resolved again here: a second resolution is a second
    # answer waiting to disagree with the first, and the dependency check below, the ref
    # children are cut from, and the base a PR is proposed against at reap all have to be
    # the SAME ref. An explicit `--base` still refuses at startup when the remote does not
    # have it — that refusal now happens in `resolve_wave_base`, before the lock.
    base = wave_base.ref if wave_base is not None else ""

    def _leave(reason: str, code: int, **kw) -> Report:
        # GRPH-1401. The process is exiting, so a child that registered and holds
        # nothing must not be left for the next cycle to adopt. A child that holds
        # an item stays; its lease is the work.
        _stop_children_holding_nothing(wave, children, supervisor, planner, base)
        return _finish(wave, reason, code, minted, planner, **kw)
    # GRPH-846, GRPH-880. Built whether or not `merge` was asked for, and inert when it was not:
    # `enabled` is the flag, read once here, so the loop below has one call site and no
    # branch on it — the branch is inside, where a test can see it stay closed. `prd_id`
    # scopes candidates: an item outside this PRD is not merged unless a GRPH-798 hold
    # names it.
    merger = Merger(repo, planner, enabled=bool(merge), remote=remote, base=base,
                    prd_id=prd or "")
    # Items the GRPH-798 check HELD, with the finished dependencies they wait on. A merge
    # changes the answer, so these are lifted out of `delegated` when one lands.
    held: dict[str, list[str]] = {}
    # GRPH-950: holds are blockers on the server, so they outlive the wave that wrote them.
    _resume_holds(planner, held, delegated, repo, base)
    rechecked = time.monotonic()

    def next_candidate(seed: str, want: str):
        """The next matrix winner for `want`, exhausted adapters skipped; None when none is left."""
        if not (want and want not in tiers.lanes and launch_for is not None
                and matrix is not None):
            return None
        brief = _item_brief(planner, seed)
        res = matrix.resolve(tier=want, profile=profile, policy=policy,
                             measured=measured,
                             installed=_skip_exhausted(wave, matrix_mod.installed_checker()),
                             capabilities=(brief or {}).get("capabilities"),
                             cap_measured=cap_measured,
                             spend=(brief or {}).get("spend"),
                             mix=(brief or {}).get("mix"))
        if res.winner is None:
            observe.emit("resolve_refused", item=seed, detail=res.refused)
            return None
        explained = res.explain()
        observe.emit("resolved", item=seed, **{k: v for k, v in explained.items() if k in ("winner", "dropped", "eligible", "profile", "stages", "capabilities")})
        wave.resolutions.append({"item": seed, **{k: explained[k] for k in
                                                  ("winner", "dropped", "stages",
                                                   "capabilities", "profile")
                                                  if k in explained}})
        return (launch_for(res.winner.harness, res.winner.model),
                (res.winner.harness, res.winner.model))

    def failover(seat, seed, want, factory, chosen):
        """GRPH-1247, GRPH-1372. Spawn; on a vendor limit, take the next record or vendor.

        The retry is a new process on the SAME seat — the child never registered, so the
        enrolment was not consumed and no second one is minted. The failed process and its
        worktree are reaped by `_start`. The first limit on a vendor may try one other record
        on it; the second stops the walk, the vendor is excluded and the matrix resolves again.
        Ends the wave with `capacity` (not `cap`, not `vendor_limit`) when nothing is left.
        """
        while True:
            mark = (len(wave.failures), len(wave.unredeemed), wave.unused_seats)
            exc: VendorLimit | None = None
            seat = replace(seat, entitlements=tuple(wave.entitlements.get(chosen[0], ())))
            record, registered = _next_record(wave, chosen[0], matrix)
            if chosen[0] and chosen[0] not in wave.vendors_tried:
                wave.vendors_tried.append(chosen[0])
            if chosen[0] in wave.vendor_out or (registered and record is None):
                said = wave.vendor_out.get(chosen[0]) or next(
                    (e["said"] for e in wave.exhausted.values() if e["adapter"] == chosen[0]), "")
                wave.vendor_out.setdefault(chosen[0], said)
                exc = VendorLimit(f"adapter {chosen[0]!r} is exhausted this wave: {said}",
                                  adapter=chosen[0], said=said)
            else:
                launching = factory
                if record is not None and launch_for is None:
                    raise ConfigError(
                        f"entitlement {record.name!r} is registered for {chosen[0]!r}, and this "
                        "wave has no adapter factory to select its handle; refusing rather "
                        "than starting the default login")
                if record is not None:
                    # The record's handle is selected, or the launch is refused. A record
                    # never launches as the default login.
                    launching = _selecting(
                        launch_for(chosen[0], chosen[1], entitlement=record), record)
                try:
                    before = len(children)
                    _spawn_one(
                        wave, children, occupied, persist, seat, launching,
                        repo, workspace, wave_name, supervisor, limits, planner, debug,
                        base=base, ledger=planner,
                    )
                    if seed and len(children) > before:
                        owners[seed] = children[-1]
                    if chosen[0] and chosen[0] not in wave.vendors_launched:
                        wave.vendors_launched.append(chosen[0])
                    return
                except VendorLimit as caught:
                    exc = caught
                    _mark_exhausted(wave, caught, chosen[0], record)
                    hits = wave.vendor_limits.get(chosen[0], 0) + 1
                    wave.vendor_limits[chosen[0]] = hits
                    left, _ = _next_record(wave, chosen[0], matrix)
                    if hits >= 2 or left is None:
                        # The second limit means it is still in force for a new process —
                        # including when the second handle is the same login.
                        wave.vendor_out[chosen[0]] = caught.said
                    else:
                        del wave.failures[mark[0]:]
                        del wave.unredeemed[mark[1]:]
                        wave.unused_seats = mark[2]
                        observe.emit("failover", item=seed,
                                     detail=f"{record.name if record else chosen[0]} exhausted; "
                                            f"continuing on {left.name} ({chosen[0]})")
                        continue
            nxt = next_candidate(seed, want)
            if nxt is None:
                wave.reason = "capacity"
                raise CapError("capacity", _capacity_detail(wave, str(exc))) from exc
            # The failed attempt is superseded by the retry, not a second failure.
            del wave.failures[mark[0]:]
            del wave.unredeemed[mark[1]:]
            wave.unused_seats = mark[2]
            prev = chosen[0]
            factory, chosen = nxt
            seat = replace(seat, declare=matrix_mod.declaration(
                chosen[0], chosen[1], want or None, matrix))
            observe.emit("failover", item=seed,
                         detail=f"{prev} exhausted; continuing on {chosen[0]}")

    while True:
        # GRPH-1056. This loop IS the supervisor working. An `mcp` that merely stays
        # up does not touch, and past the idle threshold the next `hold` stops it.
        # Touching here is what keeps a long drain from being read as that.
        if wave.lock is not None:
            wave.lock.touch()
        # GRPH-869: planner, not supervisor. `watch_tick` → `_reap_exited` → `_publish`
        # → `propose_branch` → `update_item`. The supervisor allowlist is two reads;
        # `update_item` is on the planner. Passing supervisor here opened the PR and
        # then logged "PR opened but not recorded" on every item.
        watch_tick(wave, children, limits, planner, debug=debug, persist=persist,
                   base_branch=base)
        # GRPH-834: checked HERE, right after the tick that reads the exit records, and before
        # anything else this pass can spawn. `_cap_children` guards `--max-children` at the
        # spawn site, and that is the wrong shape for a budget: a wave whose last child has
        # already blown the cap should stop even if this pass was never going to spawn.
        #
        # The wave FINISHES rather than aborting — running children are left to their own
        # ends. Killing them would spend the tokens and throw away the work, which is the one
        # outcome worse than going over.
        if (crossed := spend_mod.over(wave.spend, budget)):
            observe.emit("budget", detail=crossed)
            raise CapError("budget", crossed)
        finished = [c for c in children if not c.running]
        if finished:
            # S6 (PRD-39 D-i): re-keyed off the fact it measures — a child exited
            # and there are still unheld review rows. Not off a role.
            try:
                rows_for_reap, beyond = _split_by_prd(_review_rows(planner), prd)
                if _staff_every_row(planner):
                    rows_for_reap = rows_for_reap + beyond
            except (NotPermitted, ToolFailed, ServerUnreachable) as exc:
                watchdog_mod.current.retry("review_rows(reap)", exc)
                rows_for_reap = []
            unheld_at_reap = [r for r in rows_for_reap if not review_held(r)]
            for child in finished:
                if unheld_at_reap and not child.held_items:
                    review_fails += 1
                elif child.held_items:
                    review_fails = 0
            # READ THE EXIT RECORD BEFORE DROPPING THE CHILD (GRPH-834). `watch_tick` reports
            # exits, but it ran a few lines up — a child that exited in between is in
            # `finished` and was never reported, and the line below removes it from `children`
            # so no later pass can ever see it. Found by the spend summary coming back empty on
            # a wave that plainly spent something; the same window was silently losing the
            # PRD-38 attempt row for that child, which is the more expensive half.
            #
            # Idempotent: `child.reported` makes a second pass a no-op, so the common case
            # where `watch_tick` already reported the child costs nothing.
            _report_exits(finished, supervisor, wave)
            _reap_all(wave, finished, client=planner, base_branch=base)
            _release_delegated(delegated, owners, finished, held)
            children[:] = [c for c in children if c.running]
            persist()
            if any("handoff-failed" in f for f in wave.failures):
                wave.reason = "handoff-failed"
                return _finish(wave, "handoff-failed", 1, minted, planner, prd=prd)

        live = [c for c in children if c.running]
        wd = watchdog_mod.current
        wd.check(bool(live))
        # GRPH-1401. A running process is not a lease. `busy` is a non-stale build
        # lease, or a live child that currently holds an item. The unreachable
        # branch below still treats any live process as "not empty": a missed read
        # must not look like a clear backlog.
        busy = _idle_blocked(supervisor, live)
        try:
            # GRPH-1188: with `--prd`, only this PRD's review rows can trigger a spawn or fail
            # the run. The reviewer seat minted below is PRD-scoped and cannot claim any other
            # row, so a project-wide trigger spawned children that could only exit, and ended
            # the run `review-unsigned` over an item it was never able to review.
            staff_all = _staff_every_row(planner)
            rows, rows_outside = _split_by_prd(_review_rows(planner), prd)
            if staff_all:
                # GRPH-1273: every row visible to the key is this run's to staff.
                rows, rows_outside = rows + rows_outside, []
            # GRPH-1409: a row whose newest refusal is a post-cooldown `pr_ci_not_green` cannot
            # be cleared by another reviewer, so it is taken out of what can spawn one.
            ci_stuck = [r for r in rows if ci_unattestable(r)]
            rows = [r for r in rows if not ci_unattestable(r)]
            # GRPH-1417: silence from a run that is still going is not "unattestable": its
            # attestation will arrive. Those rows wait; only the rest can end the wave.
            ci_pending: dict[str, str] = {}
            for r in list(ci_stuck):
                running = ci_run_in_progress(repo, r)
                if running:
                    ci_pending[str(r["id"])] = running
                    ci_stuck.remove(r)
            reviews = [str(r["id"]) for r in rows + ci_stuck]
            outside = [str(r["id"]) for r in rows_outside]
            waits = _wait_ids(planner)
            for rid, running in ci_pending.items():
                line = f"{rid}: review_waits_on_ci ({running})"
                if line not in waits:
                    waits.append(line)
                if line not in wave.review_waits:
                    wave.review_waits.append(line)
                    observe.emit("review_waits_on_ci", review=rid, run=running)
        except NotPermitted as exc:
            raise ConfigError(f"cannot classify review/waits: {exc}") from exc
        except ToolFailed as exc:
            raise ConfigError(f"cannot classify review/waits: {exc}") from exc
        except ServerUnreachable as exc:
            # Unknown is not empty: leftover review must not look like idle.
            wd.retry("review/waits", exc)
            if live or busy:
                sleep(poll)
                continue
            raise CapError(
                "cap",
                f"search_items unreachable; leftover review is unknown, not empty ({exc})",
            ) from exc
        wd.progress("review/waits read")

        # GRPH-846, GRPH-880. The merger discovers candidates from the attestation, not from
        # observing departures. An item already `done` with a sign_off attestation is a
        # candidate on the first tick.
        if merger.tick(wave):
            # The base moved. Whatever was held on a finished-but-unmerged dependency is
            # offered again — the check re-runs against the freshly fetched ref and either
            # lets it through or holds it on whatever is still missing.
            for item_id in list(held):
                # The blocker goes too (GRPH-950), or the server keeps refusing an item this
                # loop has stopped refusing. The re-check below writes it back if still missing.
                _lift(planner, item_id)
                delegated.discard(item_id)
                observe.emit("hold_lifted", item=item_id,
                             detail=f"{item_id}: a merge landed; re-checking its dependencies")
            held.clear()
        elif held and time.monotonic() - rechecked >= HOLD_RECHECK_SECONDS:
            # Merged by somebody other than this loop — a person, or a wave without `--merge`.
            rechecked = time.monotonic()
            _recheck_holds(planner, held, delegated, repo, base)

        try:
            need = _wanted_workers(planner, supervisor, live_n=len(live),
                                   max_workers=limits.max_workers, prd=prd)
        except ServerUnreachable as exc:
            # D-i: no new spawns while unreachable. Live children run to their lease.
            wd.retry("wanted_workers", exc)
            if not live:
                wave.reason = "cap"
                raise CapError("cap", "server unreachable with no live children")
            sleep(poll)
            continue

        if need > 0:
            # Re-read before minting into a cluster that just filled (allocation race).
            try:
                need = _wanted_workers(planner, supervisor, live_n=len(live),
                                       max_workers=limits.max_workers, prd=prd)
            except ServerUnreachable as exc:
                wd.retry("wanted_workers(recheck)", exc)
                sleep(poll)
                continue
            if need <= 0:
                sleep(poll)
                continue
            # Before the mint, not after it. A seat minted into a machine with no room is a
            # consumed enrolment nothing registers on, and `--mint-tries` is finite.
            if _no_room(wave, room, len(live)):
                sleep(poll)
                continue
            # PRD-36 D9: the delegation mints the BOUND seat the child will register on, so
            # the child claims the seed rather than whatever the divvy hands it. When the
            # server refused a bound seat (areas held) the delegation stands without one
            # and the seat is minted as before; when nothing was delegable, likewise.
            # GRPH-885: when _delegate_next returns None for seed, there is no delegable work
            # (all clusters are held or refused). Do NOT spawn an unbound child — that would
            # tell the child to `claim_cluster` on a neighborhood whose seed is pinned, which
            # skip-aheads the DAG. Do NOT reset `empty` either: `_wanted_workers` still
            # counts those free file-clusters, so zeroing here spun the loop forever
            # (CI cancelled the fleet suite after 6h). Count toward idle like a no-work tick.
            flags: dict = {}
            seed, code, want = _delegate_next(planner, agent_id, wave_name, delegated,
                                              request, prd, repo, base, held=held,
                                              merger=merger, bound_out=flags,
                                              live=live, owners=owners)
            if seed is None and flags.get("pinned_until"):
                # GRPH-1332. A bounced item is reserved for its live author: a wait, not
                # idle. The pin ends with its expiry or the author going offline, so this is
                # bounded; the item was not marked delegated, so the next tick offers it.
                empty = 0
                sleep(poll)
                continue
            if seed is not None:
                # GRPH-1458: a reopen_on_red item reaches its next child with the failing
                # tests named, not just the failing jobs.
                name_red_tests(planner, repo, seed, agent_id)
            if seed is None:
                # No takeable work this tick. Fall through to the idle counter below.
                pass
            elif code:
                empty = 0
                seat = Seat(shared=dict(shared or {}),
                            code=code, server_url=server, api_key=api_key, role="worker",
                            item=seed, bound_only=bool(flags.get("bound_only")))
                minted += 1
                factory = launch_factory
                chosen = (adapter, "")
                if want and want in tiers.lanes and launch_for is not None:
                    lane = tiers.resolve(want)
                    factory = launch_for(lane.adapter, lane.model)
                    chosen = (lane.adapter, lane.model)
                elif want and launch_for is not None and matrix is not None:
                    # PRD-37: no flag for this tier — the matrix resolves under the profile and
                    # policy read at launch, and the log says how.
                    brief = _item_brief(planner, seed)
                    res = matrix.resolve(tier=want, profile=profile, policy=policy,
                                         measured=measured, installed=_skip_exhausted(wave, matrix_mod.installed_checker()),
                                         capabilities=(brief or {}).get("capabilities"),
                                         cap_measured=cap_measured,
                                         spend=(brief or {}).get("spend"),
                                         mix=(brief or {}).get("mix"))
                    if res.winner is not None:
                        factory = launch_for(res.winner.harness, res.winner.model)
                        chosen = (res.winner.harness, res.winner.model)
                        explained = res.explain()
                        observe.emit("resolved", item=seed, **{k: v for k, v in explained.items() if k in ("winner", "dropped", "eligible", "profile", "stages", "capabilities")})
                        wave.resolutions.append({"item": seed, **{k: explained[k] for k in
                                                                  ("winner", "dropped", "stages",
                                                                   "capabilities", "profile")
                                                                  if k in explained}})
                        # PRD-41 D21 / criterion 24: the same object spawn already posts
                        # (PRD-38 D3). until is the fleet's primary path; without this,
                        # attempt_telemetry has no stages and replay/R6 cannot see what
                        # was resolved. Fire-and-forget inside the client.
                        declare = matrix_mod.declaration(
                            res.winner.harness, res.winner.model, want or None, matrix)
                        planner.post_attempt(
                            enrolment_code=seat.code,
                            adapter=res.winner.harness,
                            winner=f"{declare.get('vendor', '')}:{declare.get('model', '')}",
                            runner_up=_runner_up(explained, matrix),
                            source=explained.get("source") or "matrix",
                            resolution=explained,
                        )
                    else:
                        observe.emit("resolve_refused", item=seed, detail=res.refused)
                # GRPH-732: the child is told what it is, because only this side knows.
                if chosen[0]:
                    seat = replace(seat, declare=matrix_mod.declaration(chosen[0], chosen[1], want or None, matrix))
                _cap_children(wave, limits, item=seed)
                failover(seat, seed, want, factory, chosen)
                wd.progress(f"spawned for {seed}")
                continue
            else:
                empty = 0
                seat, minted_one = _take_seat(
                    pool, planner, agent_id, wave_name, server, api_key,
                    mint_left=mint_left, mint_deadline=mint_deadline, sleep=sleep,
                    role="worker", prd=prd,
                )
                if minted_one:
                    minted += 1
                    mint_left -= 1
                factory = launch_factory
                chosen = (adapter, "")
                if want and want in tiers.lanes and launch_for is not None:
                    lane = tiers.resolve(want)
                    factory = launch_for(lane.adapter, lane.model)
                    chosen = (lane.adapter, lane.model)
                elif want and launch_for is not None and matrix is not None:
                    # PRD-37: no flag for this tier — the matrix resolves under the profile and
                    # policy read at launch, and the log says how.
                    brief = _item_brief(planner, seed)
                    res = matrix.resolve(tier=want, profile=profile, policy=policy,
                                         measured=measured, installed=_skip_exhausted(wave, matrix_mod.installed_checker()),
                                         capabilities=(brief or {}).get("capabilities"),
                                         cap_measured=cap_measured,
                                         spend=(brief or {}).get("spend"),
                                         mix=(brief or {}).get("mix"))
                    if res.winner is not None:
                        factory = launch_for(res.winner.harness, res.winner.model)
                        chosen = (res.winner.harness, res.winner.model)
                        explained = res.explain()
                        observe.emit("resolved", item=seed, **{k: v for k, v in explained.items() if k in ("winner", "dropped", "eligible", "profile", "stages", "capabilities")})
                        wave.resolutions.append({"item": seed, **{k: explained[k] for k in
                                                                  ("winner", "dropped", "stages",
                                                                   "capabilities", "profile")
                                                                  if k in explained}})
                        # PRD-41 D21 / criterion 24: the same object spawn already posts
                        # (PRD-38 D3). until is the fleet's primary path; without this,
                        # attempt_telemetry has no stages and replay/R6 cannot see what
                        # was resolved. Fire-and-forget inside the client.
                        declare = matrix_mod.declaration(
                            res.winner.harness, res.winner.model, want or None, matrix)
                        planner.post_attempt(
                            enrolment_code=seat.code,
                            adapter=res.winner.harness,
                            winner=f"{declare.get('vendor', '')}:{declare.get('model', '')}",
                            runner_up=_runner_up(explained, matrix),
                            source=explained.get("source") or "matrix",
                            resolution=explained,
                        )
                    else:
                        observe.emit("resolve_refused", item=seed, detail=res.refused)
                # GRPH-732: the child is told what it is, because only this side knows.
                if chosen[0]:
                    seat = replace(seat, declare=matrix_mod.declaration(chosen[0], chosen[1], want or None, matrix))
                _cap_children(wave, limits, item=seed)
                failover(seat, seed, want, factory, chosen)
                wd.progress(f"spawned for {seed}")
                continue

        # S6 (PRD-39 D-i): unheld review rows need a merged worker. Re-keyed off the
        # fact it measures — a child was spawned against unheld review rows, exited,
        # and the rows are still unheld — not off a role that no longer exists.
        # A live child blocks a second spawn (just as live_reviewers did before).
        #
        # GRPH-988: the ROLE stays `worker` — `reviewer` merged into it in S3, and a worker
        # builds and reviews — but the SEAT is review-only, because the child this branch
        # spawns was never meant to build. Minting a plain worker seat here meant a supervisor
        # started with `--max-workers 0` (need pinned at 0, so this branch is its ONLY
        # spawner) still put a child on the board that could `claim_next`: one claimed
        # GRPH-965 and built it on the reviewer's branch while the child actually assigned to
        # that item produced nothing. The authority is the seat, not the prompt, so the gate is
        # server-side (`items.review_only_seat`) and this is only the mint that asks for it.
        # No memory gate on this branch, deliberately (GRPH-842): `not live` means nothing
        # is running, and the gate never refuses the first child — so a check here could
        # only ever cost a `vm_stat` per poll and answer yes. **If that guard goes, this
        # needs `_no_room` like the worker branch above.**
        #
        # GRPH-1374. A live BUILDER no longer blocks it: a reviewer for item B has nothing to do
        # with the child building item A, and waiting on that child held a review row for the
        # whole build (GRPH-1342). What still blocks is a live REVIEW spawn of this branch —
        # it has not claimed yet, so a second would stack reviewers on the same rows — and,
        # when anything is live, the memory gate and child cap like every other spawn.
        review_busy = _review_busy(live, review_spawned)
        # GRPH-1392: CI is read only when this branch would spawn — one `gh pr view` per row,
        # not paid on a poll where `need` or a live review spawn decides nothing spawns.
        unheld_review = [r for r in rows if not review_held(r)]
        ci_waiting: list[dict] = []
        if unheld_review and need <= 0 and not review_busy:
            unheld_review, ci_waiting, ci_blocked = _ci_attested(
                unheld_review, repo, ci_logged, planner=planner, agent_id=agent_id)
            if ci_blocked:
                rows = [r for r in rows if r not in ci_blocked]
                reviews = [str(r["id"]) for r in rows + ci_stuck]
        # GRPH-1387: the gate's reason is kept so the wait below names it, read once per poll
        # and only when this branch would otherwise spawn.
        review_gate = None
        if unheld_review and need <= 0 and not review_busy and live:
            review_gate = _room_refusal(wave, room, len(live))
        if unheld_review and need <= 0 and not review_busy and review_gate is None:
            empty = 0
            if review_fails >= REVIEWER_FAILS:
                wave.reason = "review-unsigned"
                return _name_ci_stuck(
                    _leave("review-unsigned", 1, review=reviews, waits=waits,
                           outside=outside), ci_stuck)
            seat, minted_one = _take_seat(
                pool, planner, agent_id, wave_name, server, api_key,
                mint_left=mint_left, mint_deadline=mint_deadline, sleep=sleep,
                role="worker", prd=None if staff_all else prd, review_only=True,
            )
            if minted_one:
                minted += 1
                mint_left -= 1
            if adapter:
                seat = replace(seat, declare=matrix_mod.declaration(adapter, "", None, matrix))
            _cap_children(wave, limits)
            before = len(wave.spawned)
            kids = len(children)
            failover(seat, "", "", launch_factory, (adapter, ""))
            if len(children) > kids:
                review_spawned.append(children[-1])
            if len(wave.spawned) == before:
                review_fails += 1
            continue

        if busy:
            if unheld_review and live:
                for line in _review_waits_on_live(
                        wave, unheld_review, live, review_spawned=review_spawned,
                        gate=review_gate):
                    if line not in waits:
                        waits.append(line)
            empty = 0
            sleep(poll)
            continue

        if ci_waiting:
            # GRPH-1392: CI is still running at these heads. That is a wait, not unsigned
            # work: keep polling until it settles, then the branch above spawns (green) or
            # blocks (red). CI finishes on its own, so this is bounded.
            for row in ci_waiting:
                line = f"{row.get('id')}: review waits on CI"
                if line not in waits:
                    waits.append(line)
            empty = 0
            sleep(poll)
            continue

        # Nothing running: an unheld review row is unsigned now. A child spawned on
        # an earlier poll that has not claimed yet is `busy` above (`CLAIM_WINDOW`),
        # so it is not reached here; one past that window still holding nothing
        # gets `empty_ticks` and is then stopped by `_leave`.
        if not live:
            unheld = [r for r in rows if not review_held(r)]
            if unheld:
                wave.reason = "review-unsigned"
                return _name_ci_stuck(
                    _leave("review-unsigned", 1, review=reviews, waits=waits,
                           outside=outside), ci_stuck)

        if ci_pending:
            # GRPH-1417: CI is still running for the head; no reviewer, no exit.
            empty = 0
            sleep(poll)
            continue

        empty += 1
        if empty >= empty_ticks:
            if ci_stuck and not rows:
                wave.reason = "review-ci-unattestable"
                report = _finish(wave, "review-ci-unattestable", 1, minted, planner,
                                 review=reviews, waits=waits, outside=outside)
                report.detail = "; ".join(ci_stuck_detail(r) for r in ci_stuck)
                return report
            # GRPH-1356: a row still in review is unsigned work, held or not. A hold with no
            # live child of ours is a reviewer that may have exited between polls; after the
            # polls above it is still not "nothing unsigned", so it must not read as idle.
            if rows:
                wave.reason = "review-unsigned"
                return _name_ci_stuck(
                    _leave("review-unsigned", 1, review=reviews, waits=waits,
                           outside=outside), ci_stuck)
            if waits:
                wave.reason = "idle-with-waits"
                return _leave("idle-with-waits", 0, waits=waits, review=reviews,
                              outside=outside)
            wave.reason = "idle"
            return _leave("idle", 0, review=reviews, outside=outside)
        sleep(poll)


def _finish(
    wave: Wave, reason: str, exit_code: int, minted: int, planner: Graphban,
    *, review: list[str] | None = None, waits: list[str] | None = None,
    outside: list[str] | None = None, prd: str | None = None,
) -> Report:
    if waits is None:
        try:
            waits = _wait_ids(planner)
        except (NotPermitted, ToolFailed, ServerUnreachable):
            waits = []
    if review is None:
        try:
            inside, beyond = _split_by_prd(_review_rows(planner), prd)
            review = [str(r["id"]) for r in inside]
            outside = [str(r["id"]) for r in beyond]
        except (NotPermitted, ToolFailed, ServerUnreachable):
            review = []
    return Report(
        ok=exit_code == 0,
        reason=reason,
        exit=exit_code,
        spawned=len(wave.spawned),
        minted=minted,
        waits=waits,
        review=review,
        review_outside_prd=list(outside or []),
        wave=wave,
    )


def _spawn_one(
    wave: Wave,
    children: list[Child],
    occupied: set[str],
    persist: Callable[[], None],
    seat: Seat,
    launch_factory: LaunchFactory,
    repo: Path,
    workspace: Path,
    wave_name: str,
    supervisor: Graphban,
    limits: Limits,
    planner: Graphban,
    debug: bool,
    base: str = "",
    ledger: Graphban | None = None,
) -> None:
    before = len(wave.spawned)
    # No `room=` on purpose (GRPH-842). `_start`'s own gate is for `up`, which decides how
    # many seats to run in one go; this loop asks `_no_room` BEFORE minting, because a seat
    # minted into a full machine is a consumed enrolment nothing registers on. Passing one
    # here would gate the same spawn twice and count the refusal as a reviewer failure.
    _start(
        wave, [seat], launch_factory, repo, workspace, wave_name, supervisor,
        limits, debug=debug, occupied=occupied, items=_declared_into(wave,
                                                                     item_status(planner)),
        into=children, persist=persist, base=base, ledger=ledger,
    )
    occupied.update(c.branch for c in children)
    persist()
    if len(wave.spawned) == before and wave.failures:
        raise ConfigError(wave.failures[-1])


#: A PRD id no deployment will ever hold, used to ask the server whether it filters at all.
PROBE_PRD = "__gbfleet_probe_no_such_prd__"


def check_scope_is_honoured(planner: Graphban, prd: str) -> None:
    """Refuse `--prd` when the server ignores it (GRPH-800).

    An MCP server that has never heard of `prd_id` does not refuse it — it drops the
    unrecognised argument and answers the unfiltered question. Measured against the deployed
    2026.09.16, which predates the filter: scoped and unscoped both returned the same four
    clusters, no error. So `--prd SA-P11` against such a server would drain the whole project
    WHILE THE OPERATOR BELIEVED IT WAS SCOPED — the bug the flag exists to fix, wearing the
    fix's clothes, which is strictly worse than not having the flag.

    The probe is a question the answer to which cannot be ambiguous: ask for a PRD that cannot
    exist. A server that filters returns nothing; one that ignores the argument returns the
    project. The filtering side is pinned by a backend test that asserts an unknown PRD scopes
    to nothing rather than to everything, so the two halves cannot drift apart.

    One case it cannot distinguish, stated because silence is what this is about: a project
    with no ready clusters answers zero either way. Then the probe reads "supported" on a
    server that is not — and it does not matter, because there is nothing to delegate.
    """
    try:
        got = planner.call("collision_clusters", prd_id=PROBE_PRD)
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        # Could not ask. Not evidence either way, and refusing the wave over an unreachable
        # server here would duplicate the loop's own handling of that.
        observe.emit("scope_unverified", detail=f"could not probe prd_id support: {exc}")
        return
    if int(got.get("total") or 0) > 0:
        raise ConfigError(
            f"--prd {prd} was given, but this server ignores prd_id: asked for a PRD that "
            f"cannot exist and it returned {got.get('total')} clusters. It would delegate "
            "every ready item in the project while reporting the wave as scoped. Upgrade the "
            "server, or run without --prd and accept that it drains the project"
        )
    check_seat_scope_is_honoured(planner, prd)


def check_budget_can_be_enforced(adapter: str, budget: int) -> None:
    """Refuse `--budget` when the adapter reports no token usage (GRPH-834).

    The same argument as `check_scope_is_honoured` one flag over, and it is the argument that
    matters most for a budget: a cap over a vendor that prints no result record is not a loose
    cap, it is one that can never be exceeded and therefore never fires. The operator would
    watch a wave run to completion believing it was bounded.

    Refused before the lock and before any worktree, naming the adapter, because the remedy is
    a different adapter or no flag — neither of which is discovered usefully an hour in.

    Only `gbagent` reports today. That is a fact about the vendors rather than a limitation
    chosen here: a vendor joins `result_facts` after its record has been measured, never on
    the strength of its documentation.
    """
    from .adapters import ADAPTERS, reports_tokens

    if reports_tokens(adapter):
        return
    able = sorted(name for name in ADAPTERS if reports_tokens(name))
    raise ConfigError(
        f"--budget {budget} was given, and the {adapter!r} adapter reports no token usage: "
        "nothing would ever be counted against it, so the cap could not end a wave and this "
        "run would look bounded while being unbounded. "
        + (f"Adapters that report: {', '.join(able)}. " if able else "")
        + "Run without --budget, or use an adapter that reports")


def check_seat_scope_is_honoured(planner: Graphban, prd: str) -> None:
    """Refuse `--prd` when the server takes the scope but does not put it on the SEAT.

    The same argument as the probe above, one layer down, and it needs its own check because
    the two halves shipped in different releases. A server that filters `collision_clusters`
    but drops `delegate`'s `scope` passes the first probe completely: the wave delegates
    inside the PRD and every child then holds an unscoped credential, which is exactly the
    measured behaviour this flag was extended to fix — three delegated items inside the scope,
    six self-claimed outside it.

    **Read from the manifest rather than probed by minting.** `tools/list` is a question with
    no side effects; the alternative was to mint a seat with an impossible scope and see
    whether it was refused, which leaves a real credential lying around on every server that
    passes. A seat is the thing this is trying to bound — spraying them to find out is the
    wrong instrument.
    """
    try:
        tools = planner.list_tools()
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        observe.emit("scope_unverified", detail=f"could not read the tool manifest: {exc}")
        return
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") != "delegate":
            continue
        props = ((tool.get("inputSchema") or {}).get("properties") or {})
        if "scope" in props:
            return
        raise ConfigError(
            f"--prd {prd} was given, but this server's `delegate` takes no `scope`: it would "
            "bound what this loop hands out and mint children that can claim the whole "
            "project anyway. Upgrade the server, or run without --prd")
    # `delegate` absent from the manifest entirely is a different problem, and the loop's own
    # handling of a missing tool reports it better than a guess here would.
    observe.emit("scope_unverified", detail="delegate is not in the tool manifest")


def check_review_only_is_honoured(planner: Graphban) -> None:
    """Refuse `--max-workers 0` when the server cannot mint a review-only seat (GRPH-988).

    The same argument as the two probes above and the same instrument as the second: read
    `tools/list`, do not mint a seat to find out. A server that has never heard of
    `review_only` does not refuse it — `_validate_args` ignores an unknown extra — so it
    answers with a plain worker seat and nothing downstream can tell. That is the defect this
    exists to close, reintroduced silently against an older server, while the operator reads
    the flag they typed as a promise.

    Probed only at `--max-workers 0`, and the narrowing is the point rather than an
    optimisation. At any other value the review branch is one spawner among several and a
    reviewer child that can also claim is today's behaviour — unfixed, not newly broken. At
    zero it is the whole wave: `need` is pinned at 0, the delegation branch cannot fire, and
    every child this supervisor starts comes from the review branch.
    """
    try:
        tools = planner.list_tools()
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        observe.emit("review_only_unverified",
                     detail=f"could not read the tool manifest: {exc}")
        return
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") != "mint_enrolment":
            continue
        props = ((tool.get("inputSchema") or {}).get("properties") or {})
        if "review_only" in props:
            return
        raise ConfigError(
            "--max-workers 0 was given, but this server's `mint_enrolment` takes no "
            "`review_only`: every child this supervisor spawns is a reviewer, and a seat the "
            "server cannot limit is a seat that can claim build work — so this wave would "
            "build while reporting that it builds nothing. Upgrade the server, or raise "
            "--max-workers and accept that its children can claim")
    observe.emit("review_only_unverified",
                 detail="mint_enrolment is not in the tool manifest")


class _Repeats:
    """Says whether a recurring line is worth printing again (GRPH-817).

    NEW, or OLD ENOUGH. A holder set that has not changed says nothing new; the same holder an
    hour later says the wave is still waiting, which is different information from silence.
    `_HEARTBEAT` is the floor, so a long wait is visible without being a transcript.

    Cleared when the condition lifts, so the next occurrence reports immediately rather than
    inheriting the last one's timer.
    """

    def __init__(self) -> None:
        self.last: str = ""
        self.at: float = 0.0
        #: How many times this same condition has been reported. Lets the caller say "still",
        #: which is the difference between a heartbeat and a line that looks like a hang.
        self.repeats: int = 0

    def changed(self, key: str, *, now: Callable[[], float] = time.monotonic) -> bool:
        stamp = now()
        if key != self.last:
            self.last, self.at, self.repeats = key, stamp, 0
            return True
        if (stamp - self.at) >= _HEARTBEAT:
            self.at, self.repeats = stamp, self.repeats + 1
            return True
        return False

    def clear(self) -> None:
        self.last, self.at, self.repeats = "", 0.0, 0


#: How long the same contention line waits before repeating. Long enough that a wave which
#: waits ten minutes prints twice rather than six hundred times.
_HEARTBEAT = 300.0

_contention = _Repeats()
#: The same, for clusters spawned into ALONGSIDE another drain's soft lease (GRPH-1389).
_sharing = _Repeats()


def _free_and_blocked(clusters: dict) -> tuple[list[dict], list[dict]]:
    """Split the divvy into what can be claimed now and what is held (GRPH-803).

    `held_by` is set by the server when a cluster's AREAS are reserved. `_delegate_next` has
    always skipped clusters carrying it — and nothing ever set it, so that guard never once
    fired. It fires now, and this is the other half: a wave should not size itself off work it
    cannot take.
    """
    free, blocked = [], []
    for cluster in clusters.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        (blocked if cluster.get("held_by") else free).append(cluster)
    return free, blocked


def _holders_key(blocked: list[dict]) -> str:
    """What makes a contention report NEW: who holds it and how much. Deliberately excludes
    the countdown, which changes every second and would defeat the deduplication."""
    holders = sorted({h for c in blocked for h in (c.get("held_by") or [])})
    return f"{len(blocked)}:{','.join(holders)}"


def _waiting(blocked: list[dict], *, repeat: int = 0) -> str:
    """What a person needs to decide whether to wait: who holds it, and for how long."""
    holders = sorted({h for c in blocked for h in (c.get("held_by") or [])})
    waits = [c.get("free_in") for c in blocked if isinstance(c.get("free_in"), int)]
    soonest = min(waits) if waits else None
    # A REPEAT SAYS IT IS ONE. The countdown resets whenever a holder renews its lease, which
    # is what a working agent does — so a bare number that jumps back up reads as a hang. On
    # a repeat the line says the wait is still live rather than leaving the reader to infer
    # it from a figure that went the wrong way.
    still = " — still held, the lease was renewed" if repeat else ""
    return (f"{len(blocked)} cluster(s) held by {', '.join(holders) or 'another agent'}{still}"
            + (f"; the earliest frees in {soonest}s" if soonest is not None
               else "; no expiry reported")
            + _merged_on(blocked)
            + "".join(f"; {h}" for c in blocked for h in _held_files(c))
            + ". Not spawning into work that cannot be claimed")


def _soft_key(free: list[dict]) -> str:
    """What makes a soft-overlap report NEW: which clusters overlap whom."""
    rows = sorted(f"{(c.get('items') or ['?'])[0]}<{o.get('by')}" for c in free
                  for o in (c.get("soft_overlap") or []) if isinstance(o, dict))
    return ",".join(rows)


def _overlapping(free: list[dict]) -> str:
    """A free cluster that overlaps another drain's SOFT lease (GRPH-1389), said once.

    Soft is the default: git reports a real conflict at merge time, and a conflict there
    bounces the item naming the files, so the drain spawns rather than idling. Said, so the
    log shows two waves sharing a file on purpose rather than a lock that failed to hold.
    """
    shared = [c for c in free if c.get("soft_overlap")]
    holders = sorted({str(o.get("by")) for c in shared for o in c["soft_overlap"]
                      if isinstance(o, dict)})
    pairs = [f"{o.get('reserved')} vs {o.get('area')}" for c in shared
             for o in c["soft_overlap"] if isinstance(o, dict)][:3]
    return (f"{len(shared)} cluster(s) overlap soft leases held by "
            f"{', '.join(holders) or 'another agent'} ({', '.join(pairs)}). Spawning anyway: "
            "git reports a real conflict at merge, and a conflict bounces the item")


def _merged_on(blocked: list[dict]) -> str:
    """Why those clusters are clusters at all (GRPH-810).

    A wait is easier to judge when you know what you are waiting for. "Held by SA-A2" says an
    agent has it; "and these items are one cluster because they are files in the same
    directory" says whether the queue is real work or an artefact of the grouping rule.

    Named only when EVERY reason is the directory rule. A mixture is a genuine overlap with
    some directory noise in it, and reporting that as "just the directory rule" would talk an
    operator out of a wait they should take seriously.
    """
    rules = {r.get("rule") for c in blocked
             for m in (c.get("because") or []) for r in (m.get("on") or [])}
    if rules == {"directory"}:
        return (", and they are one cluster only because their files share a directory "
                "(GRPH-810)")
    return ""


def _held_files(cluster: dict) -> list[str]:
    """The files a held cluster's review members keep out of reach, one sentence each
    (GRPH-951). The server writes `holds` on each review hold; a reservation hold names its
    area already."""
    out = []
    for h in cluster.get("held_because") or []:
        if isinstance(h, dict) and "holds" in h:
            out.append(_holds_sentence(str(h.get("by") or "?"), list(h.get("holds") or [])))
    return out


def _joined(cluster: dict) -> list[str]:
    """Which rule joined which two items (GRPH-951): "A + B: directory (svc/a.py ~ svc/b)"."""
    out = []
    for m in cluster.get("because") or []:
        if not isinstance(m, dict):
            continue
        pair = " + ".join(str(i) for i in m.get("items") or [])
        on = ", ".join(f"{r.get('rule')} ({r.get('a')} ~ {r.get('b')})"
                       for r in m.get("on") or [] if isinstance(r, dict))
        out.append(f"{pair}: {on}")
    return out


def plan(planner: Graphban, prd: str | None, max_workers: int,
         repo: Path | None = None, base: str = "") -> dict:
    """What this wave WOULD delegate, without delegating it (GRPH-819).

    Reported after a real wave: "you cannot ask what a wave would delegate before it does —
    and the scoping bug existed for exactly as long as nobody could see the plan." `--prd`
    bounds the damage; this is what lets you check the bound before spending anything.

    READS ONLY. It calls the same `collision_clusters` the loop calls and applies the same
    split, so what it prints is what the loop would take rather than a second implementation
    that can disagree with it — a dry run that models the wave instead of asking it is a dry
    run that reassures you about the wrong plan.
    """
    clusters = planner.call("collision_clusters", holds=True, **_scope(prd))
    free, blocked = _free_and_blocked(clusters)
    would = [c for c in free[:max_workers]]
    return {
        "prd": prd or None,
        # GRPH-827: a dry run that showed only what would be HANDED OUT answered half the
        # question. The other half is what the children could then take on their own, and for
        # a scoped wave that is now the same set. Said explicitly, because the previous answer
        # to it was a README paragraph that turned out to be false.
        "seats_scoped": bool(prd),
        "would_delegate": [(c.get("items") or [None])[0] for c in would],
        "clusters_free": len(free),
        "clusters_held": len(blocked),
        # Named, because "10 free" and "10 free, and here they are" are different amounts of
        # help when the question is whether the scope is right.
        "free": [{"seed": (c.get("items") or [None])[0], "items": c.get("items") or [],
                  "areas": c.get("areas") or [],
                  # GRPH-951: the rule that joined each pair, so "one cluster" can be judged.
                  "joined": _joined(c)} for c in free],
        "held": [{"items": c.get("items") or [], "held_by": c.get("held_by") or [],
                  "free_in": c.get("free_in"),
                  # WHICH area the hold covers and by which rule (GRPH-833). "Held by SA-A39"
                  # sends the reader looking for SA-A39; this says what the collision actually
                  # is, which is the half an operator was left to infer — and inferred wrong.
                  "because": c.get("held_because") or [],
                  # GRPH-951: "SA-576 holds CueQueue.tsx, styles/queue.css".
                  "holds": _held_files(c),
                  "joined": _joined(c)} for c in blocked],
        # The reservation table itself, keyed on the HOLD rather than on the cluster. A
        # cluster leaves the partition the moment its item is claimed, taking its reservation
        # off every read while that reservation goes on blocking everyone — so a wave with no
        # free clusters and no `held` rows had nothing to show for itself at all.
        "holds": clusters.get("holds") or [],
        # GRPH-950: what the loop would hold on an unmerged dependency — "SA-556 waiting on
        # #397 to merge" — and what an earlier wave already holds. Empty without a repository
        # and a base to measure against, which is the one case the check cannot run.
        "dependency_holds": _dependency_holds(planner, free, repo, base),
        "capped_by_max_workers": len(free) > max_workers,
    }


def _dependency_holds(planner: Graphban, free: list[dict], repo: Path | None,
                      base: str) -> list[dict]:
    """The GRPH-950 holds a wave would write now, and the ones already written. READS ONLY."""
    out: list[dict] = []
    seen: set[str] = set()
    for item_id, blocker in _held_on_server(planner):
        seen.add(item_id)
        out.append({"item": item_id, "waiting_on": blocker, "already_held": True})
    if repo is None or not base:
        return out
    for cluster in free:
        for item_id in cluster.get("items") or []:
            if not isinstance(item_id, str) or item_id in seen:
                continue
            seen.add(item_id)
            try:
                details = planner.call("get_item_details", id=item_id) or {}
            except (ToolFailed, NotPermitted, ServerUnreachable):
                continue
            if not isinstance(details, dict) or not _seed_ready(details):
                continue
            absent, _ = deps.check(planner, item_id, repo, base)
            if absent:
                out.append({"item": item_id, "waiting_on": deps.waiting_on(absent, base),
                            "already_held": False})
    return out


def _scope(prd: str | None) -> dict:
    """The wave's work filter, as `collision_clusters` arguments (GRPH-797).

    One function rather than an inline dict at each call site, because the two sites decide
    DIFFERENT things — what to delegate, and how many workers are wanted — and a filter applied
    to one and not the other would size the fleet for work it then refuses to hand out.

    Empty when unscoped, so an unfiltered run sends exactly what it sent before.
    """
    return {"prd_id": prd} if prd else {}


def _seat_scope(prd: str | None) -> dict:
    """The wave's scope, as the arguments that put it on the CREDENTIAL (GRPH-827).

    Separate from `_scope` above and deliberately so: that one filters what this loop offers,
    this one binds what the child may take on its own. They carry the same value and answer
    different questions, and the gap between them is the whole finding — one measured wave
    delegated three items inside its PRD while its children self-claimed six outside it,
    including an ops item whose checklist mutates production.

    Empty when unscoped, so an unfiltered wave mints exactly the seat it minted before. A
    server that has never heard of `scope` drops it silently, which is why `until` refuses to
    run scoped against a server that ignores the filter (`check_scope_is_honoured`).
    """
    return {"scope": prd} if prd else {}


def _seed_ready(details: dict) -> bool:
    """Startable the way `get_backlog` means ready: next/backlog, no unfinished blocker.

    A file-cluster's `items[0]` is not that. Review, done, blocked, and an item whose brief
    still names an unfinished dependency are not seeds (GRPH-886). Status `blocked` is
    named first so a "Do not launch" row cannot be seeded even when a supervisor asks
    (GRPH-1032).
    """
    status = str(details.get("status") or "")
    if status == "blocked":
        return False
    if status not in ("next", "backlog"):
        return False
    brief = details.get("brief") if isinstance(details.get("brief"), dict) else {}
    if brief.get("blocked_by"):
        return False
    if str(details.get("blocker") or "").strip():
        return False
    return True


def _attested_here(entry: dict, branch: str) -> bool:
    """Is this evidence entry about `branch`? (GRPH-983, reading 2)

    A CI attestation records the ref it ran on in its predicate detail — `CI passed on <branch>
    at <sha>` — so an attestation naming a DIFFERENT branch is about a different PR and says
    nothing about this item. An entry that names no branch is believed: receipts written before
    the ref was recorded are still good evidence, and refusing them would trade one false
    reading for another.
    """
    if not branch:
        return True
    named = str(entry.get("branch") or "").strip()
    if not named:
        # Receipts written before the field existed carry the ref only in the predicate prose.
        # Read as a fallback, never as the primary: a consumer that depended on this regex
        # would read every receipt as unnamed after a wording change, and unnamed is the
        # BELIEVED case — the guard would switch itself off silently.
        for pred in entry.get("predicates") or []:
            if not isinstance(pred, dict):
                continue
            found = re.search(r"CI passed on (\S+) at ", str(pred.get("detail") or ""))
            if found:
                named = found.group(1)
                break
    return not named or named == branch


def _already_in_base(details: dict, repo: Path | None, base: str) -> bool:
    """True when this item's branch or an attested commit is already in `base`.

    A git-merged item still `next` (waiting on sign-off) is not a new build. `reaches`
    returns None when this clone cannot tell; that is not "merged".

    GRPH-930: a squash merge rewrites the SHA, so `reaches` returns False for a commit
    whose work IS in the base via the squash. When the forge says the PR is MERGED, the
    work is carried by the squash SHA — same fallback as `deps.check` (GRPH-868). Without
    it, a squash-merged item still `next` would be re-delegated and rebuilt for nothing.

    GRPH-983 closes two ways this said "delivered" about work that did not exist. Both cost
    real items, and both presented as a supervisor exiting `idle, spawned 0` with ready work
    on the board — the failure looking exactly like having nothing to do.

    1. **An empty branch.** A branch cut from the base and never committed to still points AT
       the base, so it is trivially an ancestor and `reaches` says True. A child that produced
       nothing marked its item delivered. Counting the branch's own commits does NOT separate
       the two — once a branch merges, `rev-list base..branch` is 0 for the same reason — so
       the branch's ancestry is treated as necessary and never sufficient: it needs a receipt
       that work existed. Wave p48a left GRPH-982/983/984/985 undelegable this way in one wave.

    2. **A sibling PR's attestation.** An attestation says CI ran green on a sha, not that the
       sha implements this item, and CI attaches receipts by scanning a PR body for item keys —
       so a PR that merely MENTIONS an id, including to say "this is not that item", leaves a
       merged sha here. GRPH-955 was made permanently undelegable by two of my own PRs that
       way, and evidence only appends, so it could not be undone: the work had to be re-filed
       as GRPH-981 and the item's bounce history was lost. An attestation that NAMES a branch
       is therefore only believed about that branch. One with no branch recorded is still
       believed, because refusing it would drop the receipts written before that was recorded.
    """
    if not base or repo is None:
        return False
    from .worktree import reaches as wt_reaches

    branch = str(details.get("branch") or "").strip()
    # COMMIT-BEARING only. A plain `note`, or a `test` receipt with no commit, says nothing
    # about whether a revision exists — and an item accumulates notes as a matter of course, so
    # "has evidence" was satisfied by any item anyone had written on. GRPH-983 itself carries
    # two notes, which would have held its own empty branch gb/p48a-1 under the first version
    # of this fix.
    usable = [e for e in (details.get("evidence") or [])
              if isinstance(e, dict) and str(e.get("commit") or "").strip()
              and _attested_here(e, branch)]

    # Ancestry is necessary, not sufficient (1): an empty branch is an ancestor too. A receipt
    # that work existed — a commit recorded for this branch, or this item's PR — is the second
    # signal, and git cannot supply it.
    if branch and wt_reaches(repo, base, branch) is True:
        if usable or str(details.get("pr") or "").strip():
            return True
    seen: list[str] = []
    for entry in usable:
        commit = str(entry.get("commit") or "").strip()
        if not commit or commit in seen:
            continue
        seen.append(commit)
        if wt_reaches(repo, base, commit) is True:
            return True
    # GRPH-930: SHA ancestry says "not in base", but a squash merge rewrites the SHA.
    # Ask the forge whether this item's PR is MERGED — that fact survives the rewrite.
    # Only a definitive MERGED counts; an open PR or an unreachable forge leaves the
    # answer as False (the original git-only verdict), not True.
    return deps._pr_is_merged(repo, details) is True


def _hold(planner: Graphban, item_id: str, details: dict, absent: list[dict], base: str) -> bool:
    """Write the GRPH-950 hold onto the item: a blocker every claim path refuses.

    `delegated` only stops THIS loop offering the item. A child that `claim_cluster`s or
    `claim_next`s asks the server, and the server calls a done dependency met — so without a
    blocker the item this loop just held is the item a sibling child takes (SA-556). A blocker
    somebody else wrote is left alone: it already keeps the item out of reach, and it is theirs.
    """
    current = str(details.get("blocker") or "")
    if current and not deps.is_hold(current):
        return False
    text = deps.hold_text(item_id, absent, base)
    if current == text:
        return True
    try:
        planner.call("update_item", id=item_id, blocker=text)
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        observe.emit("hold_unrecorded", item=item_id, detail=str(exc))
        return False
    return True


def _lift(planner: Graphban, item_id: str) -> None:
    """Clear a GRPH-950 hold. Only ever called for items this module held."""
    try:
        got = planner.call("get_item_details", id=item_id) or {}
        if isinstance(got, dict) and deps.is_hold(str(got.get("blocker") or "")):
            planner.call("update_item", id=item_id, blocker="")
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        observe.emit("hold_unlifted", item=item_id, detail=str(exc))


def _review_waits_on_live(wave: Wave, unheld: list, live: list, *,
                          review_spawned: list | tuple = (), gate: str | None = None) -> list[str]:
    """Name what an unheld review row is waiting on (GRPH-1349, GRPH-1387). Logged once per
    row and reason, so a ten-minute wait is one line.

    Since GRPH-1374 a live builder does not hold a review spawn, so only two things do, and
    only those are named: a review spawn of ours that has not claimed yet ("reviewer pid N
    not yet claimed"), and the memory gate (`gate`, its reason). A child that is reviewing
    the row, or that built it and is still finishing, is never what the row waits on.
    """
    out: list[str] = []

    def _say(rid: str, line: str, pid, item: str) -> None:
        out.append(line)
        if line not in wave.review_waits:
            wave.review_waits.append(line)
            observe.emit("review_waits_on_live_child", review=rid,
                         pid=pid, item=item, gate=gate or "")

    for row in unheld:
        rid = str(row["id"])
        named = False
        for child in live:
            if child not in review_spawned or _child_touches(child, rid):
                continue
            if _has_claimed(child):
                continue
            pid = getattr(getattr(child, "process", None), "pid", None)
            _say(rid, f"{rid}: review waits on reviewer pid {pid} not yet claimed",
                 pid, "no item yet")
            named = True
        if gate and not named:
            _say(rid, f"{rid}: review waits on the memory gate ({gate})", None, "")
    return out


def _review_busy(live: list, review_spawned: list | tuple) -> bool:
    """A review spawn of ours that has not claimed yet blocks another (GRPH-1374): it has
    not said which rows it is taking, so a second would stack reviewers on the same rows.

    One that HAS claimed does not (GRPH-1413). It holds its rows, `review_held` keeps them
    out of the next spawn's reach, and the rows nobody holds should not wait the 15-30
    minutes its review takes. The memory gate and child cap still apply to every spawn.
    """
    return any(c in review_spawned and not _has_claimed(c) for c in live)


def _has_claimed(child) -> bool:
    """A reviewer's claims are `review_items`; `held_items` is build leases only, which a
    review-only seat never has (GRPH-1413 bounce). A builder's `held_items` still counts."""
    return any(i for i in (list(getattr(child, "review_items", None) or [])
                           + list(getattr(child, "held_items", None) or [])))


def _child_touches(child, item_id: str) -> bool:
    """Whether `child` holds, held, or was bound to `item_id`: its reviewer or its builder."""
    seen = list(getattr(child, "held_items", None) or [])
    seen += list(getattr(child, "ever_held", None) or [])
    got = getattr(child, "assigned", None)
    if isinstance(got, dict) and got.get("item"):
        seen.append(got["item"])
    return item_id in {str(i) for i in seen if i}


def _release_delegated(delegated: set[str], owners: dict[str, object], finished: list,
                       held: dict[str, list[str]]) -> None:
    """GRPH-1315. Let the server decide again about items whose child has exited.

    Released: an item handed to a child in `finished`, and an item marked without a child
    (a refused bound seat, a spawn that failed) that no GRPH-798 hold explains — the exit
    is the moment the server's answer may have changed. Kept: an item a still-running child
    has, and a held item, which a merge lifts instead. A released item that is in review or
    done is not delegable; `_delegate_next` reads it and counts it as an occupier, which is
    what it is.
    """
    gone = {id(c) for c in finished}
    for item_id in list(delegated):
        owner = owners.get(item_id)
        if owner is not None and id(owner) not in gone:
            continue
        if owner is None and item_id in held:
            continue
        delegated.discard(item_id)
        owners.pop(item_id, None)
        observe.emit("delegation_released", item=item_id,
                     detail=f"{item_id}: its child exited; the server decides whether it is offered again")


def _adopt_holds(planner: Graphban, held: dict[str, list[str]], delegated: set[str]) -> None:
    """Holds an earlier wave wrote, taken over so this wave lifts them when their PR merges.

    A blocker nobody re-checks strands its item after the merge it waits on — worse than the
    duplicate it prevented, because it looks deliberate.
    """
    for item_id, _blocker in _held_on_server(planner):
        if item_id not in held:
            held[item_id] = []
            delegated.add(item_id)


def _resume_holds(planner: Graphban, held: dict[str, list[str]], delegated: set[str],
                  repo: Path | None, base: str) -> None:
    """Adopt the holds earlier runs wrote and re-check them NOW, before the first tick.

    GRPH-1182: not a recheck period later. Under `gbfleet service` every run is a fresh
    process that may exit idle well inside that period, so a hold whose dependency an
    earlier run merged was lifted by nobody.
    """
    _adopt_holds(planner, held, delegated)
    _recheck_holds(planner, held, delegated, repo, base)


def _held_on_server(planner: Graphban) -> list[tuple[str, str]]:
    """Every GRPH-950 hold the server carries, as (item, blocker). Empty when unreadable.

    The search matches on blocker text, but a row that does not carry `blocker` says nothing
    about it (GRPH-1182): `search_items` full rows omitted the field, so every hit read as
    unheld and no later run ever adopted, re-checked or lifted a hold. Such a row is confirmed
    against `get_item_details`, the read that does carry it, never dropped as clean.
    """
    try:
        got = planner.call("search_items", query=deps.HOLD_MARK, limit=200, fields="full") or {}
    except (ToolFailed, NotPermitted, ServerUnreachable):
        return []
    out: list[tuple[str, str]] = []
    for row in (got.get("results") if isinstance(got, dict) else None) or []:
        item_id = str(row.get("id") or "") if isinstance(row, dict) else ""
        if not item_id:
            continue
        if "blocker" in row:
            blocker = str(row["blocker"] or "")
        else:
            try:
                details = planner.call("get_item_details", id=item_id) or {}
            except (ToolFailed, NotPermitted, ServerUnreachable):
                continue
            blocker = str(details.get("blocker") or "") if isinstance(details, dict) else ""
        if deps.is_hold(blocker):
            out.append((item_id, blocker))
    return out


def _recheck_holds(planner: Graphban, held: dict[str, list[str]], delegated: set[str],
                   repo: Path | None, base: str) -> None:
    """Lift every hold whose dependency is now in `base` — merged by `--merge`, by a person,
    or by a squash the forge reports (GRPH-950). Still missing stays held."""
    if not base or repo is None:
        return
    for item_id in list(held):
        absent, _ = deps.check(planner, item_id, repo, base)
        if absent:
            continue
        _lift(planner, item_id)
        held.pop(item_id, None)
        delegated.discard(item_id)
        observe.emit("hold_lifted", item=item_id,
                     detail=f"{item_id}: its dependencies are in {base}; offered again")


#: How often a running wave re-asks whether a held item's dependency has merged. The check is
#: a `related_work` read and, for an unmerged commit, one `gh pr view` per held item.
HOLD_RECHECK_SECONDS = 60


def _touchpoints_of(details: dict) -> list[str]:
    return [str(t) for t in (details.get("touchpoints") or []) if str(t).strip()]


def _offer_merged_for_review(planner: Graphban, item_id: str, details: dict, base: str) -> bool:
    """Move a git-merged, unsigned item to `review` so the drain staffs a reviewer (GRPH-1408).

    The work is already in `base`, so there is nothing to build — only a review to do,
    against what the base carries. True when the server accepted the move. A refusal is
    reported and False returned, and the caller holds the item as before: a hold is the
    honest fallback, never a silent drop.
    """
    branch = str(details.get("branch") or "").strip()
    where = f" (branch {branch})" if branch else ""
    note = (f"gbfleet: already in {base}{where} but unsigned — offered for review against the "
            f"merge in {base}, not rebuilt (GRPH-1408)")
    try:
        planner.call("update_item", id=item_id, status="review",
                     evidence=[{"kind": "note", "detail": note}])
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        observe.emit("review_offer_refused", item=item_id,
                     detail=f"{item_id} is git-merged and unsigned; moving it to review was "
                            f"refused: {exc}")
        return False
    observe.emit("review_offered", item=item_id, detail=note)
    return True


def _holds_sentence(item_id: str, areas: list[str]) -> str:
    """"SA-576 holds CueQueue.tsx, styles/queue.css" (GRPH-951) — the files, named."""
    if not areas:
        return f"{item_id} declares no touchpoints, so it holds its whole cluster"
    return f"{item_id} holds {', '.join(areas)}"


def _live_builder(item_id: str, details: dict, live: list | None,
                  owners: dict[str, object] | None) -> object | None:
    """GRPH-1414. A running child of this loop that is already building `item_id`, else None.

    A child busy in a long tool call (a test run) does not heartbeat; the server lease lapses
    and re-offers the item. The child is still alive and still on it, so a second delegation
    would put two builders on one item. Matched three ways: the child this loop handed the
    item to, a child whose roster reading holds or held it, a child on the item's branch.
    """
    branch = str(details.get("branch") or "")
    owner = (owners or {}).get(item_id)
    for child in live or []:
        if not getattr(child, "running", False):
            continue
        if child is owner or _child_touches(child, item_id):
            return child
        if branch and getattr(child, "branch", None) == branch:
            return child
    return None


def _delegate_next(
    planner: Graphban,
    agent_id: str,
    wave_name: str,
    delegated: set[str],
    request: str | None,
    prd: str | None = None,
    repo: Path | None = None,
    base: str = "",
    held: dict[str, list[str]] | None = None,
    merger: Merger | None = None,
    bound_out: dict | None = None,
    live: list | None = None,
    owners: dict[str, object] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Write the delegation for the seed of the next free cluster, before its seat is minted.

    PRD-35 D12. Returns the item id, or None when there was nothing to delegate — a seat
    spawned without an item makes no `delegate` call, and that absence is the honest
    record: the divvy will decide what the child actually claims.

    Lane and tier come from the brief unless `--tier` was given. This loop is a program;
    "follow the suggestion" is its stated policy (D5 forbids the SERVER defaulting, not a
    harness choosing to agree). A refused delegate — another planner's open one, a bounce
    pin — is reported and the spawn goes ahead; a delegation the child never claims reads
    `expired` on Live rather than being papered over here.
    """
    try:
        clusters = planner.call("collision_clusters", **_scope(prd))
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        observe.emit("delegate_skipped", detail=f"collision_clusters: {exc}")
        return None, None, None
    seed: str | None = None
    lane = "backend"
    want = str(request or "cheap")
    for cluster in clusters.get("clusters") or []:
        if not isinstance(cluster, dict) or cluster.get("held_by"):
            continue
        items = [i for i in (cluster.get("items") or []) if isinstance(i, str) and i]
        if not items:
            continue
        # GRPH-886: seeds are DAG-ready members, not items[0]. A review member, or an
        # unsigned next/backlog member already in `--base`, occupies what it touches. Do not
        # mark it delegated — hiding the merged id would make a colliding sibling the seed on
        # the next tick (SA-467/470).
        #
        # GRPH-951: ITS files, not the whole cluster. A cluster is joined by transitivity, and
        # SA-576 in review idled a wave of screens whose files it never named. A sibling whose
        # touchpoints collide with no occupier's (by the server's rule, mirrored in
        # `touchpoints.collide`) is still buildable.
        details_for: dict[str, dict] = {}
        occupiers: dict[str, list[str]] = {}
        for item_id in items:
            if item_id in delegated:
                continue
            try:
                got = planner.call("get_item_details", id=item_id) or {}
            except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
                observe.emit("delegate_refused", item=item_id, detail=str(exc))
                continue
            if not isinstance(got, dict):
                continue
            details_for[item_id] = got
            status = str(got.get("status") or "")
            if status == "review":
                occupiers[item_id] = _touchpoints_of(got)
            elif status in ("next", "backlog") and _already_in_base(got, repo, base):
                occupiers[item_id] = _touchpoints_of(got)
                # GRPH-1408: merged but unsigned is unreviewed work in the base. Holding it
                # forever left GRPH-1359 waiting on a reviewer started by hand. Moved to
                # `review`, it is a review row, and the drain staffs a reviewer for it.
                if _offer_merged_for_review(planner, item_id, got, base):
                    delegated.add(item_id)
                    continue
                observe.emit(
                    "delegate_held", item=item_id,
                    detail=f"already in {base} (unsigned, git-merged); "
                           + _holds_sentence(item_id, occupiers[item_id]),
                )
        for item_id in occupiers:
            details_for.pop(item_id, None)
        for item_id in list(details_for):
            by = [o for o, mine in occupiers.items()
                  if tp_mod.collide(_touchpoints_of(details_for[item_id]), mine)]
            if by:
                observe.emit("delegate_held", item=item_id,
                             detail="; ".join(_holds_sentence(o, occupiers[o]) for o in by))
                details_for.pop(item_id)
        # GRPH-950: EVERY ready member is dependency-checked, not only the seed. A bound child
        # may `claim_cluster` its seed's neighbours, and `claim_next` hands out whatever the
        # server calls ready — which a done-but-unmerged dependency is. A held member gets a
        # blocker, which every claim path refuses, so the check binds on all of them rather
        # than on the one item this loop happens to delegate.
        clean: list[str] = []
        for candidate in items:
            if candidate in delegated or candidate not in details_for:
                continue
            details = details_for[candidate]
            if not _seed_ready(details):
                continue
            # GRPH-1414. Not marked delegated: when that child exits the item is offered again.
            if (builder := _live_builder(candidate, details, live, owners)) is not None:
                observe.emit("redelegate_skipped_live_child", item=candidate,
                             detail=f"{candidate}: child {getattr(builder, 'branch', '?')} is still "
                                    "running on it; not delegated again")
                continue
            # GRPH-798. A child branches from `base`, so an item whose finished dependency is not
            # THERE would be built without it. SKIPPED, not fatal: the rest of the wave is still
            # buildable, and stopping would turn one unmerged branch into an idle fleet.
            absent, unknown = deps.check(planner, candidate, repo, base) if base else ([], [])
            if absent:
                observe.emit("delegate_held", item=candidate,
                             detail=deps.explain(candidate, absent, base))
                _hold(planner, candidate, details, absent, base)
                # Marked delegated so the next tick does not re-offer it and spin. It is held for
                # this wave, not refused forever — a merge changes the answer, and under
                # `--merge` (GRPH-846) the dependency it names is the next merge to finish.
                delegated.add(candidate)
                if held is not None:
                    held[candidate] = [str(d.get("id") or "") for d in absent]
                if merger is not None:
                    merger.note_hold(absent)
                continue
            for row in unknown:
                # Reported and NOT acted on. "I have never seen that commit" is not evidence that
                # the work is missing, and refusing on it would stop every wave on a fresh clone.
                observe.emit("dependency_unresolved", item=candidate,
                             detail=f"{row['id']}'s commit is not in this clone; not checked")
            clean.append(candidate)
        for candidate in clean:
            details = details_for[candidate]
            brief = details.get("brief") if isinstance(details.get("brief"), dict) else {}
            lane = str(((brief.get("lane") or {}).get("value")) or "backend")
            want = str(request or ((brief.get("tier") or {}).get("value")) or "cheap")
            note = f"gbfleet until, wave {wave_name}"
            code: str | None = None
            try:
                # PRD-36 D9: a BOUND seat. The server refuses one when the seed's areas are held
                # by someone else (D13). GRPH-885: do not fall back to an unbound delegate.
                # GRPH-1089: do not pass bound_only. A wave's finished bound workers review
                # each other; the flag is opt-in for a caller that wants the child to exit.
                reply = planner.call("delegate", id=candidate, lane=lane, tier=want, agent_id=agent_id,
                                     note=note, seat=True, wave=wave_name, **_seat_scope(prd))
                if bound_out is not None and isinstance(reply, dict):
                    bound_out["bound_only"] = bool(reply.get("bound_only"))
                got_code = reply.get("enrolment_code") if isinstance(reply, dict) else None
                code = str(got_code) if got_code else None
            except ToolFailed as exc:
                if "reserved_until=" in str(exc):
                    # GRPH-1332. Pinned to its author after a bounce: reported as a wait and
                    # NOT marked delegated, so a later tick re-offers it once the pin lapses.
                    stamp = str(exc).split("reserved_until=", 1)[1].split(")", 1)[0].strip()
                    observe.emit("delegate_pinned", item=candidate,
                                 detail=f"{candidate} is reserved for its author until {stamp}")
                    if bound_out is not None:
                        bound_out["pinned_until"] = stamp
                    continue
                observe.emit("bound_seat_refused", item=candidate, detail=str(exc))
                delegated.add(candidate)
                continue
            except (NotPermitted, ServerUnreachable) as exc:
                observe.emit("delegate_refused", item=candidate, detail=str(exc))
                continue
            seed = candidate
            delegated.add(seed)
            observe.emit("delegated", item=seed, lane=lane, tier=want, bound=bool(code))
            return seed, code, want
    # No cluster was delegable
    return None, None, None


#: How many distinct gate refusals a wave records. `until` re-reads the condition every
#: poll for as long as it lasts, and the JSON summary is not a log — the observe stream has
#: every one of them.
GATED_MAX = 20


def _no_room(wave: Wave, room: Headroom, live_n: int) -> bool:
    """Whether the machine says wait (GRPH-842). True means skip this spawn, not stop.

    **Not a `CapError`**, and the difference is the whole point. `cap` and `budget` are
    ceilings the operator set and a wave that hits one is finished. A full machine is
    transient: the loop is already holding live children, one of them will exit, and the
    memory comes back. Ending an unattended drain on a passing spike would throw away the
    remaining backlog for a condition that fixes itself.

    It cannot deadlock, and that is a property of the gate rather than luck: it never
    refuses when nothing is running, so the only state it can hold is one where a child is
    live — and a live child either finishes or is stopped by `watch_tick`.

    Reported once per change, not once per poll. The condition is re-read every second for
    as long as it lasts, and a line a second would bury the wave's own record in it.
    """
    return _room_refusal(wave, room, live_n) is not None


def _room_refusal(wave: Wave, room: Headroom, live_n: int) -> str | None:
    """`_no_room` with the gate's reason: None when there is room, else why not (GRPH-1387)."""
    verdict = room.allow(live_n)
    if verdict.allowed:
        return None
    if not wave.gated or wave.gated[-1] != verdict.reason:
        if len(wave.gated) < GATED_MAX:
            wave.gated.append(verdict.reason)
        observe.emit(
            "memory_gated", running=live_n, available=verdict.available,
            fits=verdict.fits, detail=verdict.reason,
        )
    return str(verdict.reason or "memory gate refused")


def _cap_children(wave, limits, item: str | None = None) -> None:
    """`--max-children` is the TOTAL this loop may spawn, not a per-tick cap.

    `up` applies it once at its single spawn; `until` spawns for the life of the wave and
    applied it nowhere, so a loop that spawned into nothing (see `_wanted_workers`) had no
    ceiling at all. A wave that reaches the cap with work still open ends `cap`, exit 1 —
    the operator raised the number knowingly or the loop was spawning wrong, and both are
    theirs to look at.

    **It counts distinct ITEMS, not processes** (GRPH-949). One wave at `--max-children 4`
    ended `cap` having spawned four processes for fewer items, because a child that exited
    without delivering was respawned for the same item and both counted. A child's items
    are what it held on the roster; one that held nothing counts on its own, so a loop
    spawning into nothing (GRPH-803) is still bounded. A respawn for an item an EXITED
    child already held is free — but never unbounded: the process count is still held to
    twice the cap, so an item that kills every child it gets cannot spin the wave forever.
    """
    counted, seen, exited_for = _children_counted(wave)
    spawned = len(wave.spawned)
    if spawned >= RESPAWN_CEILING * limits.max_children:
        raise CapError(
            "cap",
            f"max_children {limits.max_children} reached: {spawned} processes spawned this "
            f"wave for {counted} item(s) (respawn ceiling {RESPAWN_CEILING}x)",
        )
    if item and item in exited_for:
        return
    if counted >= limits.max_children:
        raise CapError(
            "cap",
            f"max_children {limits.max_children} reached: {counted} item(s) across "
            f"{spawned} process(es) spawned this wave",
        )


#: How many processes per `--max-children` a wave may spend on respawns (GRPH-949).
RESPAWN_CEILING = 2


def _children_counted(wave) -> tuple[int, set[str], set[str]]:
    """(slots counted against the cap, items seen, items whose child has exited)."""
    counted = 0
    seen: set[str] = set()
    exited_for: set[str] = set()
    for child in wave.spawned:
        items = [i for i in (getattr(child, "held_items", None) or []) if i]
        if not items:
            counted += 1
            continue
        if not any(i in seen for i in items):
            counted += 1
        seen.update(items)
        if not child.running:
            exited_for.update(items)
    return counted, seen, exited_for


def _wanted_workers(
    planner: Graphban, supervisor: Graphban, *, live_n: int, max_workers: int,
    prd: str | None = None,
) -> int:
    """How many more workers to start: free clusters, bounded by the slots live children leave."""
    # READY WORK BOUNDS EVERYTHING. `propose_allocation` describes the roster, not the
    # backlog: with zero free clusters it still maps every idle registered agent as a worker
    # "pulling from the review queue", and a child this loop spawned ten seconds ago is still
    # on that roster as `idle` for the whole presence TTL after it exited. Trusting that
    # count alone spawned a child every ~14 s for 18 minutes on the PRD-39 acceptance walk —
    # 63 registrations against a project with nothing left to do — and `--max-children`
    # never bound. Review work is not this function's job: the unheld-review branch in the
    # loop spawns for that, once, and counts its own failures.
    clusters = planner.call("collision_clusters", **_scope(prd))
    # FREE clusters, not all of them (GRPH-803). An item's lease and its areas are different
    # holds: `claimable` excludes an item somebody claimed and says nothing about one whose
    # files are reserved by an agent working something else. Counting every cluster spawned a
    # child per blocked cluster, each of which registered, was refused by `claim_cluster`, and
    # exited — one real wave burned all ten children and minted nothing, failing purely by
    # arriving early.
    free, blocked = _free_and_blocked(clusters)
    total = len(free)
    if total <= 0:
        if blocked:
            # Reported ON CHANGE, not every tick (GRPH-817). This loop polls once a second, so
            # the first version wrote 566 of a wave's 575 log lines — the same sentence, with a
            # countdown that RESETS when a holder renews its lease, which reads as a hang
            # during entirely normal work. A log that says the same thing 566 times is a log
            # nobody reads, and the nine lines that mattered were in it.
            # KEYED ON THE HOLDERS, not the sentence. `_waiting` embeds a countdown that
            # ticks every second, so deduplicating on the message would compare two strings
            # that always differ and print all 566 lines again — the fix reintroducing the
            # bug through its own dedup key.
            if _contention.changed(_holders_key(blocked)):
                observe.emit("contention", detail=_waiting(blocked, repeat=_contention.repeats))
        else:
            _contention.clear()
        return 0
    _contention.clear()
    if _soft_key(free):
        if _sharing.changed(_soft_key(free)):
            observe.emit("contention", detail=_overlapping(free))
    else:
        _sharing.clear()
    # SLOTS AND WORK ARE BOUNDED SEPARATELY (GRPH-1189). `collision_clusters` already leaves
    # out what the running children claimed, so `total` is work NO child has; subtracting
    # `live_n` from it counted every child twice, and each one hid a free cluster — a wave of
    # N free clusters ran about one at a time. `live_n` comes off the slots, not the work.
    #
    # `propose_allocation` is NOT a bound here. Its `workers` is the count of online agents on
    # the roster — every one of them, live children included — never a demand for more, so
    # `alloc.workers - live_n` is just "online agents that are not our children". This loop's
    # planner registers once and does not heartbeat, so a drain alone on its project soon has
    # only its children online, that term reads 0, and the free cluster hides again. Ghosts on
    # the roster can only inflate it, which `total` already bounds (the PRD-39 runaway).
    # The roster is still read: `fleet_status` is what requeues an offline child's items.
    supervisor.call("fleet_status")
    return max(0, min(max_workers - live_n, total))


def _take_seat(
    pool: list[Seat],
    planner: Graphban,
    agent_id: str,
    wave_name: str,
    server: str,
    api_key: str,
    *,
    mint_left: int,
    mint_deadline: float,
    sleep: Callable[[float], None],
    role: str = "worker",
    prd: str | None = None,
    review_only: bool = False,
) -> tuple[Seat, bool]:
    """Pre-minted pool first (workers). S6: all seats are workers now.

    A seat from the POOL carries whatever scope it was minted with, which for a `--seats` file
    is none. That is not silently accepted: `run` refuses `--prd` together with pre-minted
    seats, because a wave that reports as scoped while half its children are not is the failure
    mode this scope exists to remove.

    GRPH-988: `review_only` NEVER takes from the pool, for the same reason at one layer down.
    A seats file carries codes and not powers — there is no way to pre-mint a seat that may
    review but may not claim — so popping one here would hand a reviewer child the
    `claim_next` its own wave was forbidden. Enforced at the one place a seat is chosen rather
    than left to the startup refusal, because `--max-workers 0` is not the only way to reach
    the review branch: a wave at full worker capacity reaches it too, and its pool is live.
    """
    if pool and not review_only:
        return pool.pop(0), False
    code = _mint(planner, agent_id, wave_name, mint_left=mint_left,
                 mint_deadline=mint_deadline, sleep=sleep, role=role, prd=prd,
                 review_only=review_only)
    return Seat(code=code, server_url=server, api_key=api_key, role=role,
                review_only=review_only), True


def _mint(
    planner: Graphban,
    agent_id: str,
    wave_name: str,
    *,
    mint_left: int,
    mint_deadline: float,
    sleep: Callable[[float], None],
    role: str = "worker",
    prd: str | None = None,
    review_only: bool = False,
) -> str:
    last: Exception | None = None
    tries = max(1, mint_left)
    for attempt in range(tries):
        if attempt and time.monotonic() >= mint_deadline:
            break
        try:
            payload = planner.call(
                "mint_enrolment", agent_id=agent_id, role=role, wave=wave_name,
                review_only=review_only, **_seat_scope(prd),
            )
        except NotPermitted as exc:
            raise ConfigError(str(exc)) from exc
        except ServerUnreachable as exc:
            last = exc
        except ToolFailed as exc:
            if _is_config(exc):
                raise ConfigError(str(exc)) from exc
            last = exc
        else:
            code = payload.get("enrolment_code")
            if code:
                return str(code)
            last = CapError("mint", "mint_enrolment returned no enrolment_code")
        if attempt + 1 < tries:
            sleep(min(5.0, max(0.0, mint_deadline - time.monotonic()) / max(1, tries - attempt - 1)))
    raise CapError("cap", f"mint: {last}" if last else "mint budget exhausted")


def _is_config(exc: ToolFailed) -> bool:
    code = (exc.code or "").lower()
    message = str(exc).lower()
    if code in _CONFIG_CODES:
        return True
    if "quota" in message or "revoked" in message:
        return True
    if "may not" in message or "not planner" in message:
        return True
    return False


_REFUSED_AT_DONE = "refused at `done` ("


def _attests_suite(ev: dict) -> bool:
    """An attestation row that carries a `suite_green` predicate, passed or failed."""
    if ev.get("kind") != "attestation":
        return False
    return any(isinstance(p, dict) and p.get("name") == "suite_green"
               for p in ev.get("predicates") or [])


def _latest_refusal(row: dict) -> str:
    """The newest `done` refusal, or "" when CI has attested `suite_green` since it.

    A passing attestation lets the next sign_off through; a failing one is a red run a
    reviewer can bounce on. Either way the refusal no longer describes the row's gate.
    """
    for ev in reversed(row.get("evidence") or []):
        if not isinstance(ev, dict):
            continue
        if _attests_suite(ev):
            return ""
        detail = str(ev.get("detail") or "")
        if detail.startswith(_REFUSED_AT_DONE):
            return detail
    return ""


def ci_unattestable(row: dict) -> bool:
    """GRPH-1409. The newest `done` refusal on this row is `pr_ci_not_green` after the cooldown.

    Silence from CI past the cooldown does not change on its own, so another reviewer would
    only be refused the same way. Only that wording counts: a CI run that FAILED is a bounce
    a reviewer can make, so it keeps its reviewer.
    """
    detail = _latest_refusal(row)
    return (detail.startswith(_REFUSED_AT_DONE + "pr_ci_not_green)")
            and "cooldown over" in detail)


def ci_run_in_progress(repo: Path, row: dict) -> str:
    """GRPH-1417. A description of a CI run for this row's head that is queued or in progress,
    or "". Asks the forge (`gh run list`); any failure to ask reads as "none", so an
    unreachable forge falls back to the GRPH-1409 exit rather than waiting forever.
    """
    import subprocess
    m = re.search(r"commit ([0-9a-f]{7,40})", _latest_refusal(row))
    branch = str(row.get("branch") or "")
    if not m or not branch:
        return ""
    sha = m.group(1)
    try:
        done = subprocess.run(
            ["gh", "run", "list", "--branch", branch, "--limit", "50",
             "--json", "databaseId,status,name,headSha"],
            cwd=str(repo), capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            return ""
        runs = json.loads(done.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    for run in runs:
        if (str(run.get("headSha") or "").startswith(sha)
                and run.get("status") in ("queued", "in_progress", "waiting", "pending", "requested")):
            return f"run {run.get('databaseId')} {run.get('name') or ''} {run.get('status')}".replace("  ", " ")
    return ""


def _name_ci_stuck(report: Report, ci_stuck: list[dict]) -> Report:
    """GRPH-1409: a `review-unsigned` exit also names the rows no reviewer was spawned for."""
    if ci_stuck:
        stuck = "; ".join(ci_stuck_detail(r) for r in ci_stuck)
        report.detail = f"{report.detail}; {stuck}" if report.detail else stuck
    return report


def ci_stuck_detail(row: dict) -> str:
    detail = _latest_refusal(row).split("): ", 1)[-1]
    return (f"{row.get('id')}: sign_off refused pr_ci_not_green ({detail[:300]}); another "
            "reviewer cannot clear it — needs a CI attestation or an owner close")


def ci_verdict(repo: Path, branch: str) -> tuple[str, str, str]:
    """(state, head, failing_job) for `branch`'s PR (GRPH-1392): `propose.ci_rollup`.

    `unknown` (no gh, no PR, no checks, unreadable) does not hold a reviewer: a repo without
    CI must not wedge its review queue.
    """
    from . import propose
    return propose.ci_rollup(repo, branch)


def _ci_attested(rows: list[dict], repo: Path, logged: set, *,
                 planner: Graphban | None = None,
                 agent_id: str = "") -> tuple[list[dict], list[dict], list[dict]]:
    """Split unheld review rows into (spawnable, waiting on CI, blocked on red CI) (GRPH-1392).

    A reviewer cannot `sign_off` until CI has attested the head, so a spawn against pending
    CI is wasted: those rows are held back, logged once per head as `review_waits_on_ci`, and
    returned second so the loop keeps polling for them instead of reading them as unsigned.

    A red row is sent back to its builder with the failing tests named (`review_ci_red`), and
    no reviewer is spawned for it (GRPH-1471). The supervisor may not bounce (PRD-22 §4); the
    planner reopens the item to `next` with evidence. If that write is refused the row stays
    spawnable, so the reviewer — who can bounce — is still the fallback, never a wedged row. A
    reopened row has left `review`, so the loop drops it rather than reading it as unsigned work.
    """
    ready: list[dict] = []
    waiting: list[dict] = []
    blocked: list[dict] = []
    for row in rows:
        item = str(row.get("id") or "")
        state, head, job = ci_verdict(repo, str(row.get("branch") or ""))
        if state == "pending":
            key = ("review_waits_on_ci", item, head)
            if key not in logged:
                logged.add(key)
                observe.emit("review_waits_on_ci", item=item, commit=head,
                             detail=f"{item}: CI has not finished at {head[:12]}")
            waiting.append(row)
            continue
        if state == "red":
            key = ("review_ci_red", item, head)
            if key not in logged:
                logged.add(key)
                observe.emit("review_ci_red", item=item, commit=head, job=job,
                             detail=f"{item}: CI failed at {head[:12]}: {job}")
            if planner is not None and _block_ci_red(
                    planner, item, head, job, agent_id,
                    repo=repo, branch=str(row.get("branch") or "")):
                blocked.append(row)
                continue
        ready.append(row)
    return ready, waiting, blocked


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_JOB_ID = re.compile(r"/job/(\d+)")
_FAILED_LINE = re.compile(r"^\s*(?:\d{4}-\d\d-\d\dT[\d:.]+Z\s+)?(FAILED\s+\S.*)$")
_TALLY_LINE = re.compile(r"=+\s*(\d+ failed.*?)\s*=+\s*$|^(\d+ failed, .*)$")


def _failed_tests_from_log(log: str, max_lines: int = 10, max_bytes: int = 2048) -> str:
    """The pytest `FAILED <nodeid> - <message>` lines of a job log plus its last tally line."""
    log = _ANSI.sub("", log).replace("\r", "")
    failed: list[str] = []
    tally = ""
    for line in log.splitlines():
        m = _FAILED_LINE.match(line)
        if m and m.group(1).strip() not in failed:
            failed.append(m.group(1).strip())
        t = _TALLY_LINE.search(re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s+", "", line.strip()))
        if t:
            tally = (t.group(1) or t.group(2) or "").strip()
    out = failed[:max_lines]
    if tally:
        out.append(tally)
    return "\n".join(out).encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def failed_test_summary(repo: Path, branch: str) -> str:
    """The failing tests of `branch`'s red CI jobs, or why they could not be read (GRPH-1458).

    `gh` works here (the planner's machine) and not in a child's sandbox, so the cause is read
    once, now, and travels in the note. A bare job name is never the whole answer: when the
    log cannot be read the summary says so.
    """
    from . import propose
    not_failed = propose._PASSING | propose._RUNNING
    try:
        done = propose._gh(repo, "pr", "view", branch, "--json", "statusCheckRollup")
        if done.returncode != 0:
            return ("failing test names unavailable: gh pr view failed "
                    f"({(done.stderr or '').strip()[:200]})")
        rollup = json.loads(done.stdout or "{}").get("statusCheckRollup") or []
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return f"failing test names unavailable: could not read the check rollup ({exc})"
    parts: list[str] = []
    for check in rollup:
        verdict = str(check.get("conclusion") or check.get("state") or "").upper()
        if verdict in not_failed:
            continue
        name = str(check.get("name") or check.get("context") or "unknown job")
        m = _JOB_ID.search(str(check.get("detailsUrl") or check.get("targetUrl") or ""))
        if not m:
            parts.append(f"{name}: log could not be read (no job id in the check URL)")
            continue
        try:
            got = propose._gh(repo, "api",
                              f"repos/{{owner}}/{{repo}}/actions/jobs/{m.group(1)}/logs")
        except (OSError, subprocess.SubprocessError) as exc:
            parts.append(f"{name}: log could not be read ({exc})")
            continue
        if got.returncode != 0:
            parts.append(f"{name}: log could not be read "
                         f"(gh api: {(got.stderr or '').strip()[:200]})")
            continue
        body = _failed_tests_from_log(got.stdout or "")
        parts.append(f"{name}:\n{body}" if body
                     else f"{name}: log read, no pytest FAILED lines found")
    return "\n".join(parts) or "failing test names unavailable: no failed check in the rollup"


#: The prefix `merge.reopen_on_red` writes on `bounce_reason` (backend/app/services/fleet.py).
REOPEN_ON_RED = "merge.reopen_on_red"
_REOPEN_HEAD = re.compile(r"signed-off head ([0-9a-f]{7,40})")


def red_tests_note_marker(head: str) -> str:
    return f"gbfleet: failing tests at reopened head {head or 'unknown'}"


def name_red_tests(planner: Graphban, repo: Path, item_id: str, agent_id: str = "") -> str | None:
    """Name the failing tests on an item `merge.reopen_on_red` sent back (GRPH-1458).

    The server reopens with the failing JOB names only ("failing jobs: fleet, windows"), and
    the resumed child cannot read the log — its sandbox refuses `gh`, and should. `until`
    runs where `gh` works, so before the next child is spawned on the item it reads the red
    jobs' logs and appends their pytest FAILED lines as an evidence note, which the child
    reads in `get_item_details`. Once per reopened head: the note is the record, so a
    restarted loop does not write it twice. Returns the note written, else None.
    """
    try:
        details = planner.call("get_item_details", id=item_id)
    except Exception:  # noqa: BLE001 - a read failure is silence, as in `_item_brief`
        return None
    if not isinstance(details, dict):
        return None
    reason = str(details.get("bounce_reason") or "")
    if not reason.startswith(REOPEN_ON_RED):
        return None
    m = _REOPEN_HEAD.search(reason)
    marker = red_tests_note_marker(m.group(1) if m else "")
    for ev in details.get("evidence") or []:
        if isinstance(ev, dict) and str(ev.get("detail") or "").startswith(marker):
            return None
    branch = str(details.get("branch") or "").strip()
    tests = (failed_test_summary(repo, branch) if branch else
             "failing test names unavailable: the item records no branch to read CI for")
    note = f"{marker} (read by gbfleet from the CI job logs; gh is not needed):\n{tests}"
    args: dict = {"id": item_id, "evidence": [{"kind": "note", "detail": note}]}
    if agent_id:
        args["agent_id"] = agent_id
    try:
        planner.call("update_item", **args)
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        observe.emit("red_tests_refused", item=item_id,
                     detail=f"{item_id}: could not record the failing tests: {exc}")
        return None
    observe.emit("red_tests_named", item=item_id, detail=note[:400])
    return note


REVIEW_CI_RED = "gbfleet.review_ci_red"


def ci_red_reopen_note(head: str, job: str, tests: str = "") -> str:
    """Evidence on a review row whose CI failed, sent back to its builder (GRPH-1471)."""
    lead = (f"{REVIEW_CI_RED}: CI red at {head[:12]} — failing check: {job}. "
            "gbfleet held review until the branch is green; fix on the branch, then move "
            "back to review.")
    if tests:
        return f"{lead}\nFailing tests:\n{tests}"
    return lead


def ci_red_blocker(head: str, job: str, tests: str = "") -> str:
    """Legacy name for the prose in a red-CI reopen note (GRPH-1392, GRPH-1471)."""
    return ci_red_reopen_note(head, job, tests)


def _block_ci_red(planner: Graphban, item: str, head: str, job: str, agent_id: str,
                  *, repo: Path | None = None, branch: str = "") -> bool:
    """Reopen a review row to `next` when its CI is red. False when the write was refused.

    GRPH-1471: a free-text `blocked` blocker is not claimable — the builder must get the item
    back with the failing tests named, not a sentence that nothing hands out.
    """
    tests = failed_test_summary(repo, branch) if repo is not None and branch else ""
    note = ci_red_reopen_note(head, job, tests)
    args: dict = {
        "id": item,
        "status": "next",
        "blocker": "",
        "evidence": [{"kind": "note", "detail": note}],
    }
    if agent_id:
        args["agent_id"] = agent_id
    try:
        planner.call("update_item", **args)
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        observe.emit("review_ci_red_unrecorded", item=item, detail=str(exc))
        return False
    observe.emit("review_ci_red_reopened", item=item, commit=head, job=job,
                 detail=f"{item}: reopened on {job} at {head[:12]}")
    return True


def review_held(row: dict) -> bool:
    """Whether somebody is reviewing this row RIGHT NOW (GRPH-1254).

    `review_claimed_by` is the raw column, and a reviewer that dies leaves it set for good: only
    a verdict or the holder's own `release_item` clears it. Reading it as "held" kept a wave
    idle for over an hour on GRPH-1248, logging `held by review; no expiry reported` while the
    holder was offline and retired and the server would already have handed the item to the
    next `claim_review`. The server says when a hold has lapsed (`review_claim_lapse`, present
    only when it has), so liveness has one definition and it is the server's.

    An older server never sends the field, and then this reads exactly as before.
    """
    return bool(row.get("review_claimed_by")) and not row.get("review_claim_lapse")


def _review_rows(planner: Graphban) -> list[dict]:
    """Raises on a failed read. An empty list means looked and found none."""
    payload = planner.call("search_items", status="review", fields="full", limit=10_000)
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ToolFailed("search_items", "error", "review listing carried no results list")
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def resolve_prd(planner: Graphban, prd: str) -> str:
    """The canonical key of the PRD `--prd` names (GRPH-1190).

    `get_prd` resolves any spelling the server accepts and answers with the current key as
    `id`. A refusal, or a server that cannot be reached, ends the run with the server's
    reason: comparing against the raw string instead is how the PRD's own review rows came
    to be listed outside it while the run exited idle.
    """
    try:
        got = planner.call("get_prd", prd_id=prd)
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        raise ConfigError(f"--prd {prd} could not be resolved: {exc}") from exc
    key = str(got.get("id") or "")
    if not key:
        raise ConfigError(f"--prd {prd} could not be resolved: get_prd answered with no id")
    return key


def _staff_every_row(planner: Graphban) -> bool:
    """Whether `review.staff_every_row` is effectively on (GRPH-1273). Read each cycle from the
    server's S2a resolution. A server that does not answer, or does not know the rule, is off:
    the narrower scope is the one this loop always had."""
    try:
        got = planner.call("get_rule_toggles")
    except (NotPermitted, ToolFailed, ServerUnreachable):
        return False
    for t in (got or {}).get("toggles") or []:
        if t.get("rule_id") == STAFF_EVERY_ROW:
            on = t.get("on")
            return bool(on.get("value") if isinstance(on, dict) else on)
    return False


def _split_by_prd(rows: list[dict], prd: str | None) -> tuple[list[dict], list[dict]]:
    """(this PRD's review rows, everyone else's) — GRPH-1188. Without `--prd` every row is
    this run's, as before. With it, a row with another `prd_id` or none is outside."""
    if not prd:
        return rows, []
    return ([r for r in rows if r.get("prd_id") == prd],
            [r for r in rows if r.get("prd_id") != prd])


def _wait_ids(planner: Graphban) -> list[str]:
    """Raises on a failed read. An empty list means looked and found none."""
    return wait_mod.ids(planner)


def _fresh_holdings(agent: dict) -> list[dict]:
    """Holdings that are still a current claim. A stale phase is a dead lease."""
    out = []
    for holding in agent.get("holdings") or []:
        if not isinstance(holding, dict) or not holding.get("id"):
            continue
        if holding.get("phase") == "stale":
            continue
        out.append(holding)
    return out


def _same_path_key(path: str) -> str:
    """One spelling per directory: symlinks resolved (macOS /var -> /private/var)
    and, on Windows, case and separators folded."""
    try:
        resolved = os.path.realpath(path)
    except OSError:
        resolved = path
    return os.path.normcase(resolved)


def _agent_for_child(child: Child, agents: list[dict]) -> dict | None:
    agent_id = getattr(child, "agent_id", None)
    if agent_id:
        for agent in agents:
            if agent.get("id") == agent_id:
                return agent
    tree = str(getattr(child, "worktree", "") or "")
    if not tree:
        return None
    wanted = _same_path_key(tree)
    for agent in agents:
        got = str(agent.get("worktree") or "")
        if not got:
            continue
        if _same_path_key(got) == wanted:
            return agent
    return None


def _in_claim_window(child, now: float | None = None) -> bool:
    """A child that registered under `CLAIM_WINDOW` ago and has never held anything.

    It has not had the turns it needs to claim, so it is the work starting, not a
    process holding nothing. A hand-built child with no start time is not in it.
    """
    if _has_claimed(child) or list(getattr(child, "ever_held", None) or []):
        return False
    started = getattr(child, "started_at", None)
    if not isinstance(started, (int, float)):
        return False
    registered = started + float(getattr(child, "registration_latency", None) or 0.0)
    now = time.monotonic() if now is None else now
    return now - registered < CLAIM_WINDOW


def _child_currently_holds(child: Child, agents: list[dict]) -> bool:
    """True when this live child's roster row has a non-stale holding.

    Review claims count here and nowhere that salvages or releases: this only
    decides whether the process is in the middle of something. An empty list
    is a child that registered and claimed nothing (GRPH-1401).
    """
    agent = _agent_for_child(child, agents)
    return bool(agent and _fresh_holdings(agent))


def _roster_agents(supervisor: Graphban) -> list[dict] | None:
    """The roster's agents, or None when it could not be read. Unknown is not empty."""
    try:
        roster = supervisor.call("fleet_status") or {}
    except ServerUnreachable as exc:
        watchdog_mod.current.retry("fleet_status", exc)
        return None
    return [a for a in (roster.get("agents") or []) if isinstance(a, dict)]


def _idle_blocked(supervisor: Graphban, live: list) -> bool:
    """Whether this poll still has a lease to wait on.

    A non-stale build lease anywhere on the roster is one (``_any_holdings``,
    GRPH-1001). A live child of this wave that currently holds an item is the
    other, and a review claim counts: the process is the work, which is the
    half ``_any_holdings`` deliberately does not answer. A running child whose
    holdings are empty is neither, once its claim window (`CLAIM_WINDOW`) is over.
    """
    agents = _roster_agents(supervisor)
    if agents is None:
        # A missed read must not look like a clear backlog: any live child blocks.
        return bool(live)
    for agent in agents:
        if any((h or {}).get("phase") != "stale" for h in build_holdings(agent)):
            return True
    return any(_child_currently_holds(child, agents) or _in_claim_window(child)
               for child in live)


def _stop_children_holding_nothing(
    wave: Wave, children: list[Child], supervisor: Graphban, planner: Graphban, base: str,
) -> None:
    """Stop a child that is still running with nothing held, and reap its tree.

    Leaving it running is how the next cycle adopts a process that holds nothing
    and then never goes idle (GRPH-1401). A child that holds an item stays, and so
    does one still inside its claim window: it has not had the turns to claim yet.
    """
    agents = _roster_agents(supervisor)
    if agents is None:
        return  # roster unread: stop nothing, a child may hold an item
    quitting = [c for c in children
                if c.running and not _child_currently_holds(c, agents)
                and not _in_claim_window(c)]
    for child in quitting:
        stop(child, Reason.SHUTDOWN)
    gone = [c for c in quitting if not c.running]
    if not gone:
        return
    _report_exits(gone, supervisor, wave)
    _reap_all(wave, gone, client=planner, base_branch=base)


def _any_holdings(supervisor: Graphban) -> bool:
    """True while some LIVE agent holds something. Unknown (unreachable) is False here because
    the caller pairs it with `live`; a stale hold is a dead agent's, not a holding."""
    try:
        roster = supervisor.call("fleet_status")
    except ServerUnreachable as exc:
        watchdog_mod.current.retry("fleet_status", exc)
        return False
    for agent in roster.get("agents") or []:
        # A `stale` hold is a lease older than the presence TTL — a builder that exited with
        # its item in review still shows it. Counting that as "somebody is working" kept a
        # wave waiting on a dead process forever (PRD-39 acceptance walk, run 3).
        # Build leases only (GRPH-1001): a reviewer holding an item is not a builder working,
        # and counting it kept `until` spinning while the only "work in progress" was a review.
        if any((h or {}).get("phase") != "stale" for h in build_holdings(agent)):
            return True
    return False


def emit(report: Report, out=None) -> None:
    """Human lines, then one JSON object. Machines key on the JSON."""
    import sys
    out = sys.stdout if out is None else out
    if report.wave is not None:
        from .cli import report as wave_report
        wave_report(report.wave, out=out)
    print(json.dumps(report.as_json()), file=out)
