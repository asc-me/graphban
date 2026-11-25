"""P30 D1 — `gbfleet until` is a planner loop, not a thicker supervisor."""
from __future__ import annotations

import time
import json
from pathlib import Path

import httpx
import pytest

from gbfleet import until
from gbfleet.cli import make_launch_factory
from gbfleet.client import ALLOWED_TOOLS, Graphban
from gbfleet.seat import Seat
from gbfleet.supervisor import Limits
from gbfleet.worktree import porcelain_lists_worktree, worktree_key
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
    minted_args: list | None = None,
    mint_review_only: bool = True,
    cluster_items: list | None = None,
    delegations: list | None = None,
    delegate_fails: str | None = None,
    calls: list | None = None,
    bound_seats: bool = False,
    bound_refused: bool | str = False,
    pinned_ticks: int = 0,
    pin_message: str | None = None,
    sticky_clusters: bool = False,
    gitops: dict | None = None,
    drop_delegated: bool = False,
    prds: dict | None = None,
    staff_every_row: bool | None = None,
    observations_status: int = 200,
    observation_posts: list | None = None,
    agent_holdings: list | None = None,
    brief_tier: dict | None = None,
    maintenance_line: dict | None = None,
    backlog: list | None = None,
    item_rows: list | None = None,
    updates: list | None = None,
    item_details: dict | None = None,
    roster_assigned: dict | None = None,
    roster_timeout: str | None = None,
    roster_status: int | None = None,
    clusters_timeout: str | None = None,
):
    """Planner + supervisor clients sharing one mock Graphban.

    `prds` maps each spelling `get_prd` accepts to the PRD's canonical key (GRPH-1190); a
    spelling it does not name is refused as the server refuses it. None answers every
    spelling with itself."""
    # GRPH-885: `_delegate_next` only seeds a cluster that names an item. Fixtures that
    # used `clusters=1` with an empty `items` list used to spawn an unbound drain child;
    # that is the skip-ahead path this item closes. Default a seed so spawn-path tests
    # still exercise the loop; pass `cluster_items=[[]]` for a deliberately empty cluster.
    if cluster_items is None and clusters:
        cluster_items = [[f"GRPH-{i+1}"] for i in range(clusters)]
    seen_agents = {"yes": False}
    # GRPH-1189: what the real server does — an item a child holds leaves `collision_clusters`.
    taken: set[str] = set()
    pinned_left = [pinned_ticks]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/fleet/observations":
            body = json.loads(request.content)
            # GRPH-1346. The supervisor's receipts and the start-up probe land here.
            if observation_posts is not None:
                observation_posts.append(body)
            # What the real route does, in its order: the body is validated BEFORE the
            # credential is checked (`ObservationIn.item_id` and `kind` are required, so a
            # body without them is 422 for every key), then the credential gate (403), then
            # the item gate (404 "no such item" for an item no key resolves). A mock that
            # answered 403 whatever the body let a 422-producing probe pass (the bounce).
            if not isinstance(body.get("item_id"), str) or not isinstance(body.get("kind"), str):
                return httpx.Response(422, json={"detail": [
                    {"loc": ["body", "item_id"], "msg": "Field required"}]})
            if observations_status >= 400:
                return httpx.Response(observations_status, text=(
                    "this credential is not a supervisor key: recording a cut needs planner "
                    "among its roles, and either a fleet wave or a project-scoped key"))
            if body["kind"] == "seats":
                # GRPH-1648: the start-up seat check, as the route answers the minter's key.
                return httpx.Response(200, json={"kind": "seats", "ok": True, "checked": [
                    {"index": i, "seat": "", "ok": True}
                    for i, _ in enumerate(body.get("seats") or [])]})
            if body["item_id"] == "__gbfleet_probe_no_such_item__":
                return httpx.Response(404, json={"detail": "no such item"})
            return httpx.Response(200, json={"recorded": True})
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            # GRPH-988: a wave that builds nothing probes the manifest before it spawns, and
            # `params` carries no `name` — without this branch the line below KeyErrors. The
            # manifest is not trimmed by role (PRD-17 D-b), so `mint_enrolment` is in it for
            # every caller, exactly as the real server serves it.
            #
            # NOT `_mcp`, and that is not a shortcut: `_mcp` wraps its payload in the tool-call
            # envelope (`content` / `structuredContent`), while `list_tools` reads `tools` off
            # the RESULT itself. Answering through `_mcp` produces a manifest that reads as
            # empty, so the probe falls through to "unverified" and every assertion below it
            # passes without the probe having run at all.
            props = {"agent_id": {"type": "string"}, "role": {"type": "string"}}
            if mint_review_only:
                props["review_only"] = {"type": "boolean"}
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"tools": [{"name": "mint_enrolment",
                                      "inputSchema": {"properties": props}}]},
            })
        tool = body["params"]["name"]
        args = body["params"].get("arguments") or {}
        rid = body["id"]
        if calls is not None:
            calls.append(tool)
        if tool == "get_context":
            # GRPH-1012: the ref this wave cuts its children from. Empty by default, which
            # is what the real server answers for a project that has measured no git
            # process — so every other test here exercises the unmeasured path whether it
            # knows it or not.
            return _mcp({"project_id": "core", "gitops": dict(gitops or {}),
                         "maintenance_line": maintenance_line}, rid)
        if tool == "get_rule_toggles":
            # GRPH-1273. None: a server that has never heard of the rule's library.
            toggles = [] if staff_every_row is None else [{
                "rule_id": "review.staff_every_row",
                "on": {"value": staff_every_row, "source": "project"}}]
            return _mcp({"library_loaded": True, "toggles": toggles}, rid)
        if tool == "get_prd":
            asked = str(args.get("prd_id"))
            if prds is None:
                return _mcp({"id": asked, "project_id": "core"}, rid)
            if asked not in prds:
                return _error("not_found", f"prd not found: {asked}", rid)
            return _mcp({"id": prds[asked], "project_id": "core"}, rid)
        if tool == "get_item_details":
            item_id = args.get("id")
            payload = {"id": item_id, "title": "seed", "status": "next", "brief": {
                "lane": {"value": "backend", "basis": ["backend/app/x.py"]},
                "tier": brief_tier or {"value": "cheap", "basis": "none"},
                "text": "Item seed",
            }}
            # A caller that names this id's row wins field-by-field. Tests that
            # never pass `item_details` keep the seed above.
            payload.update((item_details or {}).get(item_id) or {})
            return _mcp(payload, rid)
        if tool == "delegate":
            taken.add(str(args.get("id")))
            if delegate_fails:
                return _error(delegate_fails, f"refused ({delegate_fails})", rid)
            if bound_refused and args.get("seat"):
                return _error("conflict", bound_refused if isinstance(bound_refused, str)
                              else "areas are reserved by GRPH-A9", rid)
            if pinned_left[0] > 0 and args.get("seat"):
                pinned_left[0] -= 1
                return _error("conflict", pin_message or (
                    "GRPH-7 is pinned to its author GRPH-A1 after a bounce "
                    "(pinned_to=GRPH-A1; pinned_until=2026-11-05T13:54:16+00:00; "
                    "reserved_until=2026-11-05T13:54:16+00:00)"), rid)
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
        if tool == "collision_clusters" and clusters_timeout is not None:
            raise httpx.ReadTimeout(clusters_timeout)
        if tool == "collision_clusters":
            total = clusters if sticky_clusters else (0 if seen_agents["yes"] else clusters)
            rows = [{"items": list(cluster_items[i]) if cluster_items and i < len(cluster_items) else []}
                    for i in range(total)]
            if drop_delegated:
                rows = [r for r in rows if not taken.intersection(r["items"])]
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
            if minted_args is not None:
                minted_args.append(dict(args))
            if on_mint is not None:
                on_mint(role)
            return _mcp({"enrolment_code": mint_code, "role": role, "seat_id": "s1",
                         "review_only": bool(args.get("review_only"))}, rid)
        if tool == "search_items":
            if search_fails:
                return _error(search_fails, f"search failed ({search_fails})", rid)
            if args.get("status") == "review":
                return _mcp({"results": list(review or [])}, rid)
            if args.get("status") == "blocked":
                tag = (args.get("tags") or [""])[0]
                rows = [w for w in (waits or []) if tag in (w.get("tags") or [])]
                return _mcp({"results": rows}, rid)
            if args.get("status") in ("backlog", "next"):
                return _mcp({"results": [r for r in (backlog or [])
                                         if r.get("status") == args.get("status")]}, rid)
            # `item_status` asks with no status and no query. A hold search also has
            # no status, and answering it with these rows would adopt a hold the
            # fixture never wrote.
            if item_rows is not None and not args.get("status") and not args.get("query"):
                return _mcp({"results": list(item_rows)}, rid)
            return _mcp({"results": []}, rid)
        if tool == "update_item" and updates is not None:
            updates.append(dict(args))
            return _mcp({"id": args.get("id"), "status": args.get("status") or "next"}, rid)
        if tool == "retire_wave":
            return _mcp({"seats_revoked": 0, "agents": 0}, rid)
        if tool == "fleet_status" and roster_timeout is not None:
            raise httpx.ReadTimeout(roster_timeout)
        if tool == "fleet_status" and roster_status is not None:
            return httpx.Response(roster_status, text="unavailable")
        trees = sorted(p for p in workspace.glob("*") if p.is_dir() and p.name != "logs")
        if trees:
            seen_agents["yes"] = True
        agents = []
        for i, p in enumerate(trees):
            row = {
                "id": f"GRPH-A{i + 1}",
                "worktree": str(p),
                "state": "idle",
                "enrolled": True,
                "enrolment_id": f"seat-{i + 1}",
                # `shim` is a PATH directory this process creates beside the
                # worktrees (GRPH-1401). It is not an agent that claimed
                # anything. Copying `agent_holdings` onto it left a build
                # lease on the roster after the child was gone, and the
                # idle check waited on that directory forever.
                "holdings": ([] if p.name == "shim" else list(agent_holdings or [])),
            }
            # Only the child. The shim is not a seat, and a missing `assigned`
            # is what every fixture from before GRPH-1292 still sends.
            if roster_assigned is not None and p.name != "shim":
                row["assigned"] = dict(roster_assigned)
            agents.append(row)
        return _mcp({"agents": agents}, rid)

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
    # GRPH-1012: the base read is the planner's, and NOT the supervisor's own. Dropping it
    # here is the quiet failure — `resolve_wave_base` treats a refused call as an unreadable
    # gitops, so every project's measured trunk would read as unmeasured and every wave would
    # fall through to the remote default with a note that looks like a clean answer.
    assert "get_context" in PLANNER_TOOLS
    assert "get_context" not in ALLOWED_TOOLS
    # GRPH-1472: the planner returns a red review to its author; the supervisor cannot.
    assert "bounce" in PLANNER_TOOLS
    assert "bounce" not in ALLOWED_TOOLS


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


def test_a_key_the_observations_route_refuses_is_refused_before_any_seat(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1346. Sabotage: remove `_preflight_observations` and this mints a seat."""
    workspace = tmp_path / "ws"
    calls: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, workers=1, observations_status=403, calls=calls)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.reason == "config" and result.exit == 2 and result.ok is False
    assert "planner" in result.detail and "project-scoped" in result.detail
    assert "mint_enrolment" not in calls and "delegate" not in calls
    assert result.spawned == 0


def test_the_preflight_probe_is_a_body_the_route_validates_and_a_supervising_key_passes(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1346 bounce. The probe names a real kind and an item no key resolves, so a key
    that can supervise reaches the item gate (404) and the wave starts. Sabotage: post
    `{"kind": "probe"}` again and the mock answers 422, as the real route does."""
    from gbfleet.client import OBSERVATION_PROBE

    workspace = tmp_path / "ws"
    posts: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, workers=1, observation_posts=posts)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert posts and posts[0] == OBSERVATION_PROBE
    assert result.reason != "config", result.detail
    assert result.spawned >= 1


def test_a_probe_the_route_cannot_validate_refuses_rather_than_reads_as_passed(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """GRPH-1346 bounce: a 422 is the check not running, and must not read as "can
    supervise" — that is how the first attempt let a refused key start a wave."""
    monkeypatch.setattr(until, "OBSERVATION_PROBE", {"kind": "probe"})
    workspace = tmp_path / "ws"
    calls: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, workers=1, observations_status=403, calls=calls)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.reason == "config" and result.ok is False
    assert "HTTP 422" in result.detail
    assert "mint_enrolment" not in calls and result.spawned == 0


def test_a_refused_observation_mid_run_is_counted_and_not_ok(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1346. Sabotage: drop the increment in `_observe` and the count stays 0."""
    from gbfleet.supervisor import Wave, _observe

    wave = Wave()

    class Refusing:
        def post_observation(self, **_):
            raise until.ObservationRefused("HTTP 403", status=403)

    _observe(wave, Refusing(), "GRPH-1", "base not recorded", kind="cut_from")
    _observe(wave, Refusing(), "GRPH-1", "base not recorded", kind="cut_from")
    assert wave.observations_refused == 2
    report = Report(ok=True, reason="idle", exit=0, wave=wave)
    out = report.as_json()
    assert out["ok"] is False
    assert out["supervision_unrecorded"] == 2
    clean = Report(ok=True, reason="idle", exit=0, wave=Wave()).as_json()
    assert clean["ok"] is True and clean["supervision_unrecorded"] == 0


def test_a_scoped_wave_refuses_pre_minted_seats(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-827. A `--seats` file carries seats minted elsewhere, and those carry no scope. A
    wave that accepted both flags would report as scoped while the children holding those
    seats could claim the whole project — the finding this scope exists to close, walked back
    in through a flag combination.

    Refused as a config error, which is before the lock and before any worktree: nothing is
    half-built and no credential has been spent."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        seats=[Seat(code="WORKER-AAAAAA", server_url="http://gb.invalid", api_key=KEY)],
        prd="SA-P11",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.reason == "config"
    assert result.exit == 2
    assert "--prd SA-P11" in result.detail and "pre-minted seats" in result.detail
    assert "Drop --seats" in result.detail, "refused without saying how to proceed"


def test_a_wave_reports_what_its_children_spent(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-834. End to end, through the vendor's own result record rather than a patched
    reader: the child prints the line `gbagent` prints, the supervisor reads it where it
    already reads it for the ledger, and the summary carries it.

    Sabotage: drop the `wave.spend[...]` write in `_report_exits` — every attempt record still
    posts, the ledger still gets the numbers, and the summary reports zero."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, workers=0)
    result = run(
        git_repo, _factory(scripts, "reports_its_tokens", adapter="gbagent"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="gbagent",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    spend = result.as_json()["spend"]

    assert spend["tokens_in"] == 700 and spend["tokens_out"] == 400
    assert spend["reported"] == 1
    assert spend["by_child"][0]["turns_used"] == 3


def test_a_wave_ends_when_the_budget_is_reached(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """THE ONE THAT MATTERS: the loop actually stops. A budget that only appeared in the
    summary would be a report, not a control.

    The wave FINISHES rather than aborting — a running child is left to its own end, because
    killing it spends the tokens and throws away the work."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, workers=0)
    result = run(
        git_repo, _factory(scripts, "reports_its_tokens", adapter="gbagent"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="gbagent",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=5,
        limits=Limits(max_workers=1), budget=500,
    )

    assert result.reason == "budget"
    assert result.exit == 1
    assert "1100 measured" in result.detail
    assert result.as_json()["spend"]["tokens"] == 1100


def test_a_budget_the_adapter_cannot_enforce_refuses_the_wave(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """THE ONE THAT MAKES THE FLAG HONEST, pinned through `run` rather than by calling the
    check directly — the unit test for `check_budget_can_be_enforced` passes whether or not
    anything calls it, which is how a guard ends up defined and never wired.

    Sabotage: remove the call in `run` and the unit test stays green while `--budget 500
    --adapter claude` silently means nothing."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, workers=0)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="claude",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1), budget=500,
    )

    assert result.reason == "config"
    assert result.exit == 2
    assert "reports no token usage" in result.detail
    assert result.spawned == 0, "spent a child before refusing the flag"


def test_a_generous_budget_does_not_end_the_wave(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """The control. A cap that fires on every wave is a cap nobody sets."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, workers=0)
    result = run(
        git_repo, _factory(scripts, "reports_its_tokens", adapter="gbagent"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="gbagent",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1), budget=10_000_000,
    )

    assert result.reason == "idle"


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
    # Empty means the refusal list was written, not skipped. A missing key would
    # read the same as this, which is the idle exit GRPH-1643 closes.
    assert result.as_json()["refused"] == []


def test_the_merger_carries_the_planner_that_registered(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """A fleet credential cannot bounce without agent_id. The loop must hand the planner
    it just registered to the Merger, or a conflicted review stays `done`.

    Sabotage: build the Merger without `agent_id`. This fails.
    """
    from gbfleet.supervisor import Merger as Real

    seen: dict = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return Real(*args, **kwargs)

    monkeypatch.setattr("gbfleet.until.Merger", spy)
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=0)
    run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
    )
    assert seen.get("agent_id") == "GRPH-P1"


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


def _run_until_roster_dead(git_repo, tmp_path, scripts, state, **client_kw):
    """A free cluster, and `fleet_status` failing on every call. No child starts."""
    workspace = tmp_path / "ws"
    sleeps: list[float] = []

    def sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) > 3:
            raise AssertionError("until kept polling an unreadable roster")

    planner, supervisor = _clients(workspace, clusters=1, workers=0, **client_kw)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=sleep, empty_ticks=3,
        limits=Limits(max_workers=1, max_children=4),
    )
    return result, sleeps


def test_a_roster_timeout_in_wanted_workers_is_not_the_spawn_cap(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1813. `fleet_status` raises a timeout on every call while a cluster is
    free. Nothing is spawned, so the exit is not `cap`. It names the unread roster
    and carries the timeout text.

    Control: `test_max_children_is_the_total_for_the_wave` still exits `cap` once
    spawned equals the cap.

    Sabotage: put the `CapError("cap", "server unreachable with no live children")`
    fallthrough back for a `fleet_status` failure. This assertion on `reason` goes red.
    """
    result, sleeps = _run_until_roster_dead(
        git_repo, tmp_path, scripts, state, roster_timeout="timed out",
    )
    assert result.spawned == 0 and result.minted == 0
    assert result.reason == "roster_unreadable", result.as_json()
    assert result.reason != "cap"
    assert result.exit == 1 and result.ok is False
    assert "timed out" in result.detail
    assert sleeps == []


def test_a_roster_server_error_in_wanted_workers_is_not_the_spawn_cap(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1813. An HTTP 500 from `fleet_status` is the server failing to answer.
    Same exit as a timeout: the roster was unreadable, and the status is in the detail.

    Sabotage: the same fallthrough as the timeout test. This goes red with it.
    """
    result, _sleeps = _run_until_roster_dead(
        git_repo, tmp_path, scripts, state, roster_status=500,
    )
    assert result.spawned == 0
    assert result.reason == "roster_unreadable", result.as_json()
    assert result.reason != "cap"
    assert "500" in result.detail


def test_a_clusters_timeout_in_wanted_workers_is_not_the_spawn_cap(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1813. `_wanted_workers` calls `collision_clusters` before `fleet_status`.
    A timeout there, with nothing spawned, is not the spawn cap. The exit names the
    call and carries the timeout text.

    Control: `test_max_children_is_the_total_for_the_wave` still exits `cap` once
    spawned equals the cap.

    Sabotage: restore `CapError("cap", "server unreachable with no live children")`
    for a `ServerUnreachable` whose tool is not `fleet_status`. This assertion on
    `reason` goes red.
    """
    result, sleeps = _run_until_roster_dead(
        git_repo, tmp_path, scripts, state, clusters_timeout="timed out",
    )
    assert result.spawned == 0 and result.minted == 0
    assert result.reason == "server_unreachable", result.as_json()
    assert result.reason != "cap"
    assert result.exit == 1 and result.ok is False
    assert "collision_clusters" in result.detail
    assert "timed out" in result.detail
    assert sleeps == []


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
    # GRPH-1356: held review is still unsigned review; it polls, then fails, never idle.
    assert result.reason == "review-unsigned"
    assert result.ok is False
    assert result.review == ["GRPH-9"]
    assert result.spawned == 0


def test_held_review_with_a_waiting_item_is_not_idle_with_waits(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1356, the p68 incident shape: GRPH-9 sits in review with a hold that is not
    lapsed and no live child of ours, and another PRD item waits only on that review. The
    wait must not let the unsigned review read as `idle-with-waits` (exit 0).

    Sabotage: move the `if rows:` exit below the `if waits:` exit and this fails."""
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "GRPH-R1"}],
        waits=[{"id": "GRPH-W1", "tags": ["wait:merge"], "status": "blocked"}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert roles == [], roles
    assert result.reason == "review-unsigned", result.as_json()
    assert result.ok is False
    assert result.exit != 0
    assert result.review == ["GRPH-9"]
    assert result.waits == ["GRPH-W1"]


def test_until_spawns_for_a_review_row_whose_holder_has_lapsed(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1254. `review_claimed_by` stays set after its holder dies, so reading it alone
    kept the GRPH-P62 wave idle for over an hour behind a dead reviewer. The server says the
    hold has lapsed (`review_claim_lapse`), and that row is unheld.

    Sabotage: make `review_held` read only `review_claimed_by` again and nothing is spawned."""
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "GRPH-R1", "review_claim_lapse": "offline"}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert roles, "a lapsed hold must get a reviewer spawned for it"
    assert all(r == "worker" for r in roles), roles
    assert result.reason == "review-unsigned"


def test_until_does_not_idle_on_a_prd_review_row_nobody_holds(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1290. The server now sends a lapsed hold as unheld (`review_claimed_by` empty)
    plus `review_claim_lapse`. A `--prd` drain starts a reviewer for that row. Exiting
    `idle` with the row still listed is the defect.

    Sabotage: make `review_held` return True whenever `review_claim_lapse` is set and
    this run exits idle with nothing spawned."""
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-1287", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": None, "review_claim_lapse": "age",
                 "prd_id": "GRPH-P63"}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        prd="GRPH-P63",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert roles, "an unheld PRD review row must get a reviewer"
    assert result.reason != "idle", result.as_json()
    assert result.review == ["GRPH-1287"]


def test_review_held_reads_the_servers_lapse_and_tolerates_an_older_server():
    """The three cases `review_held` must tell apart, including a server too old to send the
    field, which must read exactly as it did before this change."""
    from gbfleet.until import review_held

    assert review_held({"review_claimed_by": "GRPH-R1"}) is True
    assert review_held({"review_claimed_by": "GRPH-R1", "review_claim_lapse": "age"}) is False
    assert review_held({"review_claimed_by": ""}) is False
    assert review_held({}) is False


def test_a_scoped_wave_does_not_spawn_for_review_outside_its_prd(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1188. `--prd P` with one unheld review row that belongs to no PRD. The reviewer
    seat this loop mints is scoped to P and cannot claim it, so spawning for it only starts
    children that exit — seven of them across two drain runs, each run ending
    `review-unsigned` and cutting short P's own build work. The row is named as outside the
    PRD, and the run ends idle.

    Sabotage: drop the `prd_id` filter in `_split_by_prd` and a spawn happens."""
    workspace = tmp_path / "ws"
    mints: list[dict] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "", "prd_id": None}],
        minted_args=mints,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        prd="SA-P11",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert mints == [], mints
    assert result.spawned == 0
    assert result.minted == 0
    assert result.reason == "idle", result.as_json()
    assert result.exit == 0
    assert result.review == []
    assert result.as_json()["review_outside_prd"] == ["GRPH-9"]


def test_a_scoped_wave_still_spawns_a_reviewer_for_review_inside_its_prd(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1188, the other half: a row whose `prd_id` is the wave's PRD is reviewed as
    before — a review-only seat scoped to the PRD, and a child started on it."""
    workspace = tmp_path / "ws"
    mints: list[dict] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "", "prd_id": "SA-P11"}],
        minted_args=mints,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        prd="SA-P11",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert mints, result.as_json()
    assert mints[0].get("review_only") is True, mints
    assert mints[0].get("scope") == "SA-P11", mints
    assert result.spawned >= 1
    assert result.review == ["GRPH-9"]
    assert result.as_json()["review_outside_prd"] == []


def test_a_scoped_wave_named_by_an_old_key_reviews_its_own_rows(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1190. `--prd` given as a key the PRD no longer carries (a retag, a legacy key);
    the server resolves it, and the review row carries the CURRENT key. The row is this
    PRD's: a reviewer is spawned, scoped to the canonical key, and nothing is outside.

    Sabotage: compare review rows against the raw `--prd` again and the row is listed under
    `review_outside_prd` with no spawn."""
    workspace = tmp_path / "ws"
    mints: list[dict] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "", "prd_id": "GRPH-P11"}],
        minted_args=mints,
        prds={"SA-P11": "GRPH-P11", "GRPH-P11": "GRPH-P11"},
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        prd="SA-P11",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert result.as_json()["review_outside_prd"] == [], result.as_json()
    assert result.review == ["GRPH-9"]
    assert mints, result.as_json()
    assert mints[0].get("review_only") is True, mints
    assert mints[0].get("scope") == "GRPH-P11", mints
    assert result.spawned >= 1


def test_a_scoped_wave_refuses_a_prd_the_server_does_not_know(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1190. An unknown `--prd` ends the run with the server's refusal before anything
    is minted or spawned — not a fallback to the raw string."""
    workspace = tmp_path / "ws"
    mints: list[dict] = []
    calls: list[str] = []
    planner, supervisor = _clients(
        workspace, clusters=1,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "", "prd_id": "SA-P99"}],
        minted_args=mints, calls=calls,
        prds={"GRPH-P11": "GRPH-P11"},
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        prd="SA-P99",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert result.reason == "config", result.as_json()
    assert result.exit == 2
    assert result.ok is False
    assert "prd not found: SA-P99" in result.detail, result.detail
    assert mints == []
    assert result.spawned == 0
    assert "delegate" not in calls and "mint_enrolment" not in calls, calls


def test_a_review_only_supervisor_mints_seats_that_cannot_claim(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-988, and the acceptance criterion. `--max-workers 0` pins `need` at 0, so the
    delegation branch cannot fire and the review branch is this supervisor's ONLY spawner.
    The board here has BOTH halves of the reported situation: a ready unclaimed cluster and an
    unheld review row. Every seat the wave mints must be one the server will not let claim
    build work, or the child spawned to review takes the cluster instead — which is what
    happened to GRPH-965, built on the reviewer's branch by a wave told to build nothing.

    Sabotage: drop `review_only=True` from the review branch's `_take_seat` and this fails."""
    workspace = tmp_path / "ws"
    mints: list[dict] = []
    delegated: list[dict] = []
    planner, supervisor = _clients(
        workspace,
        clusters=1,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": ""}],
        minted_args=mints,
        delegations=delegated,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=0),
    )
    assert mints, f"the wave minted nothing, so this proved nothing: {result.as_json()}"
    # THE CALL: the seat it hands every child is one the server refuses `claim_next` on.
    assert all(m.get("review_only") is True for m in mints), mints
    # The role is unchanged — `reviewer` merged into `worker` in S3 and this is a seat kind,
    # not a job title.
    assert all((m.get("role") or "worker") == "worker" for m in mints), mints
    # And the flag means what it reads as: nothing was delegated, so nothing was built.
    assert delegated == [], delegated


def test_a_review_only_wave_refuses_pre_minted_seats(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-988. A `--seats` file carries codes, and only the server can grant the limit — so
    a pre-minted seat is always a seat that can claim. Handing one to a reviewer child is the
    defect walked back in through a flag combination, which is the same reason `--prd` refuses
    pre-minted seats two tests above.

    Refused as a config error: before the lock, before any worktree, before a credential is
    spent."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        seats=[Seat(code="WORKER-AAAAAA", server_url="http://gb.invalid", api_key=KEY)],
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        limits=Limits(max_workers=0),
    )
    assert result.reason == "config"
    assert result.exit == 2
    assert "--max-workers 0" in result.detail and "pre-minted seats" in result.detail
    assert "Drop --seats" in result.detail, "refused without saying how to proceed"


def test_a_review_only_wave_refuses_a_server_that_cannot_mint_one(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-988. A server that has never heard of `review_only` does not refuse the argument —
    it drops it and answers with a plain worker seat. Without the probe that is the original
    bug wearing the fix's clothes: the operator reads `--max-workers 0` as a promise and the
    wave builds anyway, with nothing anywhere to say so."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": ""}],
        mint_review_only=False,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=0),
    )
    assert result.reason == "config", result.as_json()
    assert result.exit == 2
    assert "review_only" in result.detail, result.detail
    assert "Upgrade the server" in result.detail, "refused without a remedy"


def test_review_only_instruction_forbids_claiming_and_still_teaches_review():
    """GRPH-988. The child is told the limit and told that a refusal is the seat working —
    because the loop every other child is taught ends in "EXIT when both are empty", so a
    reviewer that reads `claim_cluster`'s refusal as an empty queue reports a drained board.
    Not the control; the seat is. This is the turn it saves."""
    from gbfleet.seat import Seat, instruction_for
    text = instruction_for(
        Seat(code="W-1", server_url="https://x", api_key="k", review_only=True),
        Path("/wt"), "gb/r-1",
    )
    assert "REVIEW-ONLY" in text
    assert "claim_review" in text
    assert "Do NOT call claim_cluster or claim_next" in text
    assert "sign_off" in text
    assert "not an empty board" in text


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
    """Criterion 22, second half. The absence is the record: nothing was handed over.
    GRPH-885: when there is no delegable work (empty cluster), no child is spawned. The
    old behavior spawned an unbound child that would call `claim_cluster`, but that is
    exactly the skip-ahead path this fix closes."""
    workspace = tmp_path / "ws"
    delegations: list = []
    calls: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[[]], delegations=delegations, calls=calls,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    # GRPH-885: no delegable work means no spawn. The cluster is empty, so there's nothing
    # to delegate, and spawning an unbound child would just have it call `claim_cluster` on
    # an empty neighborhood.
    assert result.spawned == 0, result.detail
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


def test_a_needs_tier_bounce_outranks_the_tier_flag(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-933. `--tier cheap`, but the last bounce said `needs_tier=frontier`: the loop
    requests frontier, so a cheap child is not re-seated on work a reviewer said it cannot do.
    Sabotage: drop the `needs_tier` override in `_delegate_next` — this reads cheap."""
    workspace = tmp_path / "ws"
    delegations: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], delegations=delegations,
        brief_tier={"value": "frontier", "basis": "needs_tier"},
    )
    run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        request="cheap",
    )
    assert [d["tier"] for d in delegations] == ["frontier"]


def test_a_suggested_tier_does_not_outrank_the_tier_flag(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-933. Only a bounce's NAMED tier outranks the flag; the bounced-one-tier-up
    suggestion is still a suggestion, and `--tier cheap` stands (D5)."""
    workspace = tmp_path / "ws"
    delegations: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], delegations=delegations,
        brief_tier={"value": "frontier", "basis": "bounced"},
    )
    run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        request="cheap",
    )
    assert [d["tier"] for d in delegations] == ["cheap"]


def test_a_refused_delegation_does_not_stop_the_spawn(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-885: a refused bound delegate (conflict / bounce pin) does not mint an
    unbound child. The cluster is held; the next tick retries. Spawning anyway is
    how SA-P14 skip-ahead the DAG via claim_cluster."""
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
    assert result.spawned == 0, result.detail
    assert delegations == []


def test_a_bounced_item_is_delegated_again_by_the_same_run(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1315. The child exits and the item is back in `next` (a bounce): the server keeps
    offering it. The running loop must hand it out again — before the fix it never did, the
    run read idle, and only a restart re-delegated it (GRPH-1310, 1313, 1314, 1322, 1323)."""
    workspace = tmp_path / "ws"
    delegations: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], delegations=delegations,
        sticky_clusters=True,
    )
    run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        limits=Limits(max_workers=1, max_children=2),
    )
    # The cap ends the run, so the count past two is the cap's business; one is the bug.
    assert [d["id"] for d in delegations][:2] == ["GRPH-7", "GRPH-7"]


def test_a_delegation_stays_while_its_child_runs_and_a_hold_stays_until_a_merge():
    """GRPH-1315. Releasing is per child: an item a live child has is not offered twice, and
    a GRPH-798 hold is lifted by a merge, not by somebody else's exit."""
    class C:
        pass
    running, exited = C(), C()
    delegated = {"A", "B", "H", "R"}
    owners = {"A": running, "B": exited}
    until._release_delegated(delegated, owners, [exited], held={"H": ["GRPH-1"]})
    # B: its child exited. R: marked with no child (a refused bound seat), nothing holds it.
    assert delegated == {"A", "H"}
    assert owners == {"A": running}


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


def test_a_refused_bound_seat_does_not_fall_back_to_unbound(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-885: a bound seat refusal means the seed is pinned or its areas are held.
    Do NOT fall back to an unbound delegate — that tells the child to `claim_cluster` on
    the neighborhood, which takes the rest of the glob and skip-aheads the DAG. Instead,
    skip this cluster entirely and let the next tick retry after the pin lapses."""
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
    # GRPH-885: no spawn happens when the bound seat is refused. The cluster is held, not
    # available, and the next tick will retry after the pin lapses or the holder releases.
    assert result.spawned == 0, "a refused bound seat should not spawn an unbound child"
    assert "mint_enrolment" not in calls, "no unbound fallback means no mint_enrolment"
    # GRPH-1643. The refusal is not a reach signal, and it is still a stall: the
    # item stayed ready. `reason` is not idle and the exit is not 0.
    payload = result.as_json()
    assert payload["reason"] == "stalled", payload
    assert payload["exit"] != 0 and payload["ok"] is False
    assert payload["refused"][0]["item"] == "GRPH-7"
    assert payload["refused"][0]["reason"].startswith("bound_seat_refused")


def test_an_idle_run_names_the_ready_item_whose_delegation_was_refused(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1336. The GRPH-P63 drain had one ready item, the server refused its delegation
    on reach every cycle, and the run ended `idle` with `spawned: 0` — the refusal only in
    the observe log. Idle must not read as "nothing to do": the summary's `gated` names the
    item and the server's reason.

    Sabotage: drop the `_note_refused` call in `run` and `gated` is empty.
    """
    workspace = tmp_path / "ws"
    why = ("GRPH-7 has reach=repo but its text reads like work on a deployed system "
           "(platform-command)")
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]],
        bound_seats=True, bound_refused=why,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=2,
    )
    payload = result.as_json()
    assert payload["spawned"] == 0
    refused = [g for g in payload["gated"] if g.startswith("GRPH-7:")]
    assert len(refused) == 1, payload["gated"]
    assert "delegation refused" in refused[0] and "platform-command" in refused[0]


def test_a_ready_item_whose_delegation_stays_refused_on_reach_is_a_stall(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1643. The only ready item is refused because its text reads like a
    deployed system. The summary names it under `refused`, `reason` is `stalled`,
    and the process exits non-zero. Idle with exit 0 is the defect.

    Sabotage: in `_finish`, do not replace idle with stalled when
    `delegate_refused` is set. This exits idle with exit 0 and `ok` true.
    """
    workspace = tmp_path / "ws"
    why = (
        "GRPH-7 is declared `reach=repo`, and reads like work on a deployed system:\n"
        "  · rotate the credential   [platform-command: a hosted secret]\n"
        "If this work really is confined to the worktree, pass `acknowledge_reach=true`"
    )
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]],
        bound_seats=True, bound_refused=why,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=2,
    )
    payload = result.as_json()
    assert payload["spawned"] == 0
    assert payload["reason"] == "stalled", payload
    assert payload["reason"] != "idle"
    assert payload["exit"] != 0
    assert payload["ok"] is False
    assert payload["refused"] == [
        {"item": "GRPH-7", "reason": "bound_seat_refused (reach)"},
    ], payload["refused"]


def test_a_pinned_item_spawns_nothing_until_the_server_stops_reporting_the_pin(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1332. A bounced item reserved for its live author is a wait: no seat is spawned,
    the run does not read idle, and the same run delegates it once the pin is gone."""
    workspace = tmp_path / "ws"
    delegations: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], delegations=delegations,
        bound_seats=True, pinned_ticks=3,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert [d["id"] for d in delegations] == ["GRPH-7"], "delegated once, after the pin lapsed"
    assert result.spawned == 1, result.detail


def test_a_bound_seat_that_cannot_claim_is_logged_and_not_reported_delegated(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """GRPH-1292. Delegate succeeds and the child registers, but the roster says the
    seat did not claim the item (bounce pin). The summary names that refusal. The
    success `delegated` event is not emitted, and the child is stopped.

    Sabotage: drop the `_bound_claim_detail` check in `failover`. This emits
    `delegated` and `gated` has no bound-claim line.
    """
    from gbfleet.spawn import Reason

    workspace = tmp_path / "ws"
    events: list[tuple] = []
    monkeypatch.setattr(
        "gbfleet.until.observe.emit", lambda ev, **kw: events.append((ev, kw)),
    )
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]],
        bound_seats=True, drop_delegated=True,
        roster_assigned={
            "item": "GRPH-7", "state": "taken", "reason": "pinned",
            "held_by": "GRPH-A1",
        },
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=2,
    )
    payload = result.as_json()
    refused = [g for g in payload["gated"] if "bound seat did not claim" in g]
    assert len(refused) == 1, payload["gated"]
    assert "GRPH-7" in refused[0] and "pinned" in refused[0] and "GRPH-A1" in refused[0]
    assert not any(ev == "delegated" for ev, _kw in events), events
    assert any(ev == "bound_claim_refused" for ev, _kw in events)
    assert result.wave is not None and result.wave.spawned
    assert result.wave.spawned[0].stopped_because == Reason.ASKED


def test_a_bound_seat_that_claims_is_still_reported_delegated(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """The control. `assigned.state == claimed` is the child holding the item."""
    workspace = tmp_path / "ws"
    events: list[tuple] = []
    monkeypatch.setattr(
        "gbfleet.until.observe.emit", lambda ev, **kw: events.append((ev, kw)),
    )
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]],
        bound_seats=True,
        roster_assigned={"item": "GRPH-7", "state": "claimed", "reason": None, "held_by": None},
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.spawned == 1, result.detail
    assert any(ev == "delegated" for ev, _kw in events)
    assert not any(ev == "bound_claim_refused" for ev, _kw in events)


def test_the_bound_claim_check_is_in_failover():
    """Sabotage the CALL. Deleting the check in `failover` leaves the runtime test green
    only if this string is gone too — keep both."""
    src = Path(until.__file__).read_text(encoding="utf-8")
    assert "_bound_claim_detail(" in src
    assert 'observe.emit(\n                            "bound_claim_refused"' in src or (
        "bound_claim_refused" in src and "return \"unstaffed\"" in src)




#: The refusal the 2026-11-05 drain logged, before `reserved_until=` existed. The author's
#: child had already exited. Matching only `reserved_until=` recorded `bound_seat_refused`
#: and the run exited idle (GRPH-1324).
_DEAD_AUTHOR_PIN = (
    "GRPH-7 is pinned to its author GRPH-A11 after a bounce "
    "(pinned_to=GRPH-A11; pinned_until=2026-11-05T05:36:57+00:00) "
    "(wait for the pin to lapse, or let the author retry)"
)


def test_a_pinned_bounce_with_a_dead_author_waits_then_redelegates(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1324. The only ready work is pinned to an author whose child has already
    exited (this wave has no live child). The refusal is the incident wording: it
    names `pinned_until` and says to wait for the pin, and it does not say
    `reserved_until=`. until waits and re-delegates once the server stops reporting
    the pin. It does not exit idle with nothing spawned.

    Sabotage: in `_delegate_next`, ignore `_bounce_pin_stamp` and fall through
    to the bound-seat refusal. The run exits `idle` with spawned 0.
    """
    workspace = tmp_path / "ws"
    delegations: list = []
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], delegations=delegations,
        bound_seats=True, pinned_ticks=3, pin_message=_DEAD_AUTHOR_PIN,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert [d["id"] for d in delegations] == ["GRPH-7"], result.as_json()
    assert result.spawned == 1, result.as_json()
    assert not any("delegation refused" in g and "GRPH-7" in g for g in result.as_json()["gated"]), (
        result.as_json()["gated"])
    # The pin was over before the run ended, so the summary does not still list it.
    assert not any("pinned until" in w for w in result.as_json()["waits"]), result.as_json()["waits"]


def test_an_exit_while_a_dead_authors_pin_holds_names_the_pin(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1324, the exit clause. The server keeps the pin and the run stops anyway
    (here, a stall raised from the pin wait). `reason` is not `idle`, and `waits`
    names the item and its `pinned_until`.

    Sabotage: drop the pin lines off the stall report. `waits` is empty.
    """
    from gbfleet import watchdog

    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]],
        bound_seats=True, pinned_ticks=100, pin_message=_DEAD_AUTHOR_PIN,
    )

    def _stop_on_the_pin_wait(_seconds: float) -> None:
        raise watchdog.Stalled("stopped while the pin still holds")

    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=_stop_on_the_pin_wait, empty_ticks=1,
    )
    payload = result.as_json()
    assert result.spawned == 0, payload
    assert payload["reason"] != "idle", payload
    named = [w for w in payload["waits"]
             if "GRPH-7" in w and "2026-11-05T05:36:57+00:00" in w]
    assert named, payload["waits"]





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



def test_the_terminal_json_says_what_the_machine_had(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """`gated: []` is only readable beside a headroom number (GRPH-842).

    On its own an empty list has two meanings and they are opposites: nothing was refused,
    or nothing could be measured and the gate never bound. An unattended `until` reporting
    the second as the first is a machine quietly running one child a wave.
    """
    from gbfleet import headroom, hostos

    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, workers=0)
    monkeypatch.setattr(hostos, "available_memory", lambda: None)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    payload = result.as_json()
    assert payload["gated"] == []
    assert payload["headroom_bytes"] is None, "an unmeasured host must not read as a roomy one"

    monkeypatch.setattr(hostos, "available_memory", lambda: 9 * headroom.DEFAULT_CHILD_MEMORY)
    planner, supervisor = _clients(workspace, clusters=1, workers=0)
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert result.as_json()["headroom_bytes"] == 9 * headroom.DEFAULT_CHILD_MEMORY


def test_until_waits_for_room_instead_of_spawning_into_a_full_machine(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """The command that was actually killed (GRPH-842).

    `until` does not call `supervisor.run` — it drives `_start` one seat at a time from its
    own loop, so gating the wave function alone left the one command in the incident
    ungated. A live child plus a full machine must hold the next spawn.

    Not a `CapError`: memory comes back when a child exits, and ending an unattended drain
    on a passing spike throws away the rest of the backlog.
    """
    from gbfleet import headroom, hostos

    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=4, workers=3, sticky_clusters=True)
    monkeypatch.setattr(hostos, "available_memory", lambda: headroom.RESERVE)

    # GRPH-1608: the live child is held until the gate has refused, not for a 1.0 s wall
    # clock a loaded runner can spend before the loop comes round to the next spawn — at
    # which point nothing is live, the first child is never gated, and nothing is refused.
    # The wall clock and the script's own 10 s cap only bound a sabotaged run.
    release = scripts["works_until_released"].parent / "release"
    refuse = until._room_refusal

    def refuse_then_release(wave, room, live_n):
        reason = refuse(wave, room, live_n)
        if reason is not None:
            release.write_text("", encoding="utf-8")
        return reason

    monkeypatch.setattr(until, "_room_refusal", refuse_then_release)
    result = run(
        git_repo, _factory(scripts, "works_until_released"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0.05, empty_ticks=3,
        limits=Limits(max_workers=4, max_children=2, child_wall_clock=15.0),
    )
    payload = result.as_json()
    assert payload["gated"], "a live child on a full machine must hold the next spawn"
    assert "no room" in payload["gated"][0]
    assert payload["headroom_bytes"] == headroom.RESERVE
    assert result.reason != "config", payload


# ---- GRPH-847: --base for stacked slices -----------------------------------------------------

import subprocess

def _repo_with_remote(tmp_path: Path) -> Path:
    """A repo with a bare remote that has both `main` and an `integration` branch."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True,
                                   check=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    g("remote", "add", "origin", str(bare))
    (repo / "README.md").write_text("x\n")
    g("add", "-A")
    g("commit", "-qm", "first")
    g("push", "-q", "-u", "origin", "main")

    # Create an integration branch on the remote with an extra commit.
    g("checkout", "-q", "-b", "integration")
    (repo / "integration-work.txt").write_text("stacked\n")
    g("add", "-A")
    g("commit", "-qm", "integration base")
    g("push", "-q", "origin", "integration")
    g("checkout", "-q", "main")
    return repo


def test_base_branch_not_found_refuses_at_startup(
    tmp_path: Path, scripts, state: Path,
):
    """GRPH-847. A `--base` that does not exist on the remote refuses at startup,
    naming the branch; it does not fall back to the default ref."""
    repo = _repo_with_remote(tmp_path)
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace)
    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        base_branch="no-such-branch",
    )
    assert result.reason == "config"
    assert result.exit == 2
    assert "no-such-branch" in result.detail


def test_base_branch_cuts_children_from_the_integration_ref(
    tmp_path: Path, scripts, state: Path,
):
    """GRPH-847. With `--base integration`, children are cut from origin/integration,
    not from the default ref. The worktree's base sha is the integration branch tip."""
    repo = _repo_with_remote(tmp_path)
    workspace = tmp_path / "ws"

    # Resolve what origin/integration points at, so we can verify the worktree was cut
    # from it.
    integration_sha = subprocess.run(
        ["git", "rev-parse", "origin/integration"], cwd=repo,
        capture_output=True, text=True,
    ).stdout.strip()

    planner, supervisor = _clients(workspace, clusters=1, workers=0)
    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
        base_branch="integration",
    )
    assert result.spawned == 1
    assert result.reason == "idle"
    # The child's worktree was cut from the integration ref, not main.
    child = result.wave.spawned[0]
    assert child.base == integration_sha, (
        f"child base {child.base[:12]} != integration {integration_sha[:12]}"
    )


def test_dependency_check_reads_the_integration_branch(
    tmp_path: Path, scripts, state: Path,
):
    """GRPH-847 sabotage. The dependency check must read 'merged into <base>', not the
    default ref. A dependency whose commit is on origin/integration but NOT on origin/main
    must be considered present when --base integration is set.

    Sabotage: make deps.check keep reading the default ref (origin/main) → the item whose
    dependency landed only on the integration branch is held when it should be free."""
    repo = _repo_with_remote(tmp_path)
    workspace = tmp_path / "ws"

    # The integration branch has a commit that main does not.
    integration_sha = subprocess.run(
        ["git", "rev-parse", "origin/integration"], cwd=repo,
        capture_output=True, text=True,
    ).stdout.strip()

    # A cluster with one item that depends on a 'done' item whose commit is the
    # integration-only commit. With --base integration, the dependency IS in the base and
    # the item should be delegated (not held).
    planner, supervisor = _clients(
        workspace, clusters=1, workers=0,
        cluster_items=[["GRPH-DEP"]],
    )

    # Patch the mock to return a dependency row for GRPH-DEP whose commit is the
    # integration-only sha.
    orig_handler = planner._transport._handler if hasattr(planner, '_transport') else None

    # We need the related_work call for GRPH-DEP to return a dependency whose commit is
    # on integration but not main. We do this by wrapping the planner's call method.
    real_call = planner.call
    def patched_call(tool, **kw):
        if tool == "related_work" and kw.get("id") == "GRPH-DEP":
            return {"results": [
                {"id": "GRPH-DEP-DEP", "title": "dep", "status": "done",
                 "link_types": ["dependency"],
                 "evidence": [{"kind": "attestation", "commit": integration_sha}]},
            ]}
        return real_call(tool, **kw)
    planner.call = patched_call

    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
        base_branch="integration",
    )
    # The item was NOT held — its dependency is in origin/integration, which is the base.
    assert result.spawned == 1, (
        f"dependency on integration branch should have been satisfied; "
        f"spawned={result.spawned}, reason={result.reason}"
    )
    assert result.reason == "idle"


# --- GRPH-869: the CALL — planner, not supervisor, records the receipt --------

def test_loop_passes_planner_to_watch_tick_and_reap_all():
    """THE CALL. `watch_tick` → `_reap_exited` → `_publish` → `propose_branch` →
    `update_item`. The supervisor allowlist is two reads; `update_item` is on the planner.
    Passing `supervisor` here opened the PR and then logged "PR opened but not recorded"
    on every item.

    Sabotage: swap `planner` for `supervisor` in either call → this test fails. Pinning
    the variable name, not that `PLANNER_TOOLS` contains `update_item` — the set could
    grow and the bug would still be there if the wrong client was passed.
    """
    import inspect
    from gbfleet.until import _loop

    src = inspect.getsource(_loop)

    # watch_tick must receive `planner`, not `supervisor`.
    assert "watch_tick(wave, children, limits, planner" in src, (
        "watch_tick must receive the planner client, not supervisor"
    )
    assert "watch_tick(wave, children, limits, supervisor" not in src, (
        "watch_tick must NOT receive the supervisor client"
    )

    # _reap_all must receive client=planner, not client=None or client=supervisor.
    assert "_reap_all(wave, finished, client=planner" in src, (
        "_reap_all must receive the planner client for PR receipt recording"
    )
    assert "_reap_all(wave, finished, client=supervisor)" not in src


def test_pr_proposed_against_the_integration_branch():
    """GRPH-847 sabotage. PRs must be proposed against the integration branch when
    --base is set, not against the default ref. The wiring: `watch_tick`/`_reap_all`
    receive `base_branch=base`, which flows through `_reap_exited`/`_publish` to
    `_propose` as `base_override`, and from there to `propose_branch`.

    Sabotage: delete the `base_override=base_branch` assignment in `_publish` or the
    `base_branch=base` assignment in `_loop` → this test fails.
    """
    import inspect
    from gbfleet.supervisor import _publish, _propose, _reap_exited, _reap_all
    from gbfleet.until import _loop

    publish_src = inspect.getsource(_publish)
    assert "base_override=base_branch" in publish_src, (
        "_publish must pass base_branch to _propose as base_override"
    )

    propose_src = inspect.getsource(_propose)
    assert "base_override=base_override" in propose_src, (
        "_propose must pass base_override to propose_branch"
    )

    reap_exited_src = inspect.getsource(_reap_exited)
    assert "base_branch=base_branch" in reap_exited_src, (
        "_reap_exited must pass base_branch to _publish"
    )

    reap_all_src = inspect.getsource(_reap_all)
    assert "base_branch=base_branch" in reap_all_src, (
        "_reap_all must pass base_branch to _publish"
    )

    loop_src = inspect.getsource(_loop)
    assert "base_branch=base)" in loop_src, (
        "_loop must pass the resolved base ref to watch_tick and _reap_all"
    )


# --- GRPH-867: the diagnostic surface for cap+duplicate waves -----------------

def test_report_surfaces_collided_give_ups_proposed_and_undeclared():
    """An operator seeing `cap` exit needs to know whether the slots were spent on empty
    exits or duplicate work. The Wave already computes all four; the report just was not
    surfacing them. Empty is 'nothing of this kind happened', not 'we did not look'.

    Sabotage: drop any of these four keys from `as_json` → this test fails.
    """
    from gbfleet.supervisor import Wave
    report = Report(ok=False, reason="cap", exit=1, spawned=8, wave=Wave())
    payload = report.as_json()

    assert "collided" in payload, "collided files must be in the report"
    assert "give_ups" in payload, "give_ups must be in the report"
    assert "proposed" in payload, "proposed PRs must be in the report"
    assert "undeclared" in payload, "undeclared touchpoints must be in the report"

    # Empty is not absent — all four are present with empty defaults.
    assert payload["collided"] == {}
    assert payload["give_ups"] == []
    assert payload["proposed"] == {}
    assert payload["undeclared"] == {}


def test_report_collided_carries_the_actual_overlap():
    """When two branches changed the same file, the operator must see WHICH file."""
    from gbfleet.supervisor import Wave
    wave = Wave()
    wave.collided = {"fleet/src/gbfleet/supervisor.py": ["gb/w-1", "gb/w-2"]}
    report = Report(ok=False, reason="cap", exit=1, spawned=8, wave=wave)
    payload = report.as_json()

    assert "fleet/src/gbfleet/supervisor.py" in payload["collided"]
    assert payload["collided"]["fleet/src/gbfleet/supervisor.py"] == ["gb/w-1", "gb/w-2"]


def test_the_wave_report_counts_what_it_held_as_already_in_base():
    """GRPH-1024. The JSON report always names how many items were not re-delegated
    because their work is already in the base, and why. Zero is present: a missing
    key is the idle exit that hid a queue of remainders for weeks.

    Sabotage: drop `held_in_base` or `in_base` from `as_json`, or stop printing
    `HELD IN BASE` from `cli.report`. This fails.
    """
    import io

    from gbfleet.cli import report
    from gbfleet.supervisor import Wave

    empty = Report(ok=True, reason="idle", exit=0, wave=Wave()).as_json()
    assert empty["held_in_base"] == 0
    assert empty["in_base"] == {}
    assert Report(ok=True, reason="idle", exit=0).as_json()["held_in_base"] == 0
    assert Report(ok=True, reason="idle", exit=0).as_json()["in_base"] == {}

    why = ("branch gb/x reaches origin/main; "
           "believed pr https://example/pull/1 says work existed")
    wave = Wave()
    wave.in_base = {"GRPH-890": why}
    payload = Report(ok=True, reason="idle", exit=0, spawned=0, wave=wave).as_json()
    assert payload["held_in_base"] == 1
    assert payload["in_base"] == {"GRPH-890": why}
    assert payload["reason"] == "idle" and payload["spawned"] == 0

    out = io.StringIO()
    report(wave, out=out)
    text = out.getvalue()
    assert "HELD IN BASE 1" in text
    assert f"GRPH-890: {why}" in text

    quiet = io.StringIO()
    report(Wave(), out=quiet)
    assert "HELD IN BASE 0" in quiet.getvalue()


class _SeedPlanner:
    """Enough of Graphban.call for `_delegate_next` (GRPH-886)."""

    def __init__(self, clusters, details, *, refuse_update: bool = False):
        self.clusters = clusters
        self.details = details
        self.delegated: list[str] = []
        self.updates: list[dict] = []
        self.refuse_update = refuse_update

    def call(self, tool, **kw):
        if tool == "collision_clusters":
            return {"clusters": self.clusters}
        if tool == "get_item_details":
            return dict(self.details[kw["id"]])
        if tool == "related_work":
            return {"results": []}
        if tool == "delegate":
            self.delegated.append(kw["id"])
            return {"enrolment_code": "WORKER-X"}
        if tool == "update_item":
            if self.refuse_update:
                from gbfleet.client import ToolFailed
                raise ToolFailed("update_item", "error", "refused for the test")
            self.updates.append(dict(kw))
            return {"id": kw["id"], "status": kw.get("status")}
        raise AssertionError(tool)


def test_a_review_member_keeps_the_sibling_from_being_the_seed(tmp_path: Path):
    """GRPH-886 CALL. items[0] is review, the sibling is next, and the cluster is not
    held_by. The sibling must not be delegated.

    Sabotage: stop treating status=review as occupying the glob (seed items[0]'s
    neighbor). This fails: delegate is called with SIB.
    """
    from gbfleet.until import _delegate_next

    brief = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}
    planner = _SeedPlanner(
        [{"items": ["REV", "SIB"]}],
        {
            "REV": {"id": "REV", "status": "review", "brief": brief},
            "SIB": {"id": "SIB", "status": "next", "brief": brief},
        },
    )
    seed, _code, _want = _delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=tmp_path, base="origin/main",
    )
    assert seed is None
    assert planner.delegated == []


def test_a_git_merged_member_occupies_the_glob(tmp_path: Path, monkeypatch):
    """GRPH-886 CALL. Unsigned `next` already in base occupies the glob. The sibling
    is not the seed, in either order.

    Sabotage: skip the merged id and seed the next ready member. This fails: seed is
    READY and delegate was called with READY.
    """
    from gbfleet import worktree as wt
    from gbfleet.until import _delegate_next

    monkeypatch.setattr(wt, "reaches", lambda repo, base, ref: ref == "gb/merged")
    brief = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}
    details = {
        "MERGED": {"id": "MERGED", "status": "next", "branch": "gb/merged", "pr": "https://github.com/o/r/pull/1",
                   "brief": brief, "evidence": []},
        "READY": {"id": "READY", "status": "next", "branch": "gb/ready",
                  "brief": brief, "evidence": []},
    }
    for order in (["MERGED", "READY"], ["READY", "MERGED"]):
        planner = _SeedPlanner([{"items": order}], details)
        seen: set[str] = set()
        seed, _code, _want = _delegate_next(
            planner, "GRPH-A1", "w", seen, None, repo=tmp_path, base="origin/main",
        )
        assert seed is None
        assert planner.delegated == []
        assert "READY" not in seen


def _merged_and_ready(monkeypatch):
    from gbfleet import worktree as wt

    monkeypatch.setattr(wt, "reaches", lambda repo, base, ref: ref == "gb/merged")
    brief = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}
    return {
        "MERGED": {"id": "MERGED", "status": "next", "branch": "gb/merged",
                   "pr": "https://github.com/o/r/pull/1", "brief": brief, "evidence": []},
        "READY": {"id": "READY", "status": "next", "branch": "gb/ready",
                  "brief": brief, "evidence": []},
    }


def test_a_git_merged_unsigned_item_is_offered_for_review(tmp_path: Path, monkeypatch):
    """GRPH-1408. An item merged into the base but unsigned is moved to `review`, so the
    drain's review staffing spawns a reviewer for it, rather than being held forever (as
    GRPH-1359 was after #1388). It still occupies its files for this tick.

    Sabotage: hold it without the update_item move. This fails: no review move recorded.
    """
    from gbfleet.until import _delegate_next

    planner = _SeedPlanner([{"items": ["MERGED", "READY"]}], _merged_and_ready(monkeypatch))
    seen: set[str] = set()
    seed, _code, _want = _delegate_next(
        planner, "GRPH-A1", "w", seen, None, repo=tmp_path, base="origin/main",
    )
    assert seed is None and planner.delegated == []
    assert [(u["id"], u["status"]) for u in planner.updates] == [("MERGED", "review")]
    assert "origin/main" in planner.updates[0]["evidence"][0]["detail"]
    assert "MERGED" in seen


def test_a_refused_review_offer_falls_back_to_the_hold(tmp_path: Path, monkeypatch):
    """GRPH-1408. When the server refuses the move, the item is held exactly as before and
    not marked delegated, so a later tick tries again."""
    from gbfleet.until import _delegate_next

    planner = _SeedPlanner([{"items": ["MERGED", "READY"]}], _merged_and_ready(monkeypatch),
                           refuse_update=True)
    seen: set[str] = set()
    seed, _code, _want = _delegate_next(
        planner, "GRPH-A1", "w", seen, None, repo=tmp_path, base="origin/main",
    )
    assert seed is None and planner.delegated == [] and planner.updates == []
    assert "MERGED" not in seen


def _bounced_and_sibling(monkeypatch, *, bounce: str):
    """MERGED's branch is in the base. SIB shares one of its files. `bounce` is MERGED's
    bounce_reason — empty means the control, a sentence means a remainder."""
    from gbfleet import worktree as wt

    monkeypatch.setattr(wt, "reaches", lambda repo, base, ref: ref == "gb/merged")
    brief = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}
    shared = "fleet/src/gbfleet/until.py"
    return {
        "MERGED": {"id": "MERGED", "status": "next", "branch": "gb/merged",
                   "pr": "https://github.com/o/r/pull/1", "brief": brief, "evidence": [],
                   "bounce_reason": bounce, "touchpoints": [shared, "fleet/tests/test_until.py"]},
        "SIB": {"id": "SIB", "status": "next", "branch": "gb/sib", "brief": brief,
                "evidence": [], "bounce_reason": "", "touchpoints": [shared]},
    }


def test_a_bounced_item_whose_first_half_merged_is_delegated(tmp_path: Path, monkeypatch, caplog):
    """GRPH-1024. An item in `next` with a bounce, whose earlier work is in the base, is
    delegated. It is not offered for review of the merged half, and it is not recorded
    as held.

    Sabotage: `if why and bounced` -> `if why and False`. This fails: seed is None.
    """
    import logging

    from gbfleet.until import _delegate_next

    planner = _SeedPlanner(
        [{"items": ["MERGED"]}],
        {"MERGED": _bounced_and_sibling(monkeypatch, bounce="clause 4 is still unpinned")["MERGED"]},
    )
    held_map: dict[str, str] = {}
    with caplog.at_level(logging.INFO, logger="gbfleet"):
        seed, code, _want = _delegate_next(
            planner, "GRPH-A1", "w", set(), None, repo=tmp_path, base="origin/main",
            in_base=held_map,
        )
    assert seed == "MERGED" and code == "WORKER-X"
    assert planner.delegated == ["MERGED"]
    assert planner.updates == []
    assert held_map == {}
    remainder = [r for r in caplog.records if r.message == "delegate_remainder"]
    assert len(remainder) == 1
    detail = remainder[0].gbfleet["detail"]
    assert "bounce asks for the remainder" in detail
    assert "branch gb/merged reaches origin/main" in detail
    assert "pr https://github.com/o/r/pull/1" in detail
    assert "believed" in detail


def test_an_unbounced_item_already_in_base_is_still_held_and_counted(
        tmp_path: Path, monkeypatch, caplog):
    """GRPH-1024 control, and GRPH-930 / GRPH-983 still hold. No bounce: the item
    occupies its files, the sibling that shares one is not the seed, and `in_base`
    names the signal. GRPH-1408 still offers the unsigned item for review. When that
    offer is refused, the hold line carries the same signal.

    Sabotage: treat the unbounced item as a remainder. This fails: seed is SIB and
    `in_base` is empty.
    """
    import logging

    from gbfleet.until import _delegate_next

    details = _bounced_and_sibling(monkeypatch, bounce="")
    held_map: dict[str, str] = {}
    planner = _SeedPlanner([{"items": ["SIB", "MERGED"]}], details)
    seed, _code, _want = _delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=tmp_path, base="origin/main",
        in_base=held_map,
    )
    assert seed is None and planner.delegated == []
    assert [(u["id"], u["status"]) for u in planner.updates] == [("MERGED", "review")]
    reason = held_map["MERGED"]
    assert reason.startswith("branch gb/merged reaches origin/main")
    assert "believed pr https://github.com/o/r/pull/1 says work existed" in reason

    refused = _SeedPlanner([{"items": ["SIB", "MERGED"]}], details, refuse_update=True)
    refused_map: dict[str, str] = {}
    with caplog.at_level(logging.INFO, logger="gbfleet"):
        seed, _code, _want = _delegate_next(
            refused, "GRPH-A1", "w", set(), None, repo=tmp_path, base="origin/main",
            in_base=refused_map,
        )
    assert seed is None and refused.delegated == [] and refused.updates == []
    assert refused_map["MERGED"] == reason
    held = [r for r in caplog.records
            if r.message == "delegate_held" and r.gbfleet.get("item") == "MERGED"]
    assert len(held) == 1 and reason in held[0].gbfleet["detail"]
    assert "fleet/src/gbfleet/until.py" in held[0].gbfleet["detail"]


def test_a_bounced_item_in_base_does_not_block_its_files_siblings(
        tmp_path: Path, monkeypatch):
    """GRPH-1024 cascade. The bounced item shares `until.py` with SIB. SIB is still
    the seed: one remainder must not make its file's other items unclaimable.

    SIB is listed first, so a wrong occupier removes SIB before the bounced item
    could be chosen. Sabotage: `if why and bounced` -> `if why and False`. This
    fails: seed is None.
    """
    from gbfleet.until import _delegate_next

    planner = _SeedPlanner(
        [{"items": ["SIB", "MERGED"]}],
        _bounced_and_sibling(monkeypatch, bounce="the documenting half did not merge"),
    )
    held_map: dict[str, str] = {}
    seed, code, _want = _delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=tmp_path, base="origin/main",
        in_base=held_map,
    )
    assert seed == "SIB" and code == "WORKER-X"
    assert planner.delegated == ["SIB"]
    assert "MERGED" not in held_map
    assert planner.updates == []


def test_the_loop_reports_the_hold_it_recorded(tmp_path: Path, monkeypatch, scripts, state: Path):
    """GRPH-1024. The wave-end count is what `_loop` recorded, not a map a test handed
    to `_delegate_next` and not a `wave.in_base` the test assigned.

    GRPH-893 is `next`, unsigned, and its branch is already in the base. The loop
    holds it, offers it for review, and the report says 1. `run` is the only caller
    here. It does not pass its own map and it does not write `wave.in_base`.

    Sabotage: delete `in_base=wave.in_base` from the `_loop` call, the only
    production caller. This fails: `held_in_base` is 0 and the JSON `in_base` is
    empty, while the review offer still happens.
    """
    import io

    from gbfleet import worktree as wt
    from gbfleet.cli import report

    repo = _repo_with_remote(tmp_path)
    real = wt.reaches

    def reaches(path, base, ref):
        if ref == "gb/merged":
            return True
        return real(path, base, ref)

    monkeypatch.setattr(wt, "reaches", reaches)
    why = ("branch gb/merged reaches origin/main; "
           "believed pr https://github.com/o/r/pull/806 says work existed")
    updates: list[dict] = []
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace, clusters=1, workers=0, updates=updates,
        cluster_items=[["GRPH-893"]],
        item_details={"GRPH-893": {
            "id": "GRPH-893",
            "status": "next",
            "branch": "gb/merged",
            "pr": "https://github.com/o/r/pull/806",
            "bounce_reason": "",
            "evidence": [],
            "touchpoints": ["backend/app/services/fleet.py"],
            "brief": {"lane": {"value": "backend"}, "tier": {"value": "cheap"},
                      "blocked_by": []},
        }},
    )
    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
        base_branch="main",
    )
    assert result.reason == "idle", result.detail
    assert result.spawned == 0
    assert [(u.get("id"), u.get("status")) for u in updates] == [("GRPH-893", "review")]
    payload = result.as_json()
    assert payload["held_in_base"] == 1
    assert payload["in_base"] == {"GRPH-893": why}
    assert result.wave is not None and result.wave.in_base == {"GRPH-893": why}

    out = io.StringIO()
    report(result.wave, out=out)
    text = out.getvalue()
    assert "HELD IN BASE 1" in text
    assert f"GRPH-893: {why}" in text


def test_in_base_reason_names_each_signal(tmp_path: Path, monkeypatch):
    """The three signals read as three sentences. One shared 'already in base' would
    leave an operator unable to tell a branch receipt from a squash."""
    from gbfleet import propose as propose_mod
    from gbfleet import worktree as wt
    from gbfleet.until import _already_in_base, _in_base_reason

    def reaches(repo, base, ref):
        return ref in {"gb/merged", "abc123"}

    monkeypatch.setattr(wt, "reaches", reaches)
    branch_reason = _in_base_reason(
        {"branch": "gb/merged", "pr": "https://github.com/o/r/pull/1", "evidence": []},
        tmp_path, "origin/main")
    assert branch_reason == (
        "branch gb/merged reaches origin/main; "
        "believed pr https://github.com/o/r/pull/1 says work existed")
    assert _already_in_base(
        {"branch": "gb/merged", "pr": "https://github.com/o/r/pull/1"},
        tmp_path, "origin/main") is True

    commit_reason = _in_base_reason(
        {"branch": "gb/ahead", "evidence": [{"kind": "test", "commit": "abc123"}]},
        tmp_path, "origin/main")
    assert commit_reason == (
        "commit abc123 reaches origin/main; believed a test receipt about gb/ahead")
    assert "branch gb/ahead reaches" not in commit_reason

    monkeypatch.setattr(propose_mod, "view", lambda *a, **kw: (
        {"state": "MERGED", "number": 42, "mergeCommit": {"oid": "c" * 40}}, ""))
    squash = {
        "branch": "feature",
        "evidence": [{"commit": "pre-squash", "kind": "sabotage"}],
        "pr": "https://github.com/o/r/pull/42",
    }
    # `feature` and `pre-squash` are not ancestors. The forge is the signal.
    squash_reason = _in_base_reason(squash, tmp_path, "origin/main")
    # Carrier and sha are GRPH-1023's `_merged_pr_of`. "believed the forge" is the
    # GRPH-1024 signal name, so this sentence is not an ancestry hold.
    assert squash_reason == (
        f"PR #42 (the item's pr) is MERGED at {'c' * 40}; believed the forge, "
        "because ancestry did not find the work in origin/main")
    assert _already_in_base(squash, tmp_path, "origin/main") is True
    assert "branch feature reaches" not in squash_reason


def test_a_blocked_items0_is_not_the_seed(tmp_path: Path):
    """GRPH-886. items[0] still names an unfinished dependency; the next ready member is the seed.

    Sabotage: seed the first id in the cluster. This fails: the seed is BLOCKED.
    """
    from gbfleet.until import _delegate_next

    blocked = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": ["DEP"]}
    ready = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}
    planner = _SeedPlanner(
        [{"items": ["BLOCKED", "READY"]}],
        {
            "BLOCKED": {"id": "BLOCKED", "status": "next", "brief": blocked},
            "READY": {"id": "READY", "status": "next", "brief": ready},
        },
    )
    seed, code, _want = _delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=tmp_path, base="",
    )
    assert seed == "READY" and code == "WORKER-X"
    assert planner.delegated == ["READY"]


# --- GRPH-930: _already_in_base forge fallback for squash merges ---------------

import subprocess as _sp


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: _sp.run(["git", *a], cwd=repo, capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    run("add", "-A")
    run("commit", "-qm", "one")
    return repo


def _branch_and_commit(repo: Path, name: str) -> str:
    run = lambda *a: _sp.run(["git", *a], cwd=repo, capture_output=True, text=True)
    run("checkout", "-q", "-b", name)
    (repo / f"{name}.txt").write_text("work\n")
    run("add", "-A")
    run("commit", "-qm", name)
    sha = _sp.run(["git", "rev-parse", "HEAD"], cwd=repo,
                   capture_output=True, text=True).stdout.strip()
    run("checkout", "-q", "main")
    return sha


def test_already_in_base_recognises_squash_merged_pr(tmp_path, monkeypatch):
    """A squash-merged item's pre-squash SHA is not ancestral to main, but the forge says
    the PR is MERGED. The work IS in base — the squash SHA carries it (GRPH-930)."""
    from gbfleet import propose as propose_mod
    from gbfleet.until import _already_in_base, _in_base_reason

    repo = _git_repo(tmp_path)
    pre_squash = _branch_and_commit(repo, "feature")

    monkeypatch.setattr(propose_mod, "view", lambda *a, **kw: (
        {"state": "MERGED", "mergeCommit": {"oid": "c" * 40}}, ""))

    details = {
        "branch": "feature",
        "evidence": [{"commit": pre_squash, "kind": "sabotage"}],
        "pr": "https://github.com/o/r/pull/42",
    }
    assert _already_in_base(details, repo, "main") is True
    reason = _in_base_reason(details, repo, "main")
    assert "is MERGED" in reason
    assert "believed the forge" in reason
    assert "branch feature reaches" not in reason


def test_already_in_base_open_pr_is_not_in_base(tmp_path, monkeypatch):
    """An open PR with a non-ancestral SHA: the work is NOT in base yet."""
    from gbfleet import propose as propose_mod
    from gbfleet.until import _already_in_base

    repo = _git_repo(tmp_path)
    pre_squash = _branch_and_commit(repo, "feature")

    monkeypatch.setattr(propose_mod, "view", lambda *a, **kw: (
        {"state": "OPEN"}, ""))

    details = {
        "branch": "feature",
        "evidence": [{"commit": pre_squash}],
        "pr": "https://github.com/o/r/pull/42",
    }
    assert _already_in_base(details, repo, "main") is False


def test_already_in_base_unreachable_forge_is_not_in_base(tmp_path, monkeypatch):
    """Forge unreachable: the PR might be merged but we cannot tell. Not True —
    `_already_in_base` must not skip the cluster on an uncertain answer."""
    from gbfleet import propose as propose_mod
    from gbfleet.until import _already_in_base

    repo = _git_repo(tmp_path)
    pre_squash = _branch_and_commit(repo, "feature")

    monkeypatch.setattr(propose_mod, "view", lambda *a, **kw: (None, "gh could not run"))

    details = {
        "branch": "feature",
        "evidence": [{"commit": pre_squash}],
        "pr": "https://github.com/o/r/pull/42",
    }
    assert _already_in_base(details, repo, "main") is False


def test_already_in_base_no_pr_with_unmerged_sha_is_not_in_base(tmp_path, monkeypatch):
    """No PR info, SHA not ancestral. The forge has nothing to say — not in base."""
    from gbfleet import propose as propose_mod
    from gbfleet.until import _already_in_base

    repo = _git_repo(tmp_path)
    pre_squash = _branch_and_commit(repo, "feature")

    monkeypatch.setattr(propose_mod, "view", lambda *a, **kw: (None, "no PR found"))

    details = {
        "branch": "feature",
        "evidence": [{"commit": pre_squash}],
    }
    assert _already_in_base(details, repo, "main") is False


# --- GRPH-1023: a sibling's PR url, and a sibling PR on this branch, are not this merge ---

_SIBLING_URL = "https://github.com/asc-me/graphban/pull/803"
_OWN_URL = "https://github.com/o/r/pull/900"
_OWN_SHA = "c0ffee000000"
_SIBLING_SHA = "b74e2af7" + "ab" * 16
_BRANCH_SHA = "f4bf35b51d1ddf4048426ebcd43652f164f99a4d"


def _ahead(repo: Path, name: str) -> None:
    """One commit on `name`, left checked out on main. `name` may contain slashes."""
    run = lambda *a: _sp.run(["git", *a], cwd=repo, capture_output=True, text=True)
    run("checkout", "-q", "-b", name)
    (repo / "work.txt").write_text(name + "\n")
    run("add", "-A")
    run("commit", "-qm", name)
    run("checkout", "-q", "main")


def _grph890(repo: Path) -> dict:
    """GRPH-890's shape: branch ahead of main, one url receipt that names another item."""
    _ahead(repo, "gb/p45-followups")
    return {
        "id": "GRPH-890",
        "status": "next",
        "branch": "gb/p45-followups",
        "touchpoints": ["docs/prd-44-pstack.md"],
        "evidence": [{
            "kind": "url",
            "url": _SIBLING_URL,
            "detail": "WALK-A9 opened PR #803 for GRPH-891 (squash-aware release notes)",
        }],
    }


def _view_by_url(answers: dict):
    """`propose.view` that answers per selector. Anything else is "no PR", not MERGED."""

    def view(_repo, selector, **_kw):
        for needle, pr in answers.items():
            if needle in str(selector):
                return pr, ""
        return None, f"no PR for {selector}"

    return view


def _own_merged_pr() -> dict:
    return {
        "state": "MERGED", "number": 900, "headRefName": "gb/p45-followups",
        "title": "GRPH-900: the work", "body": "draft for gb/p45-followups",
        "mergeCommit": {"oid": _OWN_SHA},
    }


def _sibling_merged_pr() -> dict:
    return {
        "state": "MERGED", "number": 803, "headRefName": "fix/release-notes-squash",
        "title": "Release notes read squash landings, not only merges (GRPH-891)",
        "body": "",
        "mergeCommit": {"oid": _SIBLING_SHA},
    }


def _shared_branch_merged_pr() -> dict:
    """PR #920: MERGED, head is this item's branch, and it names other items."""
    return {
        "state": "MERGED", "number": 920, "headRefName": "gb/p45-followups",
        "title": "GRPH-990: follow-ups",
        "body": (
            "Opened by `gbfleet` after reaping `gb/p45-followups`.\n\n"
            "Items: GRPH-990, GRPH-995, GRPH-996\n"
        ),
        "mergeCommit": {"oid": _BRANCH_SHA},
    }


def _grph890_view():
    """The measured forge. #803 is the sibling url; #920 is what the branch resolves to.

    A stub that answers only `/pull/803` never resolves the branch candidate, so a
    check that believes every merged PR the branch names stays green.
    """
    return _view_by_url({
        "/pull/803": _sibling_merged_pr(),
        "gb/p45-followups": _shared_branch_merged_pr(),
    })


def test_a_siblings_merged_pr_url_does_not_make_an_item_read_as_merged(tmp_path, monkeypatch):
    """GRPH-890's measured reading. The url receipt names GRPH-891 and PR #803 is MERGED
    on `fix/release-notes-squash`. The branch `gb/p45-followups` resolves to MERGED PR
    #920, whose title and body name GRPH-990/995/996 and not GRPH-890. Neither is this
    item's merge.

    Sabotage: `_merged_pr_of`, `if not ours and not (branch and head == branch)` ->
    `if False` (believe every url receipt). This goes True on #803.

    Sabotage: `_merged_pr_of`, `if carrier == "the item's branch" and not
    _pr_text_names_item(pr, item_id)` -> `if False` (believe every PR the branch names).
    This goes True on #920.
    """
    from gbfleet import propose as propose_mod
    from gbfleet.until import _already_in_base, _in_base_reason

    repo = _git_repo(tmp_path)
    monkeypatch.setattr(propose_mod, "view", _grph890_view())
    item = _grph890(repo)

    assert _in_base_reason(item, repo, "main") == ""
    assert _already_in_base(item, repo, "main") is False


def test_the_siblings_url_does_not_stop_the_item_being_delegated(tmp_path, monkeypatch):
    """THE CALL. `_already_in_base` False is not enough: `_delegate_next` is what refuses
    the item and leaves the wave idle. GRPH-890's shape, with the branch resolving to
    #920, is still the seed. Both ignored PRs are named.

    Sabotage: either `if False` in `_merged_pr_of`. This delegates nothing.
    """
    from gbfleet import propose as propose_mod
    from gbfleet.until import _delegate_next

    repo = _git_repo(tmp_path)
    monkeypatch.setattr(propose_mod, "view", _grph890_view())
    events: list[tuple] = []
    monkeypatch.setattr(
        "gbfleet.until.observe.emit", lambda ev, **kw: events.append((ev, kw)),
    )
    brief = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}
    item = {**_grph890(repo), "brief": brief}
    planner = _SeedPlanner([{"items": ["GRPH-890"]}], {"GRPH-890": item})

    seed, code, _want = _delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=repo, base="main",
    )

    assert seed == "GRPH-890" and code == "WORKER-X"
    assert planner.delegated == ["GRPH-890"]
    ignored = [kw["detail"] for ev, kw in events if ev == "in_base_ignored"]
    assert any("PR #803" in line and _SIBLING_SHA in line for line in ignored)
    assert any("PR #920" in line and _BRANCH_SHA in line and "the item's branch" in line
               for line in ignored)
    assert not any(ev == "delegate_held" for ev, _kw in events)


def test_an_items_own_merged_pr_receipt_still_holds_it(tmp_path, monkeypatch):
    """The control for GRPH-930. The item's OWN merged url is still a hold, and the
    reason names the carrier and the sha. A refused review offer is the path that
    emits `delegate_held`; the reason is on that line.

    Sabotage: drop the url from the candidates. The reason is empty and the item is delegated.
    """
    from gbfleet import propose as propose_mod
    from gbfleet.until import _already_in_base, _delegate_next, _in_base_reason

    repo = _git_repo(tmp_path)
    monkeypatch.setattr(propose_mod, "view", _view_by_url({"/pull/900": _own_merged_pr()}))
    events: list[tuple] = []
    monkeypatch.setattr(
        "gbfleet.until.observe.emit", lambda ev, **kw: events.append((ev, kw)),
    )
    brief = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}
    item = {
        "id": "GRPH-900",
        "status": "next",
        "branch": "gb/p45-followups",
        "touchpoints": ["docs/prd-44-pstack.md"],
        "brief": brief,
        "evidence": [{
            "kind": "url",
            "url": _OWN_URL,
            "detail": "draft PR opened by gbfleet for gb/p45-followups",
        }],
    }
    _ahead(repo, "gb/p45-followups")

    reason = _in_base_reason(item, repo, "main")
    assert "PR #900" in reason
    assert "url receipt" in reason
    assert _OWN_SHA in reason
    assert _already_in_base(item, repo, "main") is True

    planner = _SeedPlanner(
        [{"items": ["GRPH-900"]}], {"GRPH-900": item}, refuse_update=True,
    )
    seed, _code, _want = _delegate_next(
        planner, "GRPH-A1", "w", set(), None, repo=repo, base="main",
    )
    assert seed is None and planner.delegated == []
    held = [kw for ev, kw in events if ev == "delegate_held"]
    assert len(held) == 1
    assert "PR #900" in held[0]["detail"]
    assert "url receipt" in held[0]["detail"]
    assert _OWN_SHA in held[0]["detail"]


def test_a_receipt_naming_nothing_is_believed_when_the_forge_says_its_head_is_ours(
        tmp_path, monkeypatch):
    """A url receipt with no item key and no branch is still this item's PR when the
    forge's `headRefName` is the item's branch. Refusing every nameless receipt would
    drop the ones written before the supervisor started naming the branch."""
    from gbfleet import propose as propose_mod
    from gbfleet.until import _already_in_base, _in_base_reason

    repo = _git_repo(tmp_path)
    _ahead(repo, "gb/p45-followups")
    monkeypatch.setattr(propose_mod, "view", _view_by_url({"/pull/900": _own_merged_pr()}))
    item = {
        "id": "GRPH-900",
        "branch": "gb/p45-followups",
        "evidence": [{
            "kind": "url",
            "url": _OWN_URL,
            "detail": "opened a pull request",
        }],
    }

    reason = _in_base_reason(item, repo, "main")
    assert _already_in_base(item, repo, "main") is True
    assert "PR #900" in reason and "url receipt" in reason and _OWN_SHA in reason


def test_a_siblings_receipt_written_first_does_not_hide_the_items_own(tmp_path, monkeypatch):
    """Evidence only appends, and the sibling url was written first. Skipping it has to
    keep looking: the item's own merged PR is the hold, named as #900, not #803.

    Sabotage: the url `if False` in `_merged_pr_of`. The reason names #803, and the
    ignored event never fires.
    """
    from gbfleet import propose as propose_mod
    from gbfleet.until import _in_base_reason

    repo = _git_repo(tmp_path)
    _ahead(repo, "gb/p45-followups")
    monkeypatch.setattr(propose_mod, "view", _view_by_url({
        "/pull/803": _sibling_merged_pr(),
        "/pull/900": _own_merged_pr(),
    }))
    events: list[tuple] = []
    monkeypatch.setattr(
        "gbfleet.until.observe.emit", lambda ev, **kw: events.append((ev, kw)),
    )
    item = {
        "id": "GRPH-890",
        "branch": "gb/p45-followups",
        "evidence": [
            {
                "kind": "url",
                "url": _SIBLING_URL,
                "detail": "WALK-A9 opened PR #803 for GRPH-891 (squash-aware release notes)",
            },
            {
                "kind": "url",
                "url": _OWN_URL,
                "detail": "draft PR opened by gbfleet for gb/p45-followups",
            },
        ],
    }

    reason = _in_base_reason(item, repo, "main")
    assert "PR #900" in reason and "url receipt" in reason and _OWN_SHA in reason
    assert "PR #803" not in reason
    ignored = [kw for ev, kw in events if ev == "in_base_ignored"]
    assert ignored and "PR #803" in ignored[0]["detail"] and _SIBLING_SHA in ignored[0]["detail"]


def test_a_branch_pr_that_names_this_item_still_holds(tmp_path, monkeypatch):
    """The branch fallback still holds a squash merge when the PR the branch resolves
    to names this item. Refusing every branch lookup would re-open GRPH-930 for an
    item whose only selector is its branch."""
    from gbfleet import propose as propose_mod
    from gbfleet.until import _already_in_base, _in_base_reason

    repo = _git_repo(tmp_path)
    _ahead(repo, "gb/p45-followups")
    pr = _shared_branch_merged_pr()
    pr["number"] = 921
    pr["title"] = "GRPH-890: the pstack doc"
    pr["body"] = "Opened by `gbfleet` after reaping `gb/p45-followups`.\n\nItems: GRPH-890\n"
    pr["mergeCommit"] = {"oid": _OWN_SHA}
    monkeypatch.setattr(propose_mod, "view", _view_by_url({"gb/p45-followups": pr}))
    item = {
        "id": "GRPH-890",
        "status": "next",
        "branch": "gb/p45-followups",
        "touchpoints": ["docs/prd-44-pstack.md"],
    }

    reason = _in_base_reason(item, repo, "main")
    assert _already_in_base(item, repo, "main") is True
    assert "PR #921" in reason and "the item's branch" in reason and _OWN_SHA in reason


# ---- GRPH-988, bounce 1: the two unpinned `_take_seat` call sites -------------------------
#
# The reviewer of PR #899 mutated both and got 0 failures each:
#   1. `if pool and not review_only:` -> `if pool:`
#   2. dropping `review_only=review_only` from the minted `Seat(...)`
#
# The startup refusal only covers `--max-workers 0` + `--seats`. At `--max-workers > 0` the
# review branch is still reachable and the pool is still live, so (1) is the only thing
# standing between a reviewer child and a pre-minted, claim-capable seat. (2) is not a safety
# hole — the server refuses the claim regardless — but it decides which INSTRUCTION the child
# is handed, and a child told to `claim_cluster` spends its run walking into refusals.


def test_a_review_only_seat_is_never_taken_from_the_pre_minted_pool():
    """THE GAP THE BOUNCE FOUND, pinned where the guard actually is.

    The startup refusal only covers `--max-workers 0` + `--seats`. At `--max-workers > 0` the
    review branch is still reachable with a live pool, and `if pool and not review_only` is
    the only thing between a reviewer child and a pre-minted, claim-capable seat. A seats file
    carries codes, never powers: there is no way to pre-mint a seat that may review but not
    claim, so popping one here hands a reviewer the `claim_next` its wave was forbidden.

    Asserted directly on `_take_seat` rather than through `run`. I tried the integration route
    first and it proved nothing twice: with ready work the worker branch pops the only pooled
    seat before the review branch looks, and without it the wave never reached the guard — the
    mutation stayed green both times. The mutation is IN this function, so this is where the
    claim belongs; the wave-level behaviour is already covered by the two tests above.

    Sabotage: `if pool and not review_only:` -> `if pool:` → this fails.
    """
    from gbfleet.until import _take_seat

    class _Minter:
        def __init__(self):
            self.args: list[dict] = []

        def call(self, tool, **args):
            self.args.append(args)
            return {"enrolment_code": "WORKER-REVONLY"}

    pooled = Seat(code="WORKER-POOLED", server_url="http://gb.invalid", api_key=KEY)
    pool = [pooled]
    minter = _Minter()

    seat, minted = _take_seat(
        pool, minter, "GRPH-A1", "w", "http://gb.invalid", KEY,
        mint_left=1, mint_deadline=9e9, sleep=lambda _: None, review_only=True,
    )

    assert minted is True, "a review-only seat was taken from the pool instead of minted"
    assert seat.code != pooled.code
    assert pool == [pooled], "the pre-minted seat was popped for a reviewer child"
    assert minter.args and minter.args[0].get("review_only") is True, minter.args


def test_a_plain_worker_still_takes_from_the_pool():
    """The control, and the reason the guard is `and not review_only` rather than a blanket
    refusal: an ordinary worker must still consume the `--seats` pool, or this fix would
    quietly disable the flag it is written for."""
    from gbfleet.until import _take_seat

    class _NoMint:
        def call(self, tool, **args):  # pragma: no cover — must not be reached
            raise AssertionError("minted instead of taking the pooled seat")

    pooled = Seat(code="WORKER-POOLED", server_url="http://gb.invalid", api_key=KEY)
    pool = [pooled]

    seat, minted = _take_seat(
        pool, _NoMint(), "GRPH-A1", "w", "http://gb.invalid", KEY,
        mint_left=1, mint_deadline=9e9, sleep=lambda _: None,
    )

    assert minted is False and seat is pooled
    assert pool == [], "the pool was not consumed"


def test_the_seat_the_review_branch_returns_carries_the_flag(monkeypatch):
    """The second mutation. `_take_seat` may mint correctly and still hand back a Seat with
    the flag dropped — the server refuses the claim either way, so no test failed. What breaks
    is the CHILD's instruction: without the flag it is handed INSTRUCTION, which tells it to
    `claim_cluster`, instead of REVIEW_ONLY_INSTRUCTION.

    Sabotage: drop `review_only=review_only` from the returned `Seat(...)` → this fails.
    """
    from gbfleet.seat import INSTRUCTION, instruction_for
    from gbfleet.until import _take_seat

    class _Minter:
        def call(self, tool, **args):
            assert tool == "mint_enrolment"
            assert args.get("review_only") is True, args
            return {"enrolment_code": "WORKER-REVONLY"}

    seat, minted = _take_seat(
        [], _Minter(), "GRPH-A1", "w", "http://gb.invalid", KEY,
        mint_left=1, mint_deadline=9e9, sleep=lambda _: None, review_only=True,
    )

    assert minted is True
    assert seat.review_only is True, "the minted seat dropped the flag"
    text = instruction_for(seat, Path("/tmp/wt"), "gb/w-1")
    # Both templates open identically, and REVIEW_ONLY names `claim_cluster` in order to
    # FORBID it — so the distinguishing phrase is the one that tells the child a refusal is
    # the seat working rather than an empty board.
    assert "that refusal is the seat working" in text, text[:300]
    assert "that refusal is the seat working" not in INSTRUCTION, \
        "the phrase this test keys on is in BOTH templates, so it distinguishes nothing"


# --- GRPH-1012: the base is the fetched one, whichever of the three rules named it --------


def _sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True).stdout.strip()


def test_a_measured_project_base_cuts_children_from_that_branch(
    tmp_path: Path, scripts, state: Path,
):
    """Rule 2, end to end through `run` with no `--base` anywhere in sight.

    The project has measured its trunk, so the wave cuts from it — and says which rule
    answered. That second half is the point: on a repository whose remote default is `main`,
    a child built on `origin/integration` is either a decision the project made or a bug, and
    nothing else in the output distinguishes them.

    Sabotage: skip the gitops step in `resolve_wave_base`, or hand `_loop` a base resolved
    somewhere else → the child's base is main and `source` reads `remote_default`.
    """
    repo = _repo_with_remote(tmp_path)
    workspace = tmp_path / "ws"
    integration_sha = _sh(repo, "rev-parse", "origin/integration")
    main_sha = _sh(repo, "rev-parse", "origin/main")
    assert integration_sha != main_sha, "the fixture has to make the two rules differ"

    planner, supervisor = _clients(
        workspace, clusters=1, workers=0,
        gitops={"base_branch": {"value": "integration", "source": "project"}},
    )
    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert result.spawned == 1, result.detail
    assert result.wave.base is not None
    assert result.wave.base.source == "gitops"
    assert result.wave.base.ref == "origin/integration"
    assert result.wave.spawned[0].base == integration_sha


def test_an_unmeasured_project_base_falls_to_the_remote_default_and_says_it_was_unmeasured(
    tmp_path: Path, scripts, state: Path,
):
    """Rule 3, on the shape this project actually has: `base_branch` null, source
    `unmeasured`, so the base is the remote's own default ref, fetched.

    The wave that carried this item was cut exactly that way, and the reading it must not
    invite is the defect: "the project's base is main" is a claim nobody made. The ref and
    the note are asserted together because either one alone is the wrong answer — the ref
    without the note reads as a project rule, and the note without the ref reads as a wave
    that could not start.

    Sabotage: cut from the checkout HEAD instead, or drop the gitops half of the note → this
    fails.
    """
    repo = _repo_with_remote(tmp_path)
    # `git init` + `remote add` never writes `refs/remotes/origin/HEAD`, and `default_ref`
    # reads that symref rather than guessing a trunk name. A clone has one; this fixture is
    # not a clone, so the test writes the one a clone would have arrived with.
    _sh(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    # Stand the checkout somewhere that is NOT the base, so "cut from HEAD" and "cut from
    # the fetched default ref" are different commits and the assertion can tell them apart.
    _sh(repo, "checkout", "-q", "-b", "feature")
    (repo / "wip.txt").write_text("wip\n", encoding="utf-8")
    _sh(repo, "add", "-A")
    _sh(repo, "commit", "-qm", "work nobody merged")
    workspace = tmp_path / "ws"
    main_sha = _sh(repo, "rev-parse", "origin/main")
    head_sha = _sh(repo, "rev-parse", "HEAD")

    planner, supervisor = _clients(workspace, clusters=1, workers=0)
    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert result.spawned == 1, result.detail
    assert result.wave.base.source == "remote_default"
    assert result.wave.base.ref == "origin/main"
    assert "unmeasured" in result.wave.base.note
    assert result.wave.spawned[0].base == main_sha != head_sha

    import io

    from gbfleet.until import emit

    out = io.StringIO()
    emit(result, out=out)
    printed = out.getvalue()
    assert "BASE origin/main" in printed
    assert "[remote_default]" in printed
    assert "unmeasured" in printed, "the summary must keep saying no project rule named it"


class _SizingStub:
    """`planner`/`supervisor` for `_wanted_workers` alone: one dict per tool."""

    def __init__(self, replies: dict):
        self.replies = replies

    def call(self, tool: str, **_args):
        return self.replies[tool]


def _sizing(*, free: int, live: int, workers: int | None = None) -> dict:
    agents = [{"id": f"GRPH-A{i}"} for i in range(live)]
    n = live if workers is None else workers
    return {
        "collision_clusters": {"clusters": [{"items": [f"GRPH-{i}"]} for i in range(free)]},
        "fleet_status": {"agents": agents},
        "propose_allocation": {"workers": n, "reviewers": 0, "rationale": "stub",
                               "mapping": [{"agent_id": a["id"]} for a in agents]},
    }


def test_a_live_child_does_not_hide_a_free_cluster():
    """GRPH-1189. `collision_clusters` already leaves out what a running child claimed, so
    the one free cluster here is work nobody has. Subtracting the live child from it as well
    returned 0, and the cluster sat unclaimed with two slots spare. The roster holds only the
    live child — this loop's planner never heartbeats — so `propose_allocation` says one
    worker, and that is the roster, not a bound."""
    from gbfleet.until import _wanted_workers

    stub = _SizingStub(_sizing(free=1, live=1))
    assert _wanted_workers(stub, stub, live_n=1, max_workers=3) == 1


def test_a_live_child_with_no_slot_left_spawns_nothing():
    from gbfleet.until import _wanted_workers

    stub = _SizingStub(_sizing(free=1, live=1, workers=3))
    assert _wanted_workers(stub, stub, live_n=1, max_workers=1) == 0


def test_live_children_and_no_free_cluster_spawn_nothing():
    """Ready work still bounds everything (the PRD-39 runaway): the roster's two live
    children and a proposal of three workers are not work."""
    from gbfleet.until import _wanted_workers

    stub = _SizingStub(_sizing(free=0, live=2, workers=3))
    assert _wanted_workers(stub, stub, live_n=2, max_workers=3) == 0


def test_free_clusters_fill_the_slots_live_children_leave():
    from gbfleet.until import _wanted_workers

    stub = _SizingStub(_sizing(free=5, live=1))
    assert _wanted_workers(stub, stub, live_n=1, max_workers=3) == 2


def test_the_loop_spawns_beside_a_live_child_for_a_free_cluster(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1189, end to end: with one child still running and one cluster nobody holds,
    the loop starts a second child rather than waiting out the first. Two clusters; the
    first child's leaves the read, as on the real server, so one free cluster remains.
    Spawning twice is not enough — the bug spawned the second once the first was reaped —
    so the second launch must land inside the first child's wall clock."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=2, workers=1, sticky_clusters=True,
                                   drop_delegated=True)
    inner = _factory(scripts, "sleeper")
    # GRPH-1715: ordering, not a wall-clock window. Each launch records how many children
    # were alive at that moment; the second must see the first still running.
    alive_at_launch: list[int] = []

    def factory(*args, **kwargs):
        alive_at_launch.append(len(_running_pids(git_repo, state)))
        return inner(*args, **kwargs)

    wall = 4.0
    result = run(
        git_repo, factory,
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0.05, empty_ticks=3,
        limits=Limits(max_workers=3, max_children=2, child_wall_clock=wall),
    )
    assert result.spawned == 2, result.detail
    assert alive_at_launch[1] >= 1, "the second child waited out the first"


def _staffing_run(git_repo, tmp_path, scripts, state, *, staff_every_row):
    workspace = tmp_path / "ws"
    mints: list[dict] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "", "prd_id": "SA-P99"}],
        minted_args=mints, staff_every_row=staff_every_row,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        prd="SA-P11",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    return result, mints


def test_staff_every_row_on_staffs_an_unheld_row_outside_prd(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1273. With `review.staff_every_row` on, an unheld row of another PRD is staffed
    by a review-only seat that is not scoped to `--prd`.

    Sabotage: ignore the toggle in `_staff_every_row` and no seat is minted."""
    result, mints = _staffing_run(git_repo, tmp_path, scripts, state, staff_every_row=True)
    assert mints and mints[0].get("review_only") is True, result.as_json()
    assert not mints[0].get("scope"), mints
    assert result.spawned >= 1
    assert "GRPH-9" in result.review
    assert result.as_json()["review_outside_prd"] == []


def test_staff_every_row_off_leaves_an_outside_row_unstaffed(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1273, the other state: toggle off (or unknown to the server) is GRPH-1188."""
    for state_ in (False, None):
        result, mints = _staffing_run(git_repo, tmp_path / str(state_), scripts,
                                      state.parent / f"s{state_}", staff_every_row=state_)
        assert mints == [] and result.spawned == 0, result.as_json()
        assert result.reason == "idle"
        assert result.as_json()["review_outside_prd"] == ["GRPH-9"]


def test_staff_every_row_still_leaves_a_held_row_alone(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1273: only rows no live agent holds (GRPH-1254 lapse semantics) are staffed."""
    workspace = tmp_path / "ws"
    mints: list[dict] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "GRPH-R1", "prd_id": "SA-P99"}],
        minted_args=mints, staff_every_row=True,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        prd="SA-P11",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert mints == [] and result.spawned == 0, result.as_json()


def test_review_waiting_on_the_gate_is_an_entry_logged_once():
    """GRPH-1349: the wait is an entry, not silence. GRPH-1387: a live builder of another
    item does not block a review spawn (GRPH-1374), so only the gate is named, not the child."""
    from gbfleet.supervisor import Wave
    from gbfleet.until import _review_waits_on_live

    class _P:
        pid = 777

    class _C:
        held_items = ["GRPH-1343"]
        process = _P()

    wave = Wave()
    rows = [{"id": "GRPH-1342"}]
    assert _review_waits_on_live(wave, rows, [_C()]) == []
    got = _review_waits_on_live(wave, rows, [_C()], gate="no room for another child")
    assert got == ["GRPH-1342: review waits on the memory gate (no room for another child)"]
    _review_waits_on_live(wave, rows, [_C()], gate="no room for another child")
    assert wave.review_waits == got


def test_a_live_builder_does_not_block_a_review_spawn_for_another_item(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1374: a child building item A is live; an unheld review row for B still gets a
    reviewer within the run. Sabotage: restore `not live` on the review branch and no
    review-only seat is ever minted."""
    workspace = tmp_path / "ws"
    minted: list = []
    builder_exited_at_mint: list = []
    here = scripts["works_until_released"].parent

    def on_mint(role: str) -> None:
        # Record whether the builder had already gone, then let it go once the reviewer
        # exists. Ordering, not elapsed time (GRPH-1536).
        builder_exited_at_mint.append((here / "builder-exited").exists())
        if len(builder_exited_at_mint) >= 2:
            (here / "release").write_text("", encoding="utf-8")

    planner, supervisor = _clients(
        workspace, clusters=1, workers=1, on_mint=on_mint,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1", "review_claimed_by": ""}],
        minted_args=minted,
    )
    run(
        git_repo, _factory(scripts, "works_until_released"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, max_children=4),
    )
    assert [a.get("review_only") for a in minted[:2]] == [False, True], minted
    # The builder runs until the reviewer's mint releases it, so the reviewer was minted
    # while the builder was still live.
    assert builder_exited_at_mint[:2] == [False, False], builder_exited_at_mint


def test_review_waits_and_lapsed_live_reach_the_summary_json():
    """GRPH-1374: both lists are in the JSON when non-empty and absent when empty."""
    from gbfleet.supervisor import Wave
    from gbfleet.until import Report

    wave = Wave()
    assert "review_waits" not in Report(ok=True, reason="idle", exit=0, wave=wave).as_json()
    wave.review_waits.append("GRPH-1342: review waits on live child pid 7 (item GRPH-1343)")
    wave.lease_lapsed_live.append("GRPH-1343: lease lapsed under live pid 7")
    got = Report(ok=True, reason="idle", exit=0, wave=wave).as_json()
    assert got["review_waits"] == wave.review_waits
    assert got["lease_lapsed_live"] == wave.lease_lapsed_live


def test_review_wait_names_an_unclaimed_reviewer_the_gate_and_never_the_rows_own_child():
    """GRPH-1387."""
    from gbfleet.supervisor import Wave
    from gbfleet.until import _review_waits_on_live

    class _P:
        pid = 49673

    class _C:
        def __init__(self, held, ever=(), assigned=None):
            self.held_items = list(held)
            self.ever_held = list(ever)
            self.assigned = assigned
            self.process = _P()

    rows = [{"id": "GRPH-1374"}]

    # Our own review spawn, not yet claimed: named as that, never as a block by a live child.
    reviewer = _C([])
    got = _review_waits_on_live(Wave(), rows, [reviewer], review_spawned=[reviewer])
    assert got == ["GRPH-1374: review waits on reviewer pid 49673 not yet claimed"]

    # The memory gate refused: the gate is named, not the unrelated live child.
    other = _C(["GRPH-9"])
    got = _review_waits_on_live(Wave(), rows, [other], gate="memory low")
    assert got == ["GRPH-1374: review waits on the memory gate (memory low)"]

    # The row's own reviewer (holding it) and its finishing builder (held it, or was bound
    # to it, now holding nothing) are never what the row waits on.
    own_reviewer = _C(["GRPH-1374"])
    assert _review_waits_on_live(Wave(), rows, [own_reviewer],
                                 review_spawned=[own_reviewer]) == []
    builder = _C([], ever=["GRPH-1374"])
    assert _review_waits_on_live(Wave(), rows, [builder]) == []
    bound = _C([], assigned={"state": "claimed", "item": "GRPH-1374"})
    assert _review_waits_on_live(Wave(), rows, [bound], review_spawned=[bound]) == []
    for line in _review_waits_on_live(Wave(), rows, [builder, own_reviewer],
                                      gate="memory low"):
        assert "pid" not in line


# ---- GRPH-1401: a live child that holds nothing is not a lease -----------------------


def _running_pids(repo: Path, state: Path) -> list[int]:
    # `hostos.pid_is_alive`, not `os.kill(pid, 0)`: on Windows signal 0 is
    # TerminateProcess with exit code 0, so the probe itself killed the sleeper
    # the lease tests need alive. A zombie (POSIX) has exited; it is not running.
    from gbfleet import adopt as adopt_mod
    from gbfleet import hostos

    loaded = adopt_mod.load(adopt_mod.children_path(repo, state))
    if not isinstance(loaded, list):
        return []
    return [
        snap.pid for snap in loaded
        if hostos.pid_is_alive(snap.pid) and not hostos.pid_is_zombie(snap.pid)
    ]


def _end_pid(pid: int) -> None:
    """Stop one sleeper from the test side. Windows has no SIGKILL; there
    `os.kill(pid, SIGTERM)` is already TerminateProcess."""
    import os
    import signal

    from gbfleet import hostos

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(50):
        if not hostos.pid_is_alive(pid) or hostos.pid_is_zombie(pid):
            return
        time.sleep(0.02)
    hard = getattr(signal, "SIGKILL", None)
    if hard is not None:
        try:
            os.kill(pid, hard)
        except OSError:
            pass


def _empty_child_run(
    git_repo, tmp_path, scripts, state, *,
    review=None, waits=None, holdings=None, release_after=None,
    claim_after=None, claim_window=0.0,
):
    """One sleeper, then no more ready work. `release_after` kills it once the
    wave has polled past `empty_ticks` while it was still alive — the lease
    tests, which must not return on their own.

    `claim_window` defaults to 0: the child has been empty longer than its claim
    window (GRPH-P72). `None` keeps `CLAIM_WINDOW`. `claim_after` puts `holdings`
    on the roster only after that many live polls: a child that claims late."""
    import gbfleet.until as until_mod

    workspace = tmp_path / "ws"
    roster_holdings: list = [] if claim_after is not None else list(holdings or [])
    planner, supervisor = _clients(
        workspace, clusters=1, workers=0, review=review, waits=waits,
        agent_holdings=roster_holdings,
    )
    calls: list[int] = []
    seen = {"past_idle": False, "spins": 0}

    def sleep(_):
        # Count only polls where a child is already alive. Registration and the
        # spawn itself do not use this sleep; a lease must survive empty_ticks
        # of these, and an empty child must not.
        seen["spins"] += 1
        if seen["spins"] > 200:
            raise AssertionError(
                "the wave did not settle; a roster row still looked like a lease"
            )
        if release_after is not None and not _running_pids(git_repo, state):
            return
        calls.append(1)
        if claim_after is not None and len(calls) == claim_after:
            seen["claimed_at_poll"] = len(calls)
            roster_holdings.extend(holdings or [])
            # What the server does on claim_review: the row now names its reviewer.
            claimed = {h.get("id") for h in holdings or [] if h.get("hold") == "review"}
            for row in review or []:
                if row.get("id") in claimed:
                    row["review_claimed_by"] = "GRPH-A1"
        if release_after is None:
            if len(calls) > 20:
                raise AssertionError(
                    "the wave did not go idle; a live child holding nothing reset empty"
                )
            return
        if len(calls) <= release_after:
            return
        seen["past_idle"] = True
        for pid in _running_pids(git_repo, state):
            _end_pid(pid)

    saved = until_mod.CLAIM_WINDOW
    if claim_window is not None:
        until_mod.CLAIM_WINDOW = claim_window
    try:
        result = run(
            git_repo, _factory(scripts, "sleeper"),
            planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
            state=state, workspace=workspace, poll=0, sleep=sleep, empty_ticks=3,
            limits=Limits(max_workers=1, child_wall_clock=3600),
        )
    finally:
        until_mod.CLAIM_WINDOW = saved
    return result, seen


def _assert_children_reaped(result) -> None:
    kids = list(result.wave.spawned)
    assert kids, result.as_json()
    assert all(not child.running for child in kids), [c.pid for c in kids if c.running]
    assert all(child.reaped for child in kids)
    reaped = {row.branch for row in result.wave.reaped}
    assert {child.branch for child in kids} <= reaped


def test_a_live_child_holding_nothing_goes_idle_and_is_reaped(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1401. The idle rule is a lease, not a process. Sabotage: put `live`
    back into the idle check (`if live or busy`) and this raises instead of
    returning idle — the child keeps resetting `empty`."""
    result, _seen = _empty_child_run(git_repo, tmp_path, scripts, state)
    assert result.reason == "idle", result.as_json()
    assert result.exit == 0
    assert result.ok is True
    _assert_children_reaped(result)


def test_a_live_child_holding_nothing_with_a_wait_is_idle_with_waits(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    result, _seen = _empty_child_run(
        git_repo, tmp_path, scripts, state,
        waits=[{"id": "GRPH-W1", "tags": ["wait:merge"], "status": "blocked"}],
    )
    assert result.reason == "idle-with-waits", result.as_json()
    assert result.exit == 0
    assert result.waits == ["GRPH-W1"]
    _assert_children_reaped(result)


def test_a_live_child_holding_nothing_with_unsigned_review_is_review_unsigned(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    result, _seen = _empty_child_run(
        git_repo, tmp_path, scripts, state,
        review=[{"id": "GRPH-9", "status": "review", "review_claimed_by": ""}],
    )
    assert result.reason == "review-unsigned", result.as_json()
    assert result.exit == 1
    assert "GRPH-9" in result.review
    _assert_children_reaped(result)


def test_a_live_child_holding_a_build_lease_does_not_go_idle(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """The lease keeps `empty` at 0, and the child is still running past
    `empty_ticks`. The test ends the process itself so `run` can return."""
    result, seen = _empty_child_run(
        git_repo, tmp_path, scripts, state,
        holdings=[{"id": "GRPH-1", "phase": "building", "hold": "build"}],
        release_after=3,
    )
    assert seen["past_idle"] is True
    assert result.reason == "idle", result.as_json()
    kids = list(result.wave.spawned)
    assert kids
    assert all(child.stopped_because is None for child in kids), [
        c.stopped_because for c in kids
    ]


def test_a_live_child_holding_a_review_claim_does_not_go_idle(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1001 kept a review claim out of `_any_holdings`. The process that
    holds it is still the work. An empty holdings list is the case that is not."""
    result, seen = _empty_child_run(
        git_repo, tmp_path, scripts, state,
        holdings=[{"id": "GRPH-9", "phase": "building", "hold": "review"}],
        release_after=3,
    )
    assert seen["past_idle"] is True
    assert result.reason == "idle", result.as_json()
    assert all(child.stopped_because is None for child in result.wave.spawned)


class _UnreachableRoster:
    def call(self, tool, **_args):
        from gbfleet.client import ServerUnreachable
        raise ServerUnreachable("no route to host")


class _Live:
    running = True
    pid = 4242
    stopped_because = None


def test_an_unreadable_roster_is_not_an_empty_one():
    """GRPH-1401. fleet_status unreachable: a live child may hold an item or a
    review claim, so it blocks idle and nothing is stopped."""
    from gbfleet.supervisor import Wave as _Wave
    from gbfleet.until import _idle_blocked, _roster_agents, _stop_children_holding_nothing

    sup = _UnreachableRoster()
    child = _Live()
    assert _roster_agents(sup) is None
    assert _idle_blocked(sup, [child]) is True
    assert _idle_blocked(sup, []) is False
    _stop_children_holding_nothing(_Wave(), [child], sup, sup, "main")
    assert child.running is True and child.stopped_because is None


class _Roster:
    def __init__(self, agents):
        self.agents = agents

    def call(self, tool, **_args):
        assert tool == "fleet_status"
        return {"agents": self.agents}


class _Held:
    """A fake live child; `stop` is patched, so only these fields are read."""

    def __init__(self, agent_id, worktree):
        self.agent_id = agent_id
        self.worktree = worktree
        self.running = True
        self.stopped_because = None


def test_a_review_spawn_that_claims_late_is_not_stopped_and_is_waited_for(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1401 bounce. A child that registered and has not claimed yet is inside
    `CLAIM_WINDOW`: it claims only after more than `empty_ticks` polls, and the run
    neither returns nor stops it before then. Sabotage: drop `_in_claim_window` from
    `_idle_blocked` and the run returns review-unsigned at poll 3, stopping the child."""
    result, seen = _empty_child_run(
        git_repo, tmp_path, scripts, state,
        review=[{"id": "GRPH-9", "status": "review", "review_claimed_by": ""}],
        holdings=[{"id": "GRPH-9", "phase": "building", "hold": "review"}],
        claim_after=6, release_after=9, claim_window=None,
    )
    assert seen.get("claimed_at_poll") == 6, seen
    assert seen["past_idle"] is True, (seen, result.as_json())
    kids = list(result.wave.spawned)
    assert kids
    assert all(child.stopped_because is None for child in kids), [
        c.stopped_because for c in kids
    ]


def test_a_live_child_whose_only_holding_is_stale_does_not_block_idle():
    """GRPH-1401 second review. A stale phase is a dead lease, on the roster's build
    holdings and on this child's own row. Sabotage: drop the stale skip in
    `_fresh_holdings` and this child blocks idle."""
    from gbfleet.until import _idle_blocked

    child = _Held("GRPH-A2", "/tmp/stale-child")
    sup = _Roster([{"id": "GRPH-A2", "worktree": child.worktree,
                    "holdings": [{"id": "GRPH-9", "phase": "stale", "hold": "review"}]}])
    assert _idle_blocked(sup, [child]) is False
    sup.agents[0]["holdings"][0]["phase"] = "building"
    assert _idle_blocked(sup, [child]) is True


def test_the_claim_window_is_bounded_and_ends_on_a_claim():
    """Registered moments ago and never held: busy. Past `CLAIM_WINDOW`: not. A child
    that has held something is never in it, and a hand-built one has no start time."""
    import gbfleet.until as until_mod

    class _C:
        started_at = 1000.0
        registration_latency = 20.0
        held_items: list = []
        review_items: list = []
        ever_held: list = []

    child = _C()
    window = until_mod.CLAIM_WINDOW
    assert until_mod._in_claim_window(child, now=1020.0 + window - 1) is True
    assert until_mod._in_claim_window(child, now=1020.0 + window) is False
    child.ever_held = ["GRPH-1"]
    assert until_mod._in_claim_window(child, now=1021.0) is False
    child.ever_held = []
    child.review_items = ["GRPH-9"]
    assert until_mod._in_claim_window(child, now=1021.0) is False
    assert until_mod._in_claim_window(_Live(), now=0.0) is False


def test_only_the_child_holding_nothing_is_stopped_at_exit(monkeypatch, tmp_path: Path):
    """GRPH-1401 bounce. Readable roster: a live child holding a non-stale item must
    survive `_leave` (the REVIEWER_FAILS exit runs it while builders still hold
    leases); only the live child that holds nothing is stopped and reaped."""
    import gbfleet.until as until_mod
    from gbfleet.supervisor import Wave as _Wave

    builder = _Held("GRPH-A1", str(tmp_path / "builder"))
    empty = _Held("GRPH-A2", str(tmp_path / "empty"))
    sup = _Roster([
        {"id": "GRPH-A1", "worktree": builder.worktree,
         "holdings": [{"id": "GRPH-7", "phase": "build"}]},
        {"id": "GRPH-A2", "worktree": empty.worktree, "holdings": []},
    ])
    stopped, reaped = [], []

    def fake_stop(child, reason, grace=0):
        stopped.append(child)
        child.stopped_because = reason
        child.running = False

    monkeypatch.setattr(until_mod, "stop", fake_stop)
    monkeypatch.setattr(until_mod, "_report_exits", lambda gone, *_a, **_k: None)
    monkeypatch.setattr(until_mod, "_reap_all",
                        lambda _w, gone, **_k: reaped.extend(gone))

    assert until_mod._idle_blocked(sup, [builder, empty]) is True
    until_mod._stop_children_holding_nothing(_Wave(), [builder, empty], sup, sup, "main")

    assert stopped == [empty] and reaped == [empty]
    assert empty.running is False
    assert builder.running is True and builder.stopped_because is None


@pytest.mark.parametrize("window, kept", [(None, True), (0.0, False)])
def test_leave_keeps_a_child_inside_its_claim_window(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch, window, kept,
):
    """GRPH-1401 third bounce. The run exits through `_leave` (the REVIEWER_FAILS
    exit, forced to the first unheld-review poll) while the builder it spawned a
    poll earlier has registered, holds nothing, and has never held anything. Inside
    `CLAIM_WINDOW` it is not stopped and is still running when `run` returns; with
    the window at 0 it is stopped and reaped. Sabotage: drop `and not
    _in_claim_window(c)` from `_stop_children_holding_nothing` and the first case
    stops it."""
    import gbfleet.until as until_mod

    monkeypatch.setattr(until_mod, "REVIEWER_FAILS", 0)
    # The memory gate reads this host; it is not what this test is about.
    monkeypatch.setattr(until_mod, "_room_refusal", lambda *_a, **_k: None)
    if window is not None:
        monkeypatch.setattr(until_mod, "CLAIM_WINDOW", window)
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace, clusters=1, workers=0,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": ""}],
        agent_holdings=[],
    )
    result = run(
        git_repo, _factory(scripts, "sleeper"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1, child_wall_clock=3600),
    )
    try:
        assert result.reason == "review-unsigned", result.as_json()
        kids = list(result.wave.spawned)
        assert len(kids) == 1, result.as_json()
        child = kids[0]
        if kept:
            assert child.stopped_because is None, child.stopped_because
            assert child.running is True
            assert child.pid in _running_pids(git_repo, state)
            assert not child.reaped
        else:
            assert child.stopped_because is not None
            assert child.running is False
            _assert_children_reaped(result)
    finally:
        for pid in _running_pids(git_repo, state):
            _end_pid(pid)


_CI_STUCK = {"kind": "note", "detail": "refused at `done` (pr_ci_not_green): cooldown over, "
             "no suite_green at commit 5f4e0ecc: run x"}


def test_a_row_refused_pr_ci_not_green_past_cooldown_gets_no_reviewer(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """GRPH-1409: another reviewer cannot clear it, so none is spawned and the run says why."""
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "", "evidence": [_CI_STUCK]}],
        minted_roles=roles,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=3,
        limits=Limits(max_workers=1),
    )
    assert roles == []
    assert result.reason == "review-ci-unattestable"
    assert "GRPH-9" in result.detail and "5f4e0ecc" in result.detail
    assert "CI attestation or an owner close" in result.detail


def test_ci_failure_refusal_still_gets_a_reviewer(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """A red run is a bounce a reviewer can make, so only the silent-CI refusal is skipped."""
    from gbfleet.until import ci_unattestable
    red = {"kind": "note", "detail": "refused at `done` (pr_ci_not_green): x did not pass"}
    assert ci_unattestable({"evidence": [red]}) is False
    assert ci_unattestable({"evidence": [_CI_STUCK]}) is True
    # A newer, different refusal supersedes it.
    other = {"kind": "note", "detail": "refused at `done` (adversarial): nope"}
    assert ci_unattestable({"evidence": [_CI_STUCK, other]}) is False


def test_a_suite_green_attestation_after_the_refusal_clears_the_stuck_row():
    """GRPH-1409 bounce: CI attesting after the refusal lets a reviewer sign it off again."""
    from gbfleet.until import ci_unattestable
    green = {"kind": "attestation", "commit": "5f4e0ecc",
             "predicates": [{"name": "suite_green", "passed": True}]}
    assert ci_unattestable({"evidence": [_CI_STUCK, green]}) is False
    # A red attestation is a bounce a reviewer can make, so it also needs a reviewer.
    red = {"kind": "attestation", "predicates": [{"name": "suite_green", "passed": False}]}
    assert ci_unattestable({"evidence": [_CI_STUCK, red]}) is False
    # An attestation OLDER than the refusal does not clear it.
    assert ci_unattestable({"evidence": [green, _CI_STUCK]}) is True
    # A scans-only attestation says nothing about suite_green.
    scans = {"kind": "attestation", "predicates": [{"name": "scans_clean", "passed": True}]}
    assert ci_unattestable({"evidence": [_CI_STUCK, scans]}) is True


def test_a_review_unsigned_exit_names_the_ci_stuck_rows_beside_it():
    """GRPH-1409 bounce: in the mixed case the stuck rows are still named in the detail."""
    from gbfleet.until import Report, _name_ci_stuck
    row = {"id": "GRPH-9", "evidence": [_CI_STUCK]}
    report = _name_ci_stuck(Report(ok=False, reason="review-unsigned", exit=1), [row])
    assert "GRPH-9" in report.detail and "owner close" in report.detail
    assert _name_ci_stuck(Report(ok=False, reason="review-unsigned", exit=1), []).detail == ""



_CI_RUNNING = {"kind": "note", "detail": "refused at `done` (pr_ci_in_progress): waiting on CI "
               "run https://x/runs/42/attempts/1, in progress at 89a0225d2552"}


def test_the_in_progress_refusal_is_not_read_as_ci_unattestable():
    """GRPH-1416: the server's in-progress refusal is its own wording, so until never takes it
    for a dead-end `pr_ci_not_green` and ends the wave on it."""
    from gbfleet.until import ci_unattestable
    assert ci_unattestable({"id": "GRPH-9", "evidence": [_CI_RUNNING]}) is False
    assert ci_unattestable({"id": "GRPH-9", "evidence": [_CI_STUCK]}) is True


def test_a_row_whose_ci_run_is_in_progress_gets_no_reviewer_and_waits(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """GRPH-1416: a row the server refused `pr_ci_in_progress`, whose CI is still running,
    gets no reviewer; until waits (the one CI wait, GRPH-1392's) and does not exit
    `review-unsigned` because of it."""
    from gbfleet import until
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("pending", "89a0225d2552", ""))
    workspace = tmp_path / "ws"
    roles: list[str] = []
    planner, supervisor = _clients(
        workspace,
        review=[{"id": "GRPH-9", "status": "review", "claimed_by": "GRPH-A1",
                 "review_claimed_by": "", "branch": "gb/x", "evidence": [_CI_RUNNING]}],
        minted_roles=roles,
    )
    ticks: list[int] = []

    class Stop(Exception):
        pass

    def sleep(_):
        ticks.append(1)
        if len(ticks) > 12:
            raise Stop

    with pytest.raises(Stop):
        run(
            git_repo, _factory(scripts, "works_then_exits"),
            planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
            state=state, workspace=workspace, poll=0, sleep=sleep, empty_ticks=3,
            limits=Limits(max_workers=1),
        )
    assert roles == []
    assert len(ticks) > 3  # waited past empty_ticks instead of exiting


# ---- GRPH-1770: the review gate reads the recorded PR's head, not only item.branch ----


_PR_URL = "https://github.com/asc-me/graphban/pull/1878"


class _CiPlanner:
    def call(self, tool, /, **args):
        return {}


def _sha_from(repo: Path, branch: str, parent: str) -> str:
    run = lambda *a: subprocess.run(
        ["git", *a], cwd=repo, check=True, capture_output=True, text=True)
    run("checkout", "-q", "-B", branch, parent)
    (repo / branch.replace("/", "_")).write_text(branch + "\n")
    run("add", "-A")
    run("commit", "-qm", branch)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         check=True, capture_output=True, text=True).stdout.strip()
    run("checkout", "-q", "main")
    return sha


def _pr_row(branch: str) -> dict:
    return {
        "id": "GRPH-1715",
        "status": "review",
        "branch": branch,
        "pr": _PR_URL,
        "evidence": [{"kind": "url", "url": _PR_URL,
                      "detail": "draft PR opened by gbfleet for gb/line-12"}],
    }


def test_a_green_pr_head_that_descends_from_a_red_item_branch_is_reviewable(
    tmp_path, monkeypatch,
):
    """GRPH-1770. item.branch is red. The recorded PR is on another branch, green, and
    that head descends from item.branch. The review gate reads the PR head, so the row
    is reviewable.

    Sabotage: pass `row['branch']` to `ci_verdict` again and this fails — the row is
    bounced for the red branch instead of left ready.
    """
    from gbfleet import propose as propose_mod
    from gbfleet.until import _ci_attested

    repo = _git_repo(tmp_path)
    red = _sha_from(repo, "gb/line-17", "main")
    green = _sha_from(repo, "gb/line-12", "gb/line-17")
    asked: list[str] = []

    def verdict(_repo, branch):
        asked.append(branch)
        if branch == "gb/line-12":
            return ("green", green, "")
        if branch == "gb/line-17":
            return ("red", red, "backend-postgres")
        return ("unknown", "", "")

    events: list[tuple] = []
    monkeypatch.setattr(propose_mod, "view", lambda *_a, **_k: (
        {"headRefName": "gb/line-12", "headRefOid": green}, ""))
    monkeypatch.setattr(until, "ci_verdict", verdict)
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: events.append((ev, kw)))
    row = _pr_row("gb/line-17")
    ready, waiting, blocked = _ci_attested(
        [row], repo, set(), planner=_CiPlanner(), agent_id="GRPH-P1")
    assert ready == [row] and waiting == [] and blocked == []
    assert asked == ["gb/line-12"]
    assert not any(ev == "review_branch_mismatch" for ev, _ in events)


def test_a_pr_head_that_does_not_descend_is_named_not_skipped(tmp_path, monkeypatch):
    """The other half of GRPH-1770. The PR's branch is not a descendant of item.branch,
    so the gate does not treat that green head as this item's CI. It emits one
    `review_branch_mismatch` naming both branches and both heads, and still judges
    item.branch.

    Sabotage: drop `_note_branch_mismatch` and this fails — the row is bounced with
    no event, which is the silent skip.
    """
    from gbfleet import propose as propose_mod
    from gbfleet.until import _ci_attested

    repo = _git_repo(tmp_path)
    red = _sha_from(repo, "gb/line-17", "main")
    other = _sha_from(repo, "gb/line-12", "main")
    asked: list[str] = []

    def verdict(_repo, branch):
        asked.append(branch)
        if branch == "gb/line-12":
            return ("green", other, "")
        return ("red", red, "backend-postgres")

    events: list[tuple] = []
    monkeypatch.setattr(propose_mod, "view", lambda *_a, **_k: (
        {"headRefName": "gb/line-12", "headRefOid": other}, ""))
    monkeypatch.setattr(until, "ci_verdict", verdict)
    monkeypatch.setattr(until, "failed_test_summary", lambda *_a: "")
    monkeypatch.setattr(until, "transport_flake_jobs", lambda *_a: [])
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: events.append((ev, kw)))
    row = _pr_row("gb/line-17")
    ready, _waiting, blocked = _ci_attested(
        [row], repo, set(), planner=_CiPlanner(), agent_id="GRPH-P1")
    assert ready == [] and blocked == [row]
    assert asked == ["gb/line-17"]
    mismatches = [kw for ev, kw in events if ev == "review_branch_mismatch"]
    assert len(mismatches) == 1
    kw = mismatches[0]
    assert kw["branch"] == "gb/line-17" and kw["pr_branch"] == "gb/line-12"
    assert kw["commit"].startswith(red[:12]) and kw["pr_head"].startswith(other[:12])
    assert "gb/line-17" in kw["detail"] and "gb/line-12" in kw["detail"]
    assert red[:12] in kw["detail"] and other[:12] in kw["detail"]
    # Once per pair of heads, not once per poll.
    again, _, _ = _ci_attested([row], repo, {("review_branch_mismatch", "GRPH-1715",
                                              f"gb/line-17@{red}|gb/line-12@{other}")},
                               planner=_CiPlanner(), agent_id="GRPH-P1")
    assert again == []
    assert len([ev for ev, _ in events if ev == "review_branch_mismatch"]) == 1


def test_the_reviewer_ci_check_reads_a_run_at_the_pr_head(tmp_path, monkeypatch):
    """GRPH-1770. The reviewer's CI check (`ci_run_in_progress`) lists runs on the PR
    branch and counts a run at the PR head, not only the commit the refusal named on
    item.branch.

    Sabotage: list `item.branch` again and this fails — the in-progress run is on the
    PR branch, so the check answers "" and the drain does not wait.
    """
    from gbfleet import propose as propose_mod
    from gbfleet.until import ci_run_in_progress

    repo = _git_repo(tmp_path)
    red = _sha_from(repo, "gb/line-17", "main")
    green = _sha_from(repo, "gb/line-12", "gb/line-17")
    asked: list[str] = []

    real_run = subprocess.run

    def fake_run(cmd, **k):
        # review_ci_branch resolves the PR head with git before the gh run list.
        if not cmd or cmd[0] != "gh":
            return real_run(cmd, **k)
        branch = cmd[cmd.index("--branch") + 1]
        asked.append(branch)
        payload = []
        if branch == "gb/line-12":
            payload = [{"databaseId": 9, "status": "in_progress", "name": "CI",
                        "headSha": green}]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(propose_mod, "view", lambda *_a, **_k: (
        {"headRefName": "gb/line-12", "headRefOid": green}, ""))
    monkeypatch.setattr(until.subprocess, "run", fake_run)
    row = _pr_row("gb/line-17")
    row["evidence"] = [
        {"kind": "note", "detail": "refused at `done` (pr_ci_not_green): cooldown over, "
         f"no suite_green at commit {red}"},
        row["evidence"][0],
    ]
    assert "9" in ci_run_in_progress(repo, row, set())
    assert asked == ["gb/line-12"]


def test_negative_subagents_exits_before_any_seat(monkeypatch, capsys):
    from gbfleet.cli import main
    monkeypatch.setenv("GBFLEET_API_KEY", "k")
    assert main(["until", "--server", "http://x", "--project", "p", "--adapter", "claude", "--subagents", "-1"]) == 2
    assert "--subagents must be 0 or more" in capsys.readouterr().err


# ---- GRPH-1785: a stale worktree holding the bounced branch ---------------------------


def _clone_with_held_branch(tmp_path: Path, *, dirty: bool) -> tuple[Path, Path]:
    """A clone whose `gb/line-104` is checked out in a linked worktree.

    The branch is pushed and matches `origin`. `gb/line-44` exists at `main` and
    is not checked out: that is the unrelated branch a failed resume started a
    child on. Returns `(repo, stale_worktree)`.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args: str, cwd: Path = repo) -> None:
        subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    g("remote", "add", "origin", str(bare))
    (repo / "README.md").write_text("x\n")
    g("add", "-A")
    g("commit", "-qm", "first")
    g("push", "-q", "-u", "origin", "main")
    g("checkout", "-q", "-b", "gb/line-104")
    (repo / "kept.py").write_text("round-1\n")
    g("add", "-A")
    g("commit", "-qm", "round 1")
    g("push", "-q", "-u", "origin", "gb/line-104")
    g("checkout", "-q", "main")
    g("branch", "gb/line-44")
    stale = tmp_path / "line-104"
    g("worktree", "add", "-q", str(stale), "gb/line-104")
    if dirty:
        (stale / "dirty.txt").write_text("unpushed\n")
    return repo, stale


def _assert_slash_spellings_match(path: Path) -> None:
    """`C:/...` in porcelain and `C:\\...` from `str(Path)` are one checkout.

    Both reclaim tests call this, so a raw `str(path) in listed` check fails
    here on every OS, not only on Windows.
    """
    from pathlib import PureWindowsPath

    forward = "worktree " + worktree_key(path) + "\n"
    assert porcelain_lists_worktree(forward, PureWindowsPath(worktree_key(path)))


def _capture_emit(monkeypatch):
    from gbfleet import observe

    events: list[tuple[str, dict]] = []
    real = observe.emit

    def capture(event, **fields):
        events.append((event, dict(fields)))
        return real(event, **fields)

    monkeypatch.setattr(observe, "emit", capture)
    return events


_HELD = {
    "id": "GRPH-1690",
    "status": "next",
    "claimed_by": "",
    "branch": "gb/line-104",
    "bounce_reason": "tests failed",
    "touchpoints": ["fleet/src/gbfleet/until.py"],
}


def test_a_clean_pushed_worktree_holding_the_branch_is_removed_and_resumed(
    tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """GRPH-1785. The previous builder's worktree is clean, pushed, and owned by
    no live child. Resume removes it and starts on `gb/line-104`, the item's branch.

    Sabotage: skip the reclaim and fall through to a fresh `gb/wave-*` cut. This
    child's branch is no longer `gb/line-104`, and `kept.py` is not on it.
    """
    repo, stale = _clone_with_held_branch(tmp_path, dirty=False)
    workspace = tmp_path / "ws"
    events = _capture_emit(monkeypatch)
    planner, supervisor = _clients(
        workspace, clusters=1, workers=0, bound_seats=True,
        cluster_items=[["GRPH-1690"]],
        item_rows=[dict(_HELD)],
    )
    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        limits=Limits(max_workers=1, max_children=1),
        base_branch="main",
    )
    assert result.spawned == 1, result.as_json()
    child = result.wave.spawned[0]
    assert child.branch == "gb/line-104", [c.branch for c in result.wave.spawned]
    show = subprocess.run(
        ["git", "show", "gb/line-104:kept.py"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert show == "round-1\n"
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    # Git prints `C:/...`; `str(stale)` is `C:\...` on Windows. A substring
    # check passes while the checkout is still listed (GRPH-1785).
    assert not porcelain_lists_worktree(listed, stale)
    _assert_slash_spellings_match(stale)
    assert not stale.exists()
    assert any(name == "resume_reclaimed" and fields.get("item") == "GRPH-1690"
               and fields.get("branch") == "gb/line-104" for name, fields in events)
    assert not any(name == "resume_blocked" for name, _fields in events)


def test_a_dirty_holder_does_not_start_the_child_on_another_branch(
    tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """GRPH-1785. The branch is still checked out and this drain will not take the
    worktree. No child starts, on `gb/line-44` or on a fresh `gb/wave-*`. The drain
    emits `resume_blocked` naming the item and the blocking worktree, withdraws the
    delegation, and does not mark the item blocked.

    Sabotage: delete the `ResumeBlocked` raise in `_start` so the `while tree is
    None` fresh cut runs. A child is spawned and `gb/wave-` appears.
    """
    repo, stale = _clone_with_held_branch(tmp_path, dirty=True)
    workspace = tmp_path / "ws"
    events = _capture_emit(monkeypatch)
    delegations: list[dict] = []
    updates: list[dict] = []
    planner, supervisor = _clients(
        workspace, clusters=1, workers=0, bound_seats=True,
        cluster_items=[["GRPH-1690"]],
        item_rows=[dict(_HELD)],
        delegations=delegations,
        updates=updates,
    )
    result = run(
        repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
        limits=Limits(max_workers=1, max_children=1),
        base_branch="main",
    )
    assert result.spawned == 0, result.as_json()
    assert result.wave.spawned == []
    assert result.reason == "spawn_failed", result.as_json()
    blocked = [fields for name, fields in events if name == "resume_blocked"]
    assert len(blocked) == 1, events
    assert blocked[0]["item"] == "GRPH-1690"
    assert blocked[0]["branch"] == "gb/line-104"
    assert Path(blocked[0]["worktree"]).resolve() == stale.resolve()
    assert any(d.get("id") == "GRPH-1690" and d.get("withdraw") is True for d in delegations)
    assert not any(u.get("status") == "blocked" for u in updates)
    assert (stale / "dirty.txt").is_file()
    branches = subprocess.run(
        ["git", "branch", "--list", "gb/wave-*"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert branches.strip() == ""


def test_a_live_child_keeps_the_worktree_that_holds_the_branch(tmp_path: Path):
    """GRPH-1785. A running child owns the checkout, so resume does not remove it.

    Sabotage: drop the live-child check in `_reclaim_idle_worktree`. This returns
    True and `line-104` is gone.
    """
    from types import SimpleNamespace

    from gbfleet.supervisor import _reclaim_idle_worktree

    repo, stale = _clone_with_held_branch(tmp_path, dirty=False)
    child = SimpleNamespace(running=True, worktree=stale)
    assert _reclaim_idle_worktree(repo, "gb/line-104", stale, [child]) is False
    assert stale.exists()
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert porcelain_lists_worktree(listed, stale)
    _assert_slash_spellings_match(stale)


def test_porcelain_lists_a_windows_checkout_printed_with_forward_slashes():
    """GRPH-1785. Git's porcelain line is `worktree C:/Users/...`. `str(Path)`
    on Windows is `C:\\Users\\...`. Those are the same checkout.

    Sabotage: make `porcelain_lists_worktree` a raw `str(path) in text` check.
    This fails, because the backslash spelling is not a substring of the
    forward-slash line.
    """
    from pathlib import PureWindowsPath

    listed = (
        "worktree C:/Users/runner/AppData/Local/Temp/line-104\n"
        "HEAD abcdef\n"
        "branch refs/heads/gb/line-104\n"
    )
    stale = PureWindowsPath(r"C:\Users\runner\AppData\Local\Temp\line-104")
    assert porcelain_lists_worktree(listed, stale)
    assert not porcelain_lists_worktree(
        listed, PureWindowsPath(r"C:\Users\runner\AppData\Local\Temp\other"),
    )


def test_a_server_that_accepts_and_never_replies_does_not_stall_the_loop(tmp_path, git_repo):
    """GRPH-1786. The loop's first read is fleet_status. A peer that accepts and
    sends nothing must be logged and left behind inside the deadline.

    This socket read is bounded twice: httpx's phase timeout and `_wait_bounded`.
    Either one returns this thread inside the join, so removing only the caller
    deadline does not hang it. The read that httpx cannot unblock, and the
    sabotage that does hang, is the next test.
    """
    import io
    import socket
    import threading

    from gbfleet import observe
    from gbfleet.client import ServerUnreachable
    from gbfleet.until import Limits, Wave, _loop

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    stop = threading.Event()
    held: list[socket.socket] = []

    def serve():
        while not stop.is_set():
            try:
                listener.settimeout(0.2)
                conn, _ = listener.accept()
            except OSError:
                continue
            held.append(conn)

    threading.Thread(target=serve, daemon=True).start()
    port = listener.getsockname()[1]
    buf = io.StringIO()
    observe.configure(tmp_path, stream=buf)
    supervisor = Graphban(
        base_url=f"http://127.0.0.1:{port}", api_key="k", deadline=0.4, timeout=30.0,
    )

    class _Down:
        allowed = frozenset({"fleet_status", "search_items", "get_context"})

        def fleet_status(self, **kw):
            return self.call("fleet_status", **kw)

        def call(self, tool, /, **kw):
            raise ServerUnreachable(f"down during {tool}", tool=tool)

        def post_attempt(self, **kw):
            return None

    moved: dict = {}

    def sleep(seconds):
        moved["slept"] = seconds
        raise RuntimeError("moved on")

    def go():
        try:
            _loop(
                Wave(), [], set(), lambda: None, git_repo, tmp_path / "ws", "w", "k",
                f"http://127.0.0.1:{port}",
                lambda *a, **k: None, _Down(), supervisor, {"agent_id": "A-1"}, [],
                Limits(), 1.0, sleep, False,
                empty_ticks=1, mint_tries=1, mint_budget=1.0, minted_start=0,
            )
        except RuntimeError as exc:
            moved["exc"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - the assertion is that the thread ended
            moved["exc"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(target=go, daemon=True)
    started = time.monotonic()
    th.start()
    th.join(3.0)
    elapsed = time.monotonic() - started
    stop.set()
    listener.close()
    for conn in held:
        conn.close()
    supervisor.close()
    assert not th.is_alive(), "timed out: until loop still blocked on a server that never replies"
    assert elapsed < 2.5, elapsed
    names = [
        json.loads(line)["event"]
        for line in buf.getvalue().splitlines() if line.startswith("{")
    ]
    assert held, "the server never accepted; this did not reach a hung read"
    assert "preferences" in names, names
    assert any(name in names for name in ("call_timeout", "retry")), names
    assert moved.get("exc"), moved


def test_a_read_the_socket_timeout_cannot_unblock_still_returns_the_loop(tmp_path, git_repo):
    """GRPH-1786. `close()` does not wake a blocked read. The caller-side deadline
    has to return anyway, and the loop has to log that and keep going.

    Sabotage: make `_wait_bounded` wait until the worker sets `done` and this
    thread is still alive at the join bound.
    """
    import io
    import threading

    from gbfleet import observe
    from gbfleet.client import ServerUnreachable
    from gbfleet.until import Limits, Wave, _loop

    class _Stuck(httpx.BaseTransport):
        def handle_request(self, request):
            time.sleep(30)
            raise httpx.ReadTimeout("stuck", request=request)

        def close(self):
            pass

    buf = io.StringIO()
    observe.configure(tmp_path, stream=buf)
    supervisor = Graphban(
        base_url="http://127.0.0.1:9", api_key="k", deadline=0.4, timeout=30.0,
        transport=_Stuck(),
    )

    class _Down:
        allowed = frozenset({"fleet_status", "search_items"})

        def call(self, tool, /, **kw):
            raise ServerUnreachable(f"down during {tool}", tool=tool)

        def post_attempt(self, **kw):
            return None

        def fleet_status(self, **kw):
            return self.call("fleet_status", **kw)

    moved: dict = {}

    def sleep(seconds):
        moved["slept"] = seconds
        raise RuntimeError("moved on")

    def go():
        try:
            _loop(
                Wave(), [], set(), lambda: None, git_repo, tmp_path / "ws", "w", "k",
                "http://127.0.0.1:9",
                lambda *a, **k: None, _Down(), supervisor, {"agent_id": "A-1"}, [],
                Limits(), 1.0, sleep, False,
                empty_ticks=1, mint_tries=1, mint_budget=1.0, minted_start=0,
            )
        except RuntimeError as exc:
            moved["exc"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            moved["exc"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(target=go, daemon=True)
    started = time.monotonic()
    th.start()
    th.join(3.0)
    elapsed = time.monotonic() - started
    supervisor.close()
    assert not th.is_alive(), "timed out: until loop still waiting on a read that never returns"
    assert elapsed < 2.5, elapsed
    names = [
        json.loads(line)["event"]
        for line in buf.getvalue().splitlines() if line.startswith("{")
    ]
    assert "call_timeout" in names, names
    assert "preferences" in names, names
    assert moved.get("exc"), moved


def test_a_fetch_that_does_not_return_is_logged_and_not_success(tmp_path, monkeypatch):
    """GRPH-1786. `refresh_ref` is the until loop's fetch of the base. No timeout used
    to be a waitpid with no log line.

    Sabotage: drop `timeout=` from `_bounded_git` and this raises AssertionError.
    """
    import io
    import subprocess

    from gbfleet import observe
    from gbfleet import worktree as wt

    buf = io.StringIO()
    observe.configure(tmp_path, stream=buf)

    def boom(args, **k):
        if "timeout" not in k:
            raise AssertionError("git fetch has no timeout")
        raise subprocess.TimeoutExpired(args, k["timeout"])

    monkeypatch.setattr(wt.subprocess, "run", boom)
    assert wt.refresh_ref(tmp_path, "origin", "origin/main") is False
    names = [
        json.loads(line)["event"]
        for line in buf.getvalue().splitlines() if line.startswith("{")
    ]
    assert "git_timeout" in names, names


# ---- GRPH-1311: --acknowledge-reach on until and the dry run ---------------------------------


def test_until_help_lists_acknowledge_reach(capsys):
    """`gbfleet until --help` lists the flag. Sabotage: delete the argument."""
    from gbfleet.cli import main

    with pytest.raises(SystemExit) as raised:
        main(["until", "--help"])
    assert raised.value.code == 0
    text = capsys.readouterr().out
    assert "--acknowledge-reach" in text
    assert "ITEM[,ITEM]" in text


def test_an_empty_acknowledge_reach_is_refused(monkeypatch, git_repo: Path, capsys):
    """A flag that names nothing is the same as omitting it, so it is an error instead."""
    from gbfleet import cli

    def fail_run(*_a, **_k):
        raise AssertionError("run_until must not be called")

    monkeypatch.setenv("GBFLEET_API_KEY", KEY)
    monkeypatch.setattr(cli, "run_until", fail_run)
    code = cli.main([
        "until", "--repo", str(git_repo), "--server", "http://gb.invalid",
        "--adapter", "gbagent", "--acknowledge-reach", " , ",
    ])
    assert code == 2
    assert "one or more item ids" in capsys.readouterr().err


def test_main_passes_acknowledge_reach_only_when_the_flag_names_items(monkeypatch, git_repo: Path):
    """THE CALL from the command. parse_args is not it: a flag that never reaches `run`
    acknowledges nothing. Sabotage: drop `acknowledge_reach=acknowledge` from the
    `run_until` call in `_until` — this reads None on the flagged invocation."""
    from gbfleet import cli
    from gbfleet.until import Report

    seen: dict = {}

    class FakeGB:
        def __init__(self, base_url, api_key, allowed=None, **_kw):
            self.allowed = allowed

        def close(self):
            pass

    def fake_run(*_a, **kw):
        seen["ack"] = kw.get("acknowledge_reach")
        return Report(ok=True, reason="idle", exit=0)

    monkeypatch.setenv("GBFLEET_API_KEY", KEY)
    monkeypatch.setattr(cli, "Graphban", FakeGB)
    monkeypatch.setattr(cli, "run_until", fake_run)
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *_a, **_k: object())

    base = ["until", "--repo", str(git_repo), "--server", "http://gb.invalid",
            "--adapter", "gbagent"]
    assert cli.main(base) == 0
    assert seen["ack"] is None
    assert cli.main(base + ["--acknowledge-reach", "GRPH-X, GRPH-Y"]) == 0
    assert seen["ack"] == {"GRPH-X", "GRPH-Y"}


def test_the_dry_run_lists_the_named_ids_and_still_does_not_delegate():
    """GRPH-1311. The dry run takes the same flag and prints the operator's list.
    It does not call delegate. A name no cluster has is still printed: an empty
    list would read as "nothing to acknowledge" when the operator mistyped the id.

    Sabotage: stop passing `acknowledge_reach` into `plan`. The flagged plan then
    has no such key, same as a run that never asked.
    """
    class Planner:
        def __init__(self):
            self.calls: list[str] = []

        def call(self, tool, **_kw):
            self.calls.append(tool)
            if tool == "collision_clusters":
                return {"clusters": [{"items": ["GRPH-X"]}, {"items": ["GRPH-Y"]}], "total": 2}
            if tool == "search_items":
                return {"results": []}
            raise AssertionError(tool)

    bare_planner = Planner()
    bare = until.plan(bare_planner, None, 4)
    assert "acknowledge_reach" not in bare
    assert "delegate" not in bare_planner.calls

    named_planner = Planner()
    named = until.plan(named_planner, None, 4, acknowledge_reach={"GRPH-Z", "GRPH-X"})
    assert named["acknowledge_reach"] == ["GRPH-X", "GRPH-Z"]
    assert named["would_delegate"] == ["GRPH-X", "GRPH-Y"]
    assert "delegate" not in named_planner.calls


def test_dry_run_main_forwards_the_flag(monkeypatch, git_repo: Path, capsys):
    """The dry-run command, not only `plan`. Sabotage: the real `run_until` path
    forwards the flag and this one does not — the printed plan has no key."""
    from gbfleet import cli
    from gbfleet import until as until_mod

    def fake_plan(*_a, **kw):
        return {
            "would_delegate": ["GRPH-X"],
            "clusters_held": 0,
            "clusters_free": 1,
            "dependency_holds": [],
            "capped_by_max_workers": False,
            "acknowledge_reach": sorted(kw.get("acknowledge_reach") or []),
        }

    monkeypatch.setenv("GBFLEET_API_KEY", KEY)
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *_a, **_k: object())
    monkeypatch.setattr(until_mod, "plan", fake_plan)
    code = cli.main([
        "until", "--repo", str(git_repo), "--server", "http://gb.invalid",
        "--adapter", "gbagent", "--dry-run", "--acknowledge-reach", "GRPH-X",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["acknowledge_reach"] == ["GRPH-X"]


def _delegations_for(git_repo, tmp_path, scripts, state, *, acknowledge_reach):
    """One run, two free clusters. The first child exits; the second is then delegated."""
    workspace = tmp_path / "ws"
    delegations: list[dict] = []
    planner, supervisor = _clients(
        workspace, clusters=2, cluster_items=[["GRPH-X"], ["GRPH-Y"]],
        delegations=delegations, sticky_clusters=True, drop_delegated=True,
    )
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=2,
        limits=Limits(max_workers=1, max_children=2),
        acknowledge_reach=acknowledge_reach,
    )
    return result, delegations


def test_acknowledge_reach_is_sent_for_the_named_item_and_not_the_other(
        git_repo: Path, tmp_path: Path, scripts, state: Path):
    """GRPH-1311. One run of `until --acknowledge-reach GRPH-X` delegates two items.
    GRPH-X's delegate call carries acknowledge_reach=true. GRPH-Y's does not.

    Sabotage: pass the argument on every delegate call, or on none. Either fails.
    """
    result, delegations = _delegations_for(
        git_repo, tmp_path, scripts, state, acknowledge_reach={"GRPH-X"})
    assert result.reason == "idle", result.detail
    by_id = {d["id"]: d for d in delegations}
    assert set(by_id) == {"GRPH-X", "GRPH-Y"}, [d["id"] for d in delegations]
    assert by_id["GRPH-X"]["acknowledge_reach"] is True
    assert "acknowledge_reach" not in by_id["GRPH-Y"]


def test_a_run_without_the_flag_sends_no_acknowledge_reach(
        git_repo: Path, tmp_path: Path, scripts, state: Path):
    """Existing behaviour. Omitting the flag leaves the argument off every delegate
    call in the run. Sabotage: default the argument to True — this fails."""
    result, delegations = _delegations_for(
        git_repo, tmp_path, scripts, state, acknowledge_reach=None)
    assert result.reason == "idle", result.detail
    assert delegations, "the run delegated nothing, so the absence is not evidence"
    assert all("acknowledge_reach" not in d for d in delegations)


# ---- GRPH-1919: a live child whose lease lapsed is the work; an exited one is settled --------


class _LapsingServer:
    """The lease route answers `extended: false`; the item is still `in_progress`; the roster
    lists the child's holding as stale (or not at all), as the server does after a lapse."""

    def __init__(self, agents):
        self.agents = agents
        self.posts: list[dict] = []

    def post_observation(self, **payload):
        self.posts.append(payload)
        return {"item_id": payload["item_id"], "kind": "lease", "extended": False}

    def call(self, tool, **args):
        if tool == "fleet_status":
            return {"agents": self.agents}
        if tool == "get_item_details":
            return {"id": args["id"], "status": "in_progress"}
        raise AssertionError(tool)


def test_a_live_child_whose_lease_lapsed_keeps_the_cycle_open_and_is_not_stopped(
    monkeypatch, tmp_path: Path,
):
    """GRPH-1919. Two children of the line drain committed work, their leases lapsed while
    their processes lived, and `until` summarised `idle` and SIGTERMed both. The lapse is
    reported, the keep-alive re-posts on the next interval, the cycle is not idle, and
    `_leave` stops nothing. Sabotage: drop the `lapsed_under_live` check from
    `_child_currently_holds` and `_idle_blocked` is False and the child is stopped."""
    import gbfleet.until as until_mod
    from gbfleet import heartbeat
    from gbfleet.supervisor import Wave as _Wave

    child = _Held("GRPH-A47578", str(tmp_path / "line-406"))
    child.adapter = "qwen"
    child.branch = "gb/line-406"
    child.held_items = ["GRPH-1905"]
    child.process = type("P", (), {"pid": 97011})()
    child.lease_kept_at = 0.0
    server = _LapsingServer([{"id": "GRPH-A47578", "worktree": child.worktree,
                              "holdings": [{"id": "GRPH-1905", "phase": "stale"}]}])
    wave = _Wave()
    monkeypatch.setattr(heartbeat, "branch_commit", lambda _c: None)

    heartbeat.keep(wave, [child], server, now=1000.0)
    assert wave.lease_lapsed_live == [
        "GRPH-1905: lease not extended for live child pid 97011 (GRPH-A47578)"]
    assert heartbeat.lapsed_under_live(child) == ["GRPH-1905"]

    stopped = []
    monkeypatch.setattr(until_mod, "stop",
                        lambda c, reason, grace=0: stopped.append((c, reason)))
    monkeypatch.setattr(until_mod, "_reap_all", lambda *_a, **_k: None)
    monkeypatch.setattr(until_mod, "_report_exits", lambda *_a, **_k: None)

    assert until_mod._idle_blocked(server, [child]) is True
    until_mod._stop_children_holding_nothing(wave, [child], server, server, "main")
    assert stopped == [] and child.running is True and child.stopped_because is None

    # The next interval posts the lease again: the cycle keeps asking, and re-reports nothing.
    heartbeat.keep(wave, [child], server, now=1000.0 + heartbeat.FALLBACK_INTERVAL)
    assert [p["item_id"] for p in server.posts] == ["GRPH-1905", "GRPH-1905"]
    assert len(wave.lease_lapsed_live) == 1

    # Once the process is gone, the lapse is no longer a live child's.
    child.running = False
    assert heartbeat.lapsed_under_live(child) == []
    assert until_mod._idle_blocked(server, []) is False


class _ExitServer:
    def __init__(self, rows):
        self.rows = rows
        self.updates: list[dict] = []

    def call(self, tool, **args):
        if tool == "get_item_details":
            return self.rows[args["id"]]
        if tool == "update_item":
            self.updates.append(args)
            self.rows[args["id"]]["status"] = args.get("status", self.rows[args["id"]]["status"])
            return self.rows[args["id"]]
        raise AssertionError(tool)


class _Exited:
    def __init__(self, item, branch):
        self.adapter = "qwen"
        self.pid = 2480
        self.agent_id = "GRPH-A47583"
        self.branch = branch
        self.running = False
        self.held_items = [item]
        self.ever_held = [item]
        self.review_items: list = []
        self.assigned = {"item": item, "state": "claimed"}


def _exited_repo(tmp_path: Path) -> tuple[Path, str]:
    import subprocess as sp
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "base"],
                 ["checkout", "-q", "-b", "gb/line-407"],
                 ["commit", "-q", "--allow-empty", "-m", "work"]):
        sp.run(["git", *args], cwd=repo, check=True)
    head = sp.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                  capture_output=True, text=True).stdout.strip()
    return repo, head


def test_an_exited_childs_green_in_progress_item_is_moved_to_review(
    monkeypatch, tmp_path: Path,
):
    """GRPH-1919. A child exits without its status call; its PR's head carries suite_green.
    `until` moves the item to review with a note naming the child and the head, and stops
    tracking it. Sabotage: drop the `_hand_exited_to_review` call and no update is made."""
    import gbfleet.until as until_mod
    from gbfleet.supervisor import Wave as _Wave

    repo, head = _exited_repo(tmp_path)
    url = "https://github.com/o/r/pull/2250"
    rows = {"GRPH-1905": {
        "id": "GRPH-1905", "status": "in_progress", "branch": "gb/line-407",
        "evidence": [{"kind": "url", "url": url, "detail": "draft PR opened by gbfleet"},
                     {"kind": "attestation", "commit": head,
                      "predicates": [{"name": "suite_green", "passed": True}]}]}}
    server = _ExitServer(rows)
    monkeypatch.setattr(until_mod, "ci_verdict",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("gh asked")))
    exited: dict = {}
    until_mod._track_exits(exited, [_Exited("GRPH-1905", "gb/line-407")])
    lines, pending = until_mod._settle_exited(server, exited, _Wave(), repo, "GRPH-A1")

    assert lines == [] and pending is False
    assert exited == {}
    assert len(server.updates) == 1
    update = server.updates[0]
    assert update["id"] == "GRPH-1905" and update["status"] == "review"
    note = update["evidence"][0]["detail"]
    assert "qwen pid 2480 (GRPH-A47583)" in note and head[:12] in note and url in note


def test_an_exited_childs_item_waits_on_ci_and_holds_the_cycle_while_it_runs(
    monkeypatch, tmp_path: Path,
):
    """Not green yet: a wait, and the cycle stays open while the run is in progress."""
    import gbfleet.until as until_mod
    from gbfleet.supervisor import Wave as _Wave

    repo, head = _exited_repo(tmp_path)
    rows = {"GRPH-1901": {
        "id": "GRPH-1901", "status": "in_progress", "branch": "gb/line-407",
        "evidence": [{"kind": "url", "url": "https://github.com/o/r/pull/2244"}]}}
    server = _ExitServer(rows)
    monkeypatch.setattr(until_mod, "ci_verdict", lambda *_a: ("pending", head, ""))
    exited: dict = {}
    until_mod._track_exits(exited, [_Exited("GRPH-1901", "gb/line-407")])
    lines, pending = until_mod._settle_exited(server, exited, _Wave(), repo, "GRPH-A1")
    assert pending is True and server.updates == []
    assert lines and lines[0].startswith("GRPH-1901: in_progress after its child")
    assert "waits on suite_green" in lines[0]
    # Moved on by somebody else: forgotten.
    rows["GRPH-1901"]["status"] = "review"
    assert until_mod._settle_exited(server, exited, _Wave(), repo, "GRPH-A1") == ([], False)
    assert exited == {}


def test_an_exited_childs_gated_branch_is_named_under_the_gate(monkeypatch, tmp_path: Path):
    """GRPH-1919. GRPH-1910's branch was pushed and held by the touchpoint gate; the cycle
    read idle over it. The item is named under the gate's reason, and `waits` is not empty,
    so the exit is not `idle`."""
    import gbfleet.until as until_mod
    from gbfleet.supervisor import Wave as _Wave

    repo, _head = _exited_repo(tmp_path)
    rows = {"GRPH-1910": {"id": "GRPH-1910", "status": "in_progress",
                          "branch": "gb/line-407", "evidence": []}}
    server = _ExitServer(rows)
    wave = _Wave()
    wave.unproposed["gb/line-407"] = "changes files outside every declared touchpoint: a.py"
    monkeypatch.setattr(until_mod, "ci_verdict",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("gh asked")))
    exited: dict = {}
    until_mod._track_exits(exited, [_Exited("GRPH-1910", "gb/line-407")])
    lines, pending = until_mod._settle_exited(server, exited, wave, repo, "GRPH-A1")
    assert pending is False and server.updates == []
    assert lines == [(
        "GRPH-1910: in_progress after its child qwen pid 2480 (GRPH-A47583) exited; "
        "gb/line-407 not proposed — changes files outside every declared touchpoint: a.py")]
    assert "GRPH-1910" in exited


def test_an_exited_childs_item_with_no_pr_and_no_gate_is_left_to_the_server(tmp_path: Path):
    import gbfleet.until as until_mod
    from gbfleet.supervisor import Wave as _Wave

    repo, _head = _exited_repo(tmp_path)
    server = _ExitServer({"GRPH-7": {"id": "GRPH-7", "status": "in_progress",
                                     "branch": "gb/line-407", "evidence": []}})
    exited: dict = {}
    until_mod._track_exits(exited, [_Exited("GRPH-7", "gb/line-407")])
    assert until_mod._settle_exited(server, exited, _Wave(), repo, "") == ([], False)
    assert exited == {} and server.updates == []
