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
from .supervisor import (
    _declared_into,
    DEFAULT_MAX_WORKERS, AllocationRead, LaunchFactory, Limits, Wave, _reap_all, _rooted,
    _start, item_status, watch_tick,
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
        with hold(repo, state) as acquired:
            wave.lock = acquired
            leftover: list[Child] = []
            occupied: set[str] = set()
            if acquired.takeover:
                leftover, occupied, notes = adopt_mod.recover(repo, workspace, state)
                for note in notes:
                    observe.emit("adopt", detail=note)
                wave.spawned.extend(leftover)

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
    shared: dict | None = None,
    tiers: TierTable | None = None,
    launch_for: Callable[..., LaunchFactory] | None = None,
    matrix: "matrix_mod.Matrix | None" = None,
    adapter: str = "",
) -> Report:
    from .mcp import read_preferences
    profile, policy, pref_note, measured = read_preferences(supervisor)
    observe.emit("preferences", detail=pref_note)
    agent_id = str(identity.get("agent_id") or identity.get("id"))
    empty = 0
    delegated: set[str] = set()
    tiers = tiers or TierTable()
    minted = minted_start
    mint_deadline = time.monotonic() + mint_budget
    mint_left = mint_tries
    review_fails = 0

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
                    role="worker",
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
                res = matrix.resolve(tier=want, profile=profile, policy=policy,
                                     measured=measured, installed=matrix_mod.installed_checker())
                if res.winner is not None:
                    factory = launch_for(res.winner.harness, res.winner.model)
                    chosen = (res.winner.harness, res.winner.model)
                    observe.emit("resolved", item=seed, **{k: v for k, v in res.explain().items() if k in ("winner", "dropped", "eligible", "profile")})
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
        if unheld_review and need <= 0 and not live:
            empty = 0
            if review_fails >= REVIEWER_FAILS:
                wave.reason = "review-unsigned"
                return _finish(wave, "review-unsigned", 1, minted, planner,
                               review=reviews, waits=waits)
            seat, minted_one = _take_seat(
                pool, planner, agent_id, wave_name, server, api_key,
                mint_left=mint_left, mint_deadline=mint_deadline, sleep=sleep,
                role="worker",
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


def _waiting(blocked: list[dict]) -> str:
    """What a person needs to decide whether to wait: who holds it, and for how long."""
    holders = sorted({h for c in blocked for h in (c.get("held_by") or [])})
    waits = [c.get("free_in") for c in blocked if isinstance(c.get("free_in"), int)]
    soonest = min(waits) if waits else None
    return (f"{len(blocked)} cluster(s) held by {', '.join(holders) or 'another agent'}"
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


def _scope(prd: str | None) -> dict:
    """The wave's work filter, as `collision_clusters` arguments (GRPH-797).

    One function rather than an inline dict at each call site, because the two sites decide
    DIFFERENT things — what to delegate, and how many workers are wanted — and a filter applied
    to one and not the other would size the fleet for work it then refuses to hand out.

    Empty when unscoped, so an unfiltered run sends exactly what it sent before.
    """
    return {"prd_id": prd} if prd else {}


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
                             note=note, seat=True, wave=wave_name)
        got = reply.get("enrolment_code") if isinstance(reply, dict) else None
        code = str(got) if got else None
    except ToolFailed as exc:
        observe.emit("bound_seat_refused", item=seed, detail=str(exc))
        try:
            planner.call("delegate", id=seed, lane=lane, tier=want, agent_id=agent_id, note=note)
        except (ToolFailed, NotPermitted, ServerUnreachable) as exc2:
            observe.emit("delegate_refused", item=seed, detail=str(exc2))
            return None, None, None
    except (NotPermitted, ServerUnreachable) as exc:
        observe.emit("delegate_refused", item=seed, detail=str(exc))
        return None, None, None
    delegated.add(seed)
    observe.emit("delegated", item=seed, lane=lane, tier=want, bound=bool(code))
    return seed, code, want


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
            # Reported, not spawned into. The wait is the server's own number, and "wait" and
            # "give up" are different instructions.
            observe.emit("contention", detail=_waiting(blocked))
        return 0
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
) -> tuple[Seat, bool]:
    """Pre-minted pool first (workers). S6: all seats are workers now."""
    if pool:
        return pool.pop(0), False
    code = _mint(planner, agent_id, wave_name, mint_left=mint_left,
                 mint_deadline=mint_deadline, sleep=sleep, role=role)
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
) -> str:
    last: Exception | None = None
    tries = max(1, mint_left)
    for attempt in range(tries):
        if attempt and time.monotonic() >= mint_deadline:
            break
        try:
            payload = planner.call(
                "mint_enrolment", agent_id=agent_id, role=role, wave=wave_name,
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
