"""`gbfleet until` — planner-mode loop, not a thicker supervisor (P30 D1).

Same binary, in-process: `start_one` / the watch loop / reap as Python calls, not MCP
and not a socket. Two Graphban clients share one key and split by allowlist. The
supervisor client stays `{fleet_status, propose_allocation}`. Minting lives on the
planner client. Mixing those into `ALLOWED_TOOLS` is the widening G5 forbids.

Idle is not "the last child exited." Idle is: no ready non-colliding work, no unsigned
`review`, and no live worker still holding a lease. Typed human waits (D11) are not
ready work; a wave that has only those left is `idle-with-waits`. A wave that exits 0
with leftover `review` is a failed run (`review-unsigned`).
"""

from __future__ import annotations

import re

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
from .client import ALLOWED_TOOLS, Graphban, NotPermitted, ServerUnreachable, ToolFailed
from .lock import hold
from .seat import Seat
from .tiers import TierTable
from . import matrix as matrix_mod
from .spawn import Child, build_holdings
from . import spend as spend_mod
from . import touchpoints as tp_mod
from .spawn import VendorLimit
from .headroom import Headroom
from .supervisor import (
    _declared_into,
    DEFAULT_MAX_WORKERS, AllocationRead, LaunchFactory, Limits, Merger, Wave, WaveBase,
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
})

EMPTY_TICKS = 3
MINT_TRIES = 3
MINT_BUDGET_S = 30.0
#: S6 (PRD-39 D-i): consecutive spawns that exited against unheld review rows before
#: until stops trying. Re-keyed off the fact it measures (a child exited and the
#: review rows are still unheld) rather than off a role that no longer exists.
REVIEWER_FAILS = 3

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
    detail: str = ""
    wave: Wave | None = None

    def as_json(self) -> dict:
        payload = {
            "ok": self.ok,
            "reason": self.reason,
            "exit": self.exit,
            "spawned": self.spawned,
            "minted": self.minted,
            "waits": list(self.waits),
            "review": list(self.review),
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
            # GRPH-982: branches whose child ran a model nobody asked for. Always present and
            # empty by default, so "none observed" cannot be read as "not checked".
            "substituted": {k: list(v) for k, v in (self.wave.substituted.items()
                                                    if self.wave else [])},
        }
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

            children: list[Child] = list(leftover)
            roster_path = adopt_mod.children_path(repo, state)

            def persist() -> None:
                adopt_mod.persist(roster_path, children)

            persist()
            identity = _identify(planner, repo, adapter)
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
            result.wave = wave
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


def _identify(planner: Graphban, repo: Path, adapter: str = "") -> dict:
    """Register this process. A key that cannot mint is refused here, not after a wave."""
    label = f"gbfleet until ({adapter})" if adapter else "gbfleet until"
    try:
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
    tiers = tiers or TierTable()
    minted = minted_start
    mint_deadline = time.monotonic() + mint_budget
    mint_left = mint_tries
    review_fails = 0
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
    _adopt_holds(planner, held, delegated)
    rechecked = 0.0

    while True:
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
                rows_for_reap = _review_rows(planner)
            except (NotPermitted, ToolFailed, ServerUnreachable):
                rows_for_reap = []
            unheld_at_reap = [r for r in rows_for_reap if not r.get("review_claimed_by")]
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
            children[:] = [c for c in children if c.running]
            persist()
            if any("handoff-failed" in f for f in wave.failures):
                wave.reason = "handoff-failed"
                return _finish(wave, "handoff-failed", 1, minted, planner)

        live = [c for c in children if c.running]
        holdings = _any_holdings(supervisor)
        try:
            rows = _review_rows(planner)
            reviews = [str(r["id"]) for r in rows]
            waits = _wait_ids(planner)
        except NotPermitted as exc:
            raise ConfigError(f"cannot classify review/waits: {exc}") from exc
        except ToolFailed as exc:
            raise ConfigError(f"cannot classify review/waits: {exc}") from exc
        except ServerUnreachable as exc:
            # Unknown is not empty: leftover review must not look like idle.
            if live or holdings:
                sleep(poll)
                continue
            raise CapError(
                "cap",
                f"search_items unreachable; leftover review is unknown, not empty ({exc})",
            ) from exc

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
        except ServerUnreachable:
            # D-i: no new spawns while unreachable. Live children run to their lease.
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
            except ServerUnreachable:
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
            seed, code, want = _delegate_next(planner, agent_id, wave_name, delegated,
                                              request, prd, repo, base, held=held,
                                              merger=merger)
            if seed is None:
                # No takeable work this tick. Fall through to the idle counter below.
                pass
            elif code:
                empty = 0
                seat = Seat(shared=dict(shared or {}),
                            code=code, server_url=server, api_key=api_key, role="worker",
                            item=seed)
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
                                         measured=measured, installed=matrix_mod.installed_checker(),
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
                _spawn_one(
                    wave, children, occupied, persist, seat, factory,
                    repo, workspace, wave_name, supervisor, limits, planner, debug,
                    base=base,
                )
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
                                         measured=measured, installed=matrix_mod.installed_checker(),
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
                _spawn_one(
                    wave, children, occupied, persist, seat, factory,
                    repo, workspace, wave_name, supervisor, limits, planner, debug,
                    base=base,
                )
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
        unheld_review = [r for r in rows if not r.get("review_claimed_by")]
        # No memory gate on this branch, deliberately (GRPH-842): `not live` means nothing
        # is running, and the gate never refuses the first child — so a check here could
        # only ever cost a `vm_stat` per poll and answer yes. **If that guard goes, this
        # needs `_no_room` like the worker branch above.**
        if unheld_review and need <= 0 and not live:
            empty = 0
            if review_fails >= REVIEWER_FAILS:
                wave.reason = "review-unsigned"
                return _finish(wave, "review-unsigned", 1, minted, planner,
                               review=reviews, waits=waits)
            seat, minted_one = _take_seat(
                pool, planner, agent_id, wave_name, server, api_key,
                mint_left=mint_left, mint_deadline=mint_deadline, sleep=sleep,
                role="worker", prd=prd, review_only=True,
            )
            if minted_one:
                minted += 1
                mint_left -= 1
            if adapter:
                seat = replace(seat, declare=matrix_mod.declaration(adapter, "", None, matrix))
            _cap_children(wave, limits)
            before = len(wave.spawned)
            _spawn_one(
                wave, children, occupied, persist, seat, launch_factory,
                repo, workspace, wave_name, supervisor, limits, planner, debug,
                base=base,
            )
            if len(wave.spawned) == before:
                review_fails += 1
            continue

        if live or holdings:
            empty = 0
            sleep(poll)
            continue

        unheld = [r for r in rows if not r.get("review_claimed_by")]
        if unheld:
            wave.reason = "review-unsigned"
            return _finish(wave, "review-unsigned", 1, minted, planner, review=reviews,
                           waits=waits)

        empty += 1
        if empty >= empty_ticks:
            if waits:
                wave.reason = "idle-with-waits"
                return _finish(wave, "idle-with-waits", 0, minted, planner, waits=waits)
            wave.reason = "idle"
            return _finish(wave, "idle", 0, minted, planner)
        sleep(poll)


def _finish(
    wave: Wave, reason: str, exit_code: int, minted: int, planner: Graphban,
    *, review: list[str] | None = None, waits: list[str] | None = None,
) -> Report:
    if waits is None:
        try:
            waits = _wait_ids(planner)
        except (NotPermitted, ToolFailed, ServerUnreachable):
            waits = []
    if review is None:
        try:
            review = _review_ids(planner)
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
        into=children, persist=persist, base=base,
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
    try:
        got = planner.call("search_items", query=deps.HOLD_MARK, limit=200, fields="full") or {}
    except (ToolFailed, NotPermitted, ServerUnreachable):
        got = {}
    for row in (got.get("results") if isinstance(got, dict) else None) or []:
        if isinstance(row, dict) and deps.is_hold(str(row.get("blocker") or "")):
            item_id = str(row.get("id") or "")
            seen.add(item_id)
            out.append({"item": item_id, "waiting_on": str(row.get("blocker") or ""),
                        "already_held": True})
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
    still names an unfinished dependency are not seeds (GRPH-886).
    """
    if str(details.get("status") or "") not in ("next", "backlog"):
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


def _adopt_holds(planner: Graphban, held: dict[str, list[str]], delegated: set[str]) -> None:
    """Holds an earlier wave wrote, taken over so this wave lifts them when their PR merges.

    A blocker nobody re-checks strands its item after the merge it waits on — worse than the
    duplicate it prevented, because it looks deliberate.
    """
    try:
        got = planner.call("search_items", query=deps.HOLD_MARK, limit=200, fields="full") or {}
    except (ToolFailed, NotPermitted, ServerUnreachable):
        return
    for row in (got.get("results") if isinstance(got, dict) else None) or []:
        if not isinstance(row, dict) or not deps.is_hold(str(row.get("blocker") or "")):
            continue
        item_id = str(row.get("id") or "")
        if item_id and item_id not in held:
            held[item_id] = []
            delegated.add(item_id)


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


def _holds_sentence(item_id: str, areas: list[str]) -> str:
    """"SA-576 holds CueQueue.tsx, styles/queue.css" (GRPH-951) — the files, named."""
    if not areas:
        return f"{item_id} declares no touchpoints, so it holds its whole cluster"
    return f"{item_id} holds {', '.join(areas)}"


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
                reply = planner.call("delegate", id=candidate, lane=lane, tier=want, agent_id=agent_id,
                                     note=note, seat=True, wave=wave_name, **_seat_scope(prd))
                got_code = reply.get("enrolment_code") if isinstance(reply, dict) else None
                code = str(got_code) if got_code else None
            except ToolFailed as exc:
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
    verdict = room.allow(live_n)
    if verdict.allowed:
        return False
    if not wave.gated or wave.gated[-1] != verdict.reason:
        if len(wave.gated) < GATED_MAX:
            wave.gated.append(verdict.reason)
        observe.emit(
            "memory_gated", running=live_n, available=verdict.available,
            fits=verdict.fits, detail=verdict.reason,
        )
    return True


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
    """How many more workers to start. Cold start reads clusters; a live roster reads the mix."""
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
    roster = supervisor.call("fleet_status")
    agents = [a for a in (roster.get("agents") or []) if a.get("id")]
    if not agents:
        return max(0, min(max_workers, total) - live_n)
    alloc = AllocationRead.of(planner.call("propose_allocation"))
    if alloc.uninformative:
        return max(0, min(max_workers, total) - live_n)
    return max(0, min(max_workers, alloc.workers, total) - live_n)


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


def _review_rows(planner: Graphban) -> list[dict]:
    """Raises on a failed read. An empty list means looked and found none."""
    payload = planner.call("search_items", status="review", fields="full", limit=10_000)
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ToolFailed("search_items", "error", "review listing carried no results list")
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def _review_ids(planner: Graphban) -> list[str]:
    return [str(r["id"]) for r in _review_rows(planner)]


def _wait_ids(planner: Graphban) -> list[str]:
    """Raises on a failed read. An empty list means looked and found none."""
    return wait_mod.ids(planner)


def _any_holdings(supervisor: Graphban) -> bool:
    """True while some LIVE agent holds something. Unknown (unreachable) is False here because
    the caller pairs it with `live`; a stale hold is a dead agent's, not a holding."""
    try:
        roster = supervisor.call("fleet_status")
    except ServerUnreachable:
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
