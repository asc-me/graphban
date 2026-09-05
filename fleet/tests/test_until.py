"""P30 D1 — `gbfleet until` is a planner loop, not a thicker supervisor."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gbfleet.cli import make_launch_factory
from gbfleet.client import ALLOWED_TOOLS, Graphban
from gbfleet.seat import Seat
from gbfleet.supervisor import Limits
from gbfleet.until import PLANNER_TOOLS, Report, run

from tests.test_supervisor import KEY, _factory, _seats

from conftest import telemetry_ack  # noqa: E402


def _mcp(payload: dict, id_: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
            },
        },
    )


def _error(code: str, message: str, id_: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": message}],
                "structuredContent": {"error": {"code": code, "message": message}},
            },
        },
    )


def _clients(
    workspace: Path,
    *,
    off_limits: list[str] | None = None,
    clusters: int = 0,
    workers: int = 0,
    review: list | None = None,
    waits: list | None = None,
    mint_code: str = "WORKER-UNTIL",
    mint_fails: str | None = None,
    search_fails: str | None = None,
    minted_roles: list | None = None,
    on_mint=None,
    cluster_items: list | None = None,
    delegations: list | None = None,
    delegate_fails: str | None = None,
    calls: list | None = None,
    bound_seats: bool = False,
    bound_refused: bool = False,
):
    """Planner + supervisor clients sharing one mock Graphban."""
    seen_agents = {"yes": False}

    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        tool = body["params"]["name"]
        args = body["params"].get("arguments") or {}
        rid = body["id"]
        if calls is not None:
            calls.append(tool)
        if tool == "get_item_details":
            return _mcp({"id": args.get("id"), "title": "seed", "brief": {
                "lane": {"value": "backend", "basis": ["backend/app/x.py"]},
                "tier": {"value": "cheap", "basis": "none"},
                "text": "Item seed",
            }}, rid)
        if tool == "delegate":
            if delegate_fails:
                return _error(delegate_fails, f"refused ({delegate_fails})", rid)
            if bound_refused and args.get("seat"):
                return _error("conflict", "areas are reserved by GRPH-A9", rid)
            if delegations is not None:
                delegations.append(dict(args))
            return _mcp({"delegation_id": f"dlg_{len(delegations or [])}", "state": "open",
                         "withdrew": None, "brief": {},
                         "enrolment_code": (f"WORKER-BOUND{len(delegations or [])}"
                                            if (bound_seats and args.get("seat")) else None)}, rid)
        if tool == "register_agent":
            return _mcp({
                "agent_id": "GRPH-P1",
                "active_role": "planner",
                "eligible_roles": ["planner"],
                "tools_off_limits": list(off_limits or []),
            }, rid)
        if tool == "collision_clusters":
            total = 0 if seen_agents["yes"] else clusters
            rows = [{"items": list(cluster_items[i]) if cluster_items and i < len(cluster_items) else []}
                    for i in range(total)]
            return _mcp({
                "clusters": rows,
                "total": total,
            }, rid)
        if tool == "propose_allocation":
            mapping = [{"agent_id": "GRPH-A1"}] if workers else []
            return _mcp({
                "workers": workers,
                "reviewers": 0,
                "mapping": mapping,
                "rationale": "fixture",
            }, rid)
        if tool == "mint_enrolment":
            if mint_fails:
                return _error(mint_fails, f"cannot mint ({mint_fails})", rid)
            role = args.get("role") or "worker"
            if minted_roles is not None:
                minted_roles.append(role)
            if on_mint is not None:
                on_mint(role)
            return _mcp({"enrolment_code": mint_code, "role": role, "seat_id": "s1"}, rid)
        if tool == "search_items":
            if search_fails:
                return _error(search_fails, f"search failed ({search_fails})", rid)
            if args.get("status") == "review":
                return _mcp({"results": list(review or [])}, rid)
            if args.get("status") == "blocked":
                tag = (args.get("tags") or [""])[0]
                rows = [w for w in (waits or []) if tag in (w.get("tags") or [])]
                return _mcp({"results": rows}, rid)
            return _mcp({"results": []}, rid)
        if tool == "retire_wave":
            return _mcp({"seats_revoked": 0, "agents": 0}, rid)
        trees = sorted(p for p in workspace.glob("*") if p.is_dir() and p.name != "logs")
        if trees:
            seen_agents["yes"] = True
        return _mcp({
            "agents": [
                {
                    "id": f"GRPH-A{i + 1}",
                    "worktree": str(p),
                    "state": "idle",
                    "enrolled": True,
                    "enrolment_id": f"seat-{i + 1}",
                    "holdings": [],
                }
                for i, p in enumerate(trees)
            ]
        }, rid)

    transport = httpx.MockTransport(handler)
    planner = Graphban("http://gb.invalid", KEY, allowed=PLANNER_TOOLS, transport=transport)
    supervisor = Graphban("http://gb.invalid", KEY, allowed=ALLOWED_TOOLS, transport=transport)
    return planner, supervisor


def test_allowed_tools_stays_two_and_planner_holds_mint():
    assert ALLOWED_TOOLS == frozenset({"fleet_status", "propose_allocation"})
    assert "mint_enrolment" in PLANNER_TOOLS
    assert "mint_enrolment" not in ALLOWED_TOOLS
    assert "collision_clusters" in PLANNER_TOOLS
    assert PLANNER_TOOLS & ALLOWED_TOOLS == ALLOWED_TOOLS


def test_a_key_that_cannot_mint_is_refused_at_start(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, off_limits=["mint_enrolment"])
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.reason == "config"
    assert result.exit == 2
    assert result.ok is False
    assert "mint_enrolment" in result.detail


def test_idle_when_there_is_no_work_no_review_and_no_lease(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """THE CALL. Idle is not 'the last child exited' — three empty ticks with nothing."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=0)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
    )
    assert result.reason == "idle"
    assert result.exit == 0
    assert result.ok is True
    assert result.spawned == 0
    assert result.as_json()["reason"] == "idle"


def test_only_typed_waits_is_idle_with_waits(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace,
        waits=[{"id": "GRPH-W1", "tags": ["wait:merge"], "status": "blocked"}],
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
    )
    assert result.reason == "idle-with-waits"
    assert result.exit == 0
    assert result.ok is True
    assert result.waits == ["GRPH-W1"]


def test_leftover_review_is_not_idle(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """A wave that exits 0 with unsigned review is a failed run."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace, review=[{"id": "GRPH-9", "status": "review"}],
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
    )
    assert result.reason == "review-unsigned"
    assert result.exit == 1
    assert result.ok is False
    assert result.review == ["GRPH-9"]
    assert result.wave is not None and result.wave.ok is False
    # D2: spawn-when-needed, then three failed claim attempts, then this reason.


def test_until_spawns_a_worker_from_a_cold_cluster(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Cold start reads collision_clusters, mints just in time, then goes idle."""
    workspace = tmp_path / "ws"
    # First cluster read is 1; after a child exists the roster is live and
    # propose_allocation.workers is 0, so we do not mint a second.
    planner, supervisor = _clients(workspace, clusters=1, workers=0)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert result.spawned == 1, result.detail
    assert result.minted == 1
    assert result.reason == "idle"
    assert result.exit == 0


def test_pre_minted_seats_are_consumed_before_minting(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, mint_code="MUST-NOT-MINT")
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        seats=_seats(1, "http://gb.invalid"),
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert result.spawned == 1
    assert result.minted == 0
    assert result.reason == "idle"


def test_a_quota_mint_is_config_not_idle(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, mint_fails="quota")
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert result.reason == "config"
    assert result.exit == 2
    assert "quota" in result.detail


def test_main_until_is_the_call(monkeypatch, capsys, git_repo: Path):
    """P30 D1 bounce. parse_args is not the CALL. Returning 0 from the until
    branch and giving both clients ALLOWED_TOOLS left the helper tests green.
    """
    import inspect
    from gbfleet import cli
    from gbfleet.until import Report

    src = inspect.getsource(cli._until)
    assert "allowed=PLANNER_TOOLS" in src
    assert "allowed=ALLOWED_TOOLS" in src

    seen: dict = {}

    class FakeGB:
        def __init__(self, base_url, api_key, allowed=None, **_kw):
            self.allowed = allowed if allowed is not None else ALLOWED_TOOLS
            self.base_url = base_url
            self.api_key = api_key

        def close(self):
            pass

    def fake_run(repo, factory, planner, supervisor, **kw):
        seen["called"] = True
        seen["planner"] = planner.allowed
        seen["supervisor"] = supervisor.allowed
        return Report(ok=True, reason="idle", exit=0, waits=["GRPH-W1"])

    monkeypatch.setenv("GBFLEET_API_KEY", KEY)
    monkeypatch.setattr(cli, "Graphban", FakeGB)
    monkeypatch.setattr(cli, "run_until", fake_run)
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: object())

    code = cli.main([
        "until", "--repo", str(git_repo),
        "--server", "http://gb.invalid", "--adapter", "gbagent",
    ])
    assert seen.get("called"), "main(['until']) never called run_until"
    assert seen["planner"] == PLANNER_TOOLS
    assert "mint_enrolment" in seen["planner"]
    assert seen["supervisor"] == ALLOWED_TOOLS
    assert "mint_enrolment" not in seen["supervisor"]
    assert code == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(last)
    assert payload["reason"] == "idle"
    assert payload["exit"] == 0
    assert payload["waits"] == ["GRPH-W1"]


def test_a_failed_search_is_not_idle(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Absence as clean: search_items failing used to look like no review."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, search_fails="error")
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
    )
    assert result.reason != "idle", result.as_json()
    assert result.reason != "idle-with-waits", result.as_json()
    assert result.exit != 0
    assert result.ok is False


def test_three_empty_ticks_are_required(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Sabotage: idle on the first empty tick. This must fail if EMPTY_TICKS is ignored."""
    workspace = tmp_path / "ws"
    sleeps: list[float] = []
    planner, supervisor = _clients(workspace, clusters=0)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0.01, sleep=sleeps.append, empty_ticks=3,
    )
    assert result.reason == "idle"
    assert len(sleeps) >= 2, f"idled after {len(sleeps)} sleeps; need two gaps for three ticks"


def test_until_mints_a_reviewer_for_unheld_review(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """P30 D2. Spawn-when-needed, not a t=0 reviewer cohort."""
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": ""}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, max_reviewers=1),
    )
    assert "reviewer" in roles, roles
    assert all(c.role == "reviewer" for c in (result.wave.spawned if result.wave else [])), (
        [c.role for c in result.wave.spawned] if result.wave else []
    )
    assert result.reason == "review-unsigned"
    # THE CALL. works_then_exits dies with empty holdings. Three of those, then
    # review-unsigned. REVIEWER_FAILS=1 still yielded this reason (GRPH-603).
    assert roles.count("reviewer") == 3, roles


def test_until_does_not_mint_a_reviewer_when_claim_review_is_held(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-R1"}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_reviewers=1),
    )
    assert "reviewer" not in roles, roles
    assert result.reason == "idle"
    assert result.spawned == 0


def test_workers_and_reviewers_are_not_a_simultaneous_cohort(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Worker first, then reviewer. Never both from the same tick."""
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace, clusters=1,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": ""}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, max_reviewers=1),
    )
    assert roles, "expected at least one mint"
    assert roles[0] == "worker", roles
    if len(roles) > 1:
        assert "reviewer" in roles


def test_a_worker_spawn_does_not_fall_through_to_a_reviewer_in_the_same_tick(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """THE CALL (GRPH-603). Dropping `continue` after the worker spawn left
    `test_workers_and_reviewers_are_not_a_simultaneous_cohort` green — it only
    checked roles[0]==worker, which is still true when both mint in one pass.

    Clear the review queue on the worker mint. With continue, the next tick
    re-reads and does not mint a reviewer. Without it, the same tick's `rows`
    still say unheld and a reviewer is minted beside the worker.
    """
    workspace = tmp_path / "ws"
    roles: list[str] = []
    review = [{"id": "GRPH-9", "status": "review", "claimed_by": ""}]

    def on_mint(role: str) -> None:
        if role == "worker":
            review.clear()

    planner, supervisor = _clients(
        workspace, clusters=1, review=review, minted_roles=roles, on_mint=on_mint,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, max_reviewers=1),
    )
    assert roles == ["worker"], roles
    assert result.reason == "idle"


def test_a_live_reviewer_child_blocks_a_second_mint(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """THE CALL (GRPH-603). until tests used works_then_exits, so live_reviewers
    was always [] by the next tick. Ignoring it and minting a second reviewer
    while the first is still running (and has not claimed) stayed green.

    works_then_waits stays live for a moment. Two reviewer mints with no sleep
    between them is the same-tick / ignored-live path.
    """
    workspace = tmp_path / "ws"
    events: list[str] = []

    def on_mint(role: str) -> None:
        if role == "reviewer":
            if events and events[-1] == "mint":
                raise AssertionError(
                    "second reviewer minted with no tick between — live_reviewers "
                    f"was not consulted: {events}"
                )
            events.append("mint")

    def sleep_fn(_dt: float) -> None:
        events.append("sleep")

    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": ""}],
        on_mint=on_mint,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_waits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=sleep_fn, empty_ticks=3,
        limits=Limits(max_reviewers=1),
    )
    assert "mint" in events, events
    for a, b in zip(events, events[1:]):
        assert not (a == "mint" and b == "mint"), events
    assert result.reason == "review-unsigned"


def test_cli_until_advertises_max_reviewers():
    from gbfleet.cli import build_parser
    args = build_parser().parse_args(
        ["until", "--server", "http://x", "--adapter", "gbagent"]
    )
    assert args.max_reviewers == 1


def test_reviewer_instruction_does_not_teach_claim_cluster():
    from gbfleet.seat import Seat, instruction_for
    text = instruction_for(
        Seat(code="R-1", server_url="https://x", api_key="k", role="reviewer"),
        Path("/wt"), "gb/w-1",
    )
    assert "Call claim_review" in text
    assert "sign_off" in text
    assert "Then claim work with claim_cluster" not in text


# ---- PRD-35 D12 / criterion 22: the delegation is written before the seat ---------------------

def test_until_delegates_the_seed_before_minting_the_seat(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """One worker seat, one free cluster with a seed: `delegate` lands with the brief's lane
    and tier, BEFORE `mint_enrolment`. The child registers on that seat, which is the
    lineage the server links on."""
    workspace = tmp_path / "ws"
    delegations: list = []
    calls: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7", "GRPH-8"]],
        delegations=delegations, calls=calls,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.spawned == 1, result.detail
    assert delegations == [{
        "id": "GRPH-7", "lane": "backend", "tier": "cheap", "agent_id": "GRPH-P1",
        "note": "gbfleet until, wave wave", "seat": True, "wave": "wave",
    }]
    # The stub returned no enrolment_code, so the seat was minted the old way — after the
    # delegation, as PRD-35 D12 required.
    assert calls.index("delegate") < calls.index("mint_enrolment")


def test_a_seat_with_no_cluster_item_makes_no_delegate_call(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Criterion 22, second half. The absence is the record: nothing was handed over."""
    workspace = tmp_path / "ws"
    delegations: list = []
    calls: list = []
    planner, supervisor = _clients(workspace, clusters=1, delegations=delegations, calls=calls)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.spawned == 1, result.detail
    assert delegations == []
    assert "delegate" not in calls


def test_the_tier_flag_is_what_the_loop_requests(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """D5: the harness commits. `--tier frontier` overrides a brief that suggests cheap."""
    workspace = tmp_path / "ws"
    delegations: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], delegations=delegations,
    )
    run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        request="frontier",
    )
    assert [d["tier"] for d in delegations] == ["frontier"]


def test_a_refused_delegation_does_not_stop_the_spawn(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Another planner's open delegation, or a bounce pin, is theirs to hold. The seat is
    still minted; the divvy decides what the child claims and the record says so."""
    workspace = tmp_path / "ws"
    delegations: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], delegations=delegations,
        delegate_fails="conflict",
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.spawned == 1, result.detail
    assert delegations == []


# ---- PRD-36 D9 / criteria 12, 13: the loop mints BOUND seats ----------------------------------

def _capturing_factory(scripts, which: str, captured: list):
    """The fake launch, plus the instruction text as the child would read it. Read here, at
    launch, because the worktree and its instruction file are reaped with the wave."""
    inner = _factory(scripts, which)

    def factory(seat, tree, instruction_file, debug_file=None):
        captured.append(Path(instruction_file).read_text(encoding="utf-8"))
        return inner(seat, tree, instruction_file, debug_file)
    return factory


def test_until_mints_a_bound_seat_through_delegate_and_skips_mint_enrolment(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Criterion 12. The delegation carries seat=true, the server answers with the seat's
    code, no mint_enrolment call is made, and the child's instruction names the item."""
    workspace = tmp_path / "ws"
    delegations: list = []
    calls: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7", "GRPH-8"]],
        delegations=delegations, calls=calls, bound_seats=True,
    )
    captured: list = []
    result = run(
        git_repo, _capturing_factory(scripts, "works_then_exits", captured),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.spawned == 1, result.detail
    assert delegations[0]["seat"] is True and delegations[0]["id"] == "GRPH-7"
    assert "mint_enrolment" not in calls, "a bound seat is minted by the delegation itself"
    instruction = captured[0]
    assert "BOUND to GRPH-7" in instruction and "WORKER-BOUND1" in instruction
    assert "claim_cluster" in instruction and "Do NOT call claim_cluster" in instruction


def test_a_refused_bound_seat_falls_back_to_an_unbound_delegation(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """Criterion 13 / D13: the areas were held, so the delegation stands without a seat and
    the seat is minted the old way — the divvy decides what the child claims."""
    workspace = tmp_path / "ws"
    delegations: list = []
    calls: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]],
        delegations=delegations, calls=calls, bound_seats=True, bound_refused=True,
    )
    captured: list = []
    result = run(
        git_repo, _capturing_factory(scripts, "works_then_exits", captured),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.spawned == 1, result.detail
    assert [d.get("seat") for d in delegations] == [None], "the retry carried no seat"
    assert "mint_enrolment" in calls
    assert "BOUND" not in captured[0]
