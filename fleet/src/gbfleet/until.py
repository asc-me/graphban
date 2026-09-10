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
from .spawn import Child
from . import spend as spend_mod
from .spawn import VendorLimit
from .headroom import Headroom
from .supervisor import (
    _declared_into,
    DEFAULT_MAX_WORKERS, AllocationRead, LaunchFactory, Limits, Wave, _reap_all, _rooted,
    _report_exits, _start, item_status, publish_salvaged, watch_tick,
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
    # PRD-35 D12: the delegation is written BEFORE the seat is minted, for the seed of the
    # next free cluster; `get_item_details` is where the brief (lane/tier suggestion) lives.
    "get_item_details",
    "delegate",
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
) -> Report:
    """Hold the repo lock and run until idle, a cap, or a config refusal.

    `request` is what every delegation this loop writes will REQUEST (PRD-35 D5). None means
    follow the brief's suggestion — a stated policy of this program, not a server default.
    `tiers` is the operator's tier table (PRD-36 D6): when the requested tier is mapped,
    `launch_for(adapter, model)` builds the child's launch; otherwise `launch_factory` does.
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
                publish_salvaged(wave, repo, recovered.salvaged, client=planner)

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
            )
            result.wave = wave
            minted = result.minted
            persist()
            return result
    except ConfigError as exc:
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
    """Capabilities and spend for this item, when the planner can read it.

    Failures are silence: resolving without capabilities is the pre-S2 path, not a crash.
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
) -> Report:
    from .mcp import read_preferences
    profile, policy, pref_note, measured, cap_measured = read_preferences(supervisor)
    observe.emit("preferences", detail=pref_note)
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
    remote = wt_mod.remote_for(repo)
    base = wt_mod.default_ref(repo, remote) if remote else ""
    if base:
        wt_mod.refresh_ref(repo, remote, base)

    while True:
        watch_tick(wave, children, limits, supervisor, debug=debug, persist=persist)
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
            _reap_all(wave, finished)
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
            empty = 0
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
            seed, code, want = _delegate_next(planner, agent_id, wave_name, delegated,
                                              request, prd, repo, base)
            if code:
                seat = Seat(shared=dict(shared or {}),
                            code=code, server_url=server, api_key=api_key, role="worker",
                            item=seed)
                minted += 1
            else:
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
                                     spend=(brief or {}).get("spend"))
                if res.winner is not None:
                    factory = launch_for(res.winner.harness, res.winner.model)
                    chosen = (res.winner.harness, res.winner.model)
                    observe.emit("resolved", item=seed, **{k: v for k, v in res.explain().items() if k in ("winner", "dropped", "eligible", "profile", "stages", "capabilities")})
                else:
                    observe.emit("resolve_refused", item=seed, detail=res.refused)
            # GRPH-732: the child is told what it is, because only this side knows.
            if chosen[0]:
                seat = replace(seat, declare=matrix_mod.declaration(chosen[0], chosen[1], want or None, matrix))
            _cap_children(wave, limits)
            _spawn_one(
                wave, children, occupied, persist, seat, factory,
                repo, workspace, wave_name, supervisor, limits, planner, debug,
            )
            continue

        # S6 (PRD-39 D-i): unheld review rows need a merged worker. Re-keyed off the
        # fact it measures — a child was spawned against unheld review rows, exited,
        # and the rows are still unheld — not off a role that no longer exists.
        # A live child blocks a second spawn (just as live_reviewers did before).
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
                role="worker", prd=prd,
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
        into=children, persist=persist,
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


def plan(planner: Graphban, prd: str | None, max_workers: int) -> dict:
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
                  "areas": c.get("areas") or []} for c in free],
        "held": [{"items": c.get("items") or [], "held_by": c.get("held_by") or [],
                  "free_in": c.get("free_in"),
                  # WHICH area the hold covers and by which rule (GRPH-833). "Held by SA-A39"
                  # sends the reader looking for SA-A39; this says what the collision actually
                  # is, which is the half an operator was left to infer — and inferred wrong.
                  "because": c.get("held_because") or []} for c in blocked],
        # The reservation table itself, keyed on the HOLD rather than on the cluster. A
        # cluster leaves the partition the moment its item is claimed, taking its reservation
        # off every read while that reservation goes on blocking everyone — so a wave with no
        # free clusters and no `held` rows had nothing to show for itself at all.
        "holds": clusters.get("holds") or [],
        "capped_by_max_workers": len(free) > max_workers,
    }


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


def _delegate_next(
    planner: Graphban,
    agent_id: str,
    wave_name: str,
    delegated: set[str],
    request: str | None,
    prd: str | None = None,
    repo: Path | None = None,
    base: str = "",
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
    for cluster in clusters.get("clusters") or []:
        if not isinstance(cluster, dict) or cluster.get("held_by"):
            continue
        items = [i for i in (cluster.get("items") or []) if isinstance(i, str) and i]
        if not items or items[0] in delegated:
            continue
        candidate = items[0]
        # GRPH-798. A child branches from `base`, so an item whose finished dependency is not
        # THERE would be built without it. SKIPPED, not fatal: the rest of the wave is still
        # buildable, and stopping would turn one unmerged branch into an idle fleet.
        absent, unknown = deps.check(planner, candidate, repo, base) if base else ([], [])
        if absent:
            observe.emit("delegate_held", item=candidate,
                         detail=deps.explain(candidate, absent, base))
            # Marked delegated so the next tick does not re-offer it and spin. It is held for
            # this wave, not refused forever — a merge changes the answer.
            delegated.add(candidate)
            continue
        for row in unknown:
            # Reported and NOT acted on. "I have never seen that commit" is not evidence that
            # the work is missing, and refusing on it would stop every wave on a fresh clone.
            observe.emit("dependency_unresolved", item=candidate,
                         detail=f"{row['id']}'s commit is not in this clone; not checked")
        seed = candidate
        break
    if seed is None:
        return None, None, None
    try:
        details = planner.call("get_item_details", id=seed) or {}
        brief = details.get("brief") if isinstance(details.get("brief"), dict) else {}
        lane = str(((brief.get("lane") or {}).get("value")) or "backend")
        want = str(request or ((brief.get("tier") or {}).get("value")) or "cheap")
    except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
        observe.emit("delegate_refused", item=seed, detail=str(exc))
        return None, None, None
    note = f"gbfleet until, wave {wave_name}"
    code: str | None = None
    try:
        # PRD-36 D9: a BOUND seat. The server refuses one when the seed's areas are held
        # by someone else (D13); then the delegation is written without a seat and the
        # divvy decides, exactly as before PRD-36.
        reply = planner.call("delegate", id=seed, lane=lane, tier=want, agent_id=agent_id,
                             note=note, seat=True, wave=wave_name, **_seat_scope(prd))
        got = reply.get("enrolment_code") if isinstance(reply, dict) else None
        code = str(got) if got else None
    except ToolFailed as exc:
        observe.emit("bound_seat_refused", item=seed, detail=str(exc))
        try:
            planner.call("delegate", id=seed, lane=lane, tier=want, agent_id=agent_id, note=note,
                         **_seat_scope(prd))
        except (ToolFailed, NotPermitted, ServerUnreachable) as exc2:
            observe.emit("delegate_refused", item=seed, detail=str(exc2))
            return None, None, None
    except (NotPermitted, ServerUnreachable) as exc:
        observe.emit("delegate_refused", item=seed, detail=str(exc))
        return None, None, None
    delegated.add(seed)
    observe.emit("delegated", item=seed, lane=lane, tier=want, bound=bool(code))
    return seed, code, want


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


def _cap_children(wave, limits) -> None:
    """`--max-children` is the TOTAL this loop may spawn, not a per-tick cap.

    `up` applies it once at its single spawn; `until` spawns for the life of the wave and
    applied it nowhere, so a loop that spawned into nothing (see `_wanted_workers`) had no
    ceiling at all. A wave that reaches the cap with work still open ends `cap`, exit 1 —
    the operator raised the number knowingly or the loop was spawning wrong, and both are
    theirs to look at.
    """
    if len(wave.spawned) >= limits.max_children:
        raise CapError(
            "cap",
            f"max_children {limits.max_children} reached: {len(wave.spawned)} spawned this wave",
        )


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
) -> tuple[Seat, bool]:
    """Pre-minted pool first (workers). S6: all seats are workers now.

    A seat from the POOL carries whatever scope it was minted with, which for a `--seats` file
    is none. That is not silently accepted: `run` refuses `--prd` together with pre-minted
    seats, because a wave that reports as scoped while half its children are not is the failure
    mode this scope exists to remove.
    """
    if pool:
        return pool.pop(0), False
    code = _mint(planner, agent_id, wave_name, mint_left=mint_left,
                 mint_deadline=mint_deadline, sleep=sleep, role=role, prd=prd)
    return Seat(code=code, server_url=server, api_key=api_key, role=role), True


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
) -> str:
    last: Exception | None = None
    tries = max(1, mint_left)
    for attempt in range(tries):
        if attempt and time.monotonic() >= mint_deadline:
            break
        try:
            payload = planner.call(
                "mint_enrolment", agent_id=agent_id, role=role, wave=wave_name,
                **_seat_scope(prd),
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
        if any((h or {}).get("phase") != "stale" for h in (agent.get("holdings") or [])):
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
