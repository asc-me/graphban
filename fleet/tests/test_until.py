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
    sticky_clusters: bool = False,
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
            total = clusters if sticky_clusters else (0 if seen_agents["yes"] else clusters)
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


def test_a_roster_of_idle_agents_and_no_ready_work_spawns_nothing(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """THE RUNAWAY (PRD-39 acceptance walk, 2026-09-07). After the first child exited the
    roster held one idle agent, `collision_clusters` said zero, and `propose_allocation`
    still said "1 worker" — it describes the roster, not the backlog. Trusting it spawned
    a child every ~14 s for 18 minutes: 63 registrations against a finished project.
    Ready work bounds the count; with none, the loop goes idle."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, workers=1)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=2, max_children=8),
    )
    assert result.spawned == 1, "the proposal's phantom worker was spawned again"
    assert result.reason == "idle"


def test_max_children_is_the_total_for_the_wave(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """`up` applied --max-children once at its single spawn; `until` applied it nowhere, so
    the runaway above had no ceiling. It is the wave's total now, and reaching it with work
    still open ends the wave `cap` rather than spawning into it."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=5, workers=5, sticky_clusters=True)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, max_children=2),
    )
    assert result.spawned == 2
    assert result.reason == "cap" and result.exit == 1
    assert "max_children 2" in result.detail


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


def test_until_mints_a_worker_for_unheld_review(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """S6 (PRD-39 D-i): unheld review rows spawn a merged worker, not a reviewer.
    After REVIEWER_FAILS (3) consecutive empty spawns, exit review-unsigned."""
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1", "review_claimed_by": ""}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    # S6: all mints are workers now. The merged worker tries review first.
    assert all(r == "worker" for r in roles), roles
    assert result.reason == "review-unsigned"
    # THE CALL. Finite number of spawns, then review-unsigned.
    assert len(roles) >= 3, f"expected at least 3 spawns, got {len(roles)}: {roles}"
    assert len(roles) <= 4, f"expected at most 4 spawns, got {len(roles)}: {roles}"


def test_until_does_not_spawn_for_held_review(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """S6: when review rows are already held, no extra spawn."""
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1", "review_claimed_by": "GRPH-R1"}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert roles == [], roles
    assert result.reason == "idle"
    assert result.spawned == 0


def test_review_unsigned_after_exactly_three_empty_spawns(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """S6 sabotage: a child that registers and exits without claiming, three times
    → review-unsigned after a finite number of spawns (REVIEWER_FAILS=3 bounds it)."""
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1", "review_claimed_by": ""}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert result.reason == "review-unsigned"
    # Finite and bounded by REVIEWER_FAILS (+/- 1 for timing)
    assert 3 <= len(roles) <= 4, f"expected 3-4 spawns, got {len(roles)}: {roles}"


def test_a_live_child_blocks_a_second_review_spawn(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """S6: while a child is live, no second spawn for review work. After the child
    exits, the loop may spawn another. The wave eventually exits review-unsigned."""
    workspace = tmp_path / "ws"
    events: list[str] = []

    def on_mint(role: str) -> None:
        events.append("mint")

    def sleep_fn(_dt: float) -> None:
        events.append("sleep")

    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1", "review_claimed_by": ""}],
        on_mint=on_mint,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_waits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=sleep_fn, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    # At least one mint happened, and the wave exited review-unsigned
    assert events.count("mint") >= 1, f"expected at least 1 mint, got {events.count('mint')}"
    assert result.reason == "review-unsigned"


def test_unified_instruction_teaches_both_claim_review_and_claim_cluster():
    """S6 (PRD-39 D-h): one instruction template for every worker — try claim_review,
    fall through to claim_cluster, exit when both are empty."""
    from gbfleet.seat import Seat, instruction_for
    text = instruction_for(
        Seat(code="W-1", server_url="https://x", api_key="k"),
        Path("/wt"), "gb/w-1",
    )
    assert "claim_review" in text
    assert "claim_cluster" in text
    assert "sign_off" in text
    assert "EXIT when both are empty" in text


def test_deleting_the_review_fails_counter_breaks_the_test(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """S6 sabotage: the re-keyed counter (REVIEWER_FAILS=3) is what stops infinite
    spawning against unheld review. If the counter were deleted or set to a very
    large value, the loop would spawn forever. This test pins the exact count."""
    from gbfleet.until import REVIEWER_FAILS
    assert REVIEWER_FAILS == 3


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


def test_a_stale_hold_is_not_a_holding():
    """PRD-39 acceptance walk, run 3: the builder of the last item exited with it in review,
    the roster still showed the item under its holdings with `phase: stale` (older than the
    presence TTL), and `until` waited on that dead process forever. A stale hold is what a
    dead agent leaves behind, not somebody working."""
    from gbfleet.until import _any_holdings

    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        return _mcp({"agents": [
            {"id": "GRPH-A68", "state": "offline",
             "holdings": [{"id": "GRPH-6", "phase": "stale"}]},
        ] + ([{"id": "GRPH-A70", "state": "working",
               "holdings": [{"id": "GRPH-7", "phase": "reported"}]}]
             if handler.live else [])}, body["id"])
    handler.live = False
    sup = Graphban("http://gb.invalid", KEY, allowed=ALLOWED_TOOLS,
                   transport=httpx.MockTransport(handler))
    assert _any_holdings(sup) is False, "a stale hold counted as somebody working"
    handler.live = True
    assert _any_holdings(sup) is True


def test_a_review_row_still_leased_to_its_builder_gets_a_reviewer(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """THE CALL for the walk's third run. `claimed_by` on a row in `review` is the BUILDER's
    lease and stays set; `until` read it as "a reviewer holds this" and so never spawned a
    reviewer for any real review row — reviews only ever happened because the runaway build
    path spawned workers whose loop reviewed. The reviewer's hold is `review_claimed_by`."""
    workspace = tmp_path / "ws"
    roles: list = []
    planner, supervisor = _clients(
        workspace, minted_roles=roles,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1"}],
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, max_children=4),
    )
    assert len(roles) >= 1, "no reviewer was spawned for a row nobody is reviewing"
    assert result.reason in ("review-unsigned", "idle"), result.reason

