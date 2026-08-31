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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import adopt as adopt_mod
from . import waits as wait_mod
from .client import ALLOWED_TOOLS, Graphban, NotPermitted, ServerUnreachable, ToolFailed
from .lock import hold
from .seat import Seat
from .spawn import Child
from .supervisor import (
    DEFAULT_MAX_WORKERS, AllocationRead, LaunchFactory, Limits, Wave, _reap_all, _start,
    item_status, watch_tick,
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
})

EMPTY_TICKS = 3
MINT_TRIES = 3
MINT_BUDGET_S = 30.0
#: Consecutive reviewer register/claim failures on a still-non-empty review queue
#: before until stops minting reviewers (P30 D2).
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
) -> Report:
    """Hold the repo lock and run until idle, a cap, or a config refusal."""
    if planner.allowed & {"mint_enrolment"} and "mint_enrolment" in ALLOWED_TOOLS:
        raise ConfigError("ALLOWED_TOOLS must not include mint_enrolment")
    if planner.allowed == ALLOWED_TOOLS:
        raise ConfigError("until needs a planner client, not the supervisor allowlist")

    repo = Path(repo)
    workspace = Path(workspace) if workspace else repo.parent / f"{repo.name}-gbfleet"
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
) -> Report:
    agent_id = str(identity.get("agent_id") or identity.get("id"))
    empty = 0
    minted = minted_start
    mint_deadline = time.monotonic() + mint_budget
    mint_left = mint_tries
    reviewer_fails = 0

    while True:
        watch_tick(wave, children, limits, supervisor, debug=debug, persist=persist)
        finished = [c for c in children if not c.running]
        if finished:
            for child in finished:
                if child.role == "reviewer":
                    if child.held_items:
                        reviewer_fails = 0
                    else:
                        reviewer_fails += 1
            _reap_all(wave, finished)
            children[:] = [c for c in children if c.running]
            persist()
            if any("handoff-failed" in f for f in wave.failures):
                wave.reason = "handoff-failed"
                return _finish(wave, "handoff-failed", 1, minted, planner)

        live = [c for c in children if c.running]
        live_workers = [c for c in live if c.role != "reviewer"]
        live_reviewers = [c for c in live if c.role == "reviewer"]
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
            need = _wanted_workers(planner, supervisor, live_n=len(live_workers),
                                   max_workers=limits.max_workers)
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
                need = _wanted_workers(planner, supervisor, live_n=len(live_workers),
                                       max_workers=limits.max_workers)
            except ServerUnreachable:
                sleep(poll)
                continue
            if need <= 0:
                sleep(poll)
                continue
            seat, minted_one = _take_seat(
                pool, planner, agent_id, wave_name, server, api_key,
                mint_left=mint_left, mint_deadline=mint_deadline, sleep=sleep,
                role="worker",
            )
            if minted_one:
                minted += 1
                mint_left -= 1
            _spawn_one(
                wave, children, occupied, persist, seat, launch_factory,
                repo, workspace, wave_name, supervisor, limits, planner, debug,
            )
            continue

        if _need_reviewer(rows, live_reviewers, limits.max_reviewers):
            empty = 0
            if reviewer_fails >= REVIEWER_FAILS:
                wave.reason = "review-unsigned"
                return _finish(wave, "review-unsigned", 1, minted, planner,
                               review=reviews, waits=waits)
            seat, minted_one = _take_seat(
                pool, planner, agent_id, wave_name, server, api_key,
                mint_left=mint_left, mint_deadline=mint_deadline, sleep=sleep,
                role="reviewer",
            )
            if minted_one:
                minted += 1
                mint_left -= 1
            before = len(wave.spawned)
            _spawn_one(
                wave, children, occupied, persist, seat, launch_factory,
                repo, workspace, wave_name, supervisor, limits, planner, debug,
            )
            if len(wave.spawned) == before:
                reviewer_fails += 1
            continue

        if live or holdings:
            empty = 0
            sleep(poll)
            continue

        unheld = [r for r in rows if not r.get("claimed_by")]
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
        limits, debug=debug, occupied=occupied, items=item_status(planner),
        into=children, persist=persist,
    )
    occupied.update(c.branch for c in children)
    persist()
    if len(wave.spawned) == before and wave.failures:
        raise ConfigError(wave.failures[-1])


def _need_reviewer(
    rows: list[dict], live_reviewers: list[Child], max_reviewers: int,
) -> bool:
    """Spawn-when-needed. Unheld review, no in-flight reviewer, under the cap."""
    if max_reviewers <= 0:
        return False
    unheld = [r for r in rows if not r.get("claimed_by")]
    if not unheld:
        return False
    if live_reviewers:
        return False
    return True


def _wanted_workers(
    planner: Graphban, supervisor: Graphban, *, live_n: int, max_workers: int,
) -> int:
    """How many more workers to start. Cold start reads clusters; a live roster reads the mix."""
    roster = supervisor.call("fleet_status")
    agents = [a for a in (roster.get("agents") or []) if a.get("id")]
    if not agents:
        clusters = planner.call("collision_clusters")
        total = int(clusters.get("total") or 0)
        return max(0, min(max_workers, total) - live_n)
    alloc = AllocationRead.of(planner.call("propose_allocation"))
    if alloc.uninformative:
        clusters = planner.call("collision_clusters")
        total = int(clusters.get("total") or 0)
        return max(0, min(max_workers, total) - live_n)
    return max(0, min(max_workers, alloc.workers) - live_n)


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
    """Pre-minted pool first (workers). Reviewers always mint role=reviewer."""
    if role == "worker" and pool:
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
    try:
        roster = supervisor.call("fleet_status")
    except ServerUnreachable:
        return False
    for agent in roster.get("agents") or []:
        if agent.get("holdings"):
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
