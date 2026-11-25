"""Server bench runs: a scratch project and seats for a run, and baseline records (GRPH-1856)."""
import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Agent, AttemptTelemetry, BenchBaseline, BenchRun, Item, Project
from app.services import bench as bench_svc

PR = "https://github.com/o/r/pull/7"
TASKS = [{"id": "t1", "title": "fix a thing", "touchpoints": ["a.py"]},
         {"id": "t2", "title": "fix another"}]
IDENT = {"vendor": "anthropic", "model": "sonnet", "binary_version": "1.2.3",
         "suite_id": "template", "suite_version": "0.1.0"}


@pytest.fixture()
def env(client, auth):
    proj = client.post("/api/projects", json={"name": "BenchSrc"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "bench", "scopes": ["read", "write"]},
                      headers=auth).json()["plaintext"]
    return {"proj": proj, "key": key, "h": {"X-API-Key": key}, "auth": auth}


def _counts(proj):
    with SessionLocal() as db:
        return (db.scalar(select(func.count()).select_from(Item).where(Item.project_id == proj)),
                db.scalar(select(func.count()).select_from(Agent).where(Agent.project_id == proj)),
                db.scalar(select(func.count()).select_from(AttemptTelemetry)))


def _open(client, env, **over):
    return client.post(f"/api/bench/runs?project_id={env['proj']}", headers=env["h"],
                       json={**IDENT, "tasks": TASKS, **over})


def _post(client, env, **over):
    body = {**IDENT, "outcomes": [
        {"task_id": "t1", "outcome": "check_green", "capabilities": ["A1"], "tokens": 100},
        {"task_id": "t2", "outcome": "harness_error", "capabilities": ["A1"]}], **over}
    return client.post(f"/api/bench/results?project_id={env['proj']}", headers=env["h"], json=body)


def test_agent_key_opens_a_run_and_a_session_cannot(client, auth, env):
    r = _open(client, env)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [s["task_id"] for s in out["seats"]] == ["t1", "t2"]
    assert all(s["enrolment_code"] for s in out["seats"])
    assert out["project_id"] != env["proj"]
    with SessionLocal() as db:
        run = db.get(BenchRun, out["id"])
        assert run.project_id == out["project_id"] and run.source_project_id == env["proj"]
        assert db.get(Project, out["project_id"]) is not None
    assert client.post(f"/api/bench/runs?project_id={env['proj']}", headers=auth,
                       json={**IDENT, "tasks": TASKS}).status_code == 401


def test_opening_a_run_leaves_the_source_project_unchanged(client, env):
    before = _counts(env["proj"])
    assert _open(client, env).status_code == 200
    assert _counts(env["proj"])[:2] == before[:2]


def test_agent_key_posts_a_result_and_rollups_exclude_plumbing(client, env):
    r = _post(client, env)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["rollups"]["A1"] == {"n": 1, "green": 1, "excluded": 1}
    assert out["tokens"] == 100
    listed = client.get(f"/api/bench/results?project_id={env['proj']}", headers=env["h"]).json()
    assert [x["id"] for x in listed["results"]] == [out["id"]]
    # A person reads them too, beside the grid.
    as_user = client.get(f"/api/bench/results?project_id={env['proj']}", headers=env["auth"])
    assert [x["id"] for x in as_user.json()["results"]] == [out["id"]]


def test_unreported_tokens_are_null_never_zero(client, env):
    out = _post(client, env, outcomes=[{"task_id": "t1", "outcome": "check_red"}]).json()
    assert out["tokens"] is None and out["wall_seconds"] is None


def _seed_telemetry(proj):
    """Finished attempts for the same vendor+model+capability the baseline names, so a
    post that leaked into telemetry or a rate would land in exactly these cells."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for i, outcome in enumerate(["signed_off", "signed_off", "bounced"]):
            db.add(AttemptTelemetry(
                id=f"at-bench-{i}", project_id=proj, vendor="anthropic", model="sonnet",
                binary_version="1.2.3", capabilities=["A1"], outcome=outcome,
                tokens_in=10, tokens_out=10, derived_at=now - timedelta(days=1)))
        db.commit()


def _telemetry_and_grid(proj):
    from app.services import harness as harness_svc

    with SessionLocal() as db:
        rows = sorted((harness_svc.row_dict(r) for r in db.scalars(select(AttemptTelemetry))),
                      key=lambda r: r["id"])
        cells = harness_svc.report(db, proj)["cells"]
        db.commit()
    # The `baseline` block is its own layer (GRPH-1862); natural and probe must not move.
    for c in cells:
        c.pop("baseline", None)
        for leaf in c.get("leaves") or []:
            leaf.pop("baseline", None)
    return rows, cells


def test_posting_a_result_leaves_telemetry_and_rates_unchanged(client, env):
    _seed_telemetry(env["proj"])
    rows, cells = _telemetry_and_grid(env["proj"])
    assert len(rows) == 3 and any(c.get("rate") is not None for c in cells), cells
    assert _post(client, env).status_code == 201
    assert _post(client, env, outcomes=[
        {"task_id": "t1", "outcome": "check_red", "capabilities": ["A1"]}]).status_code == 201
    assert _telemetry_and_grid(env["proj"]) == (rows, cells)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(BenchBaseline)) == 2


@pytest.mark.parametrize("field", ["vendor", "model", "suite_id", "suite_version"])
def test_a_result_missing_a_key_field_is_refused_naming_it(client, env, field):
    body = {k: v for k, v in IDENT.items() if k != field}
    r = client.post(f"/api/bench/results?project_id={env['proj']}", headers=env["h"],
                    json={**body, "outcomes": [{"task_id": "t1", "outcome": "check_green"}]})
    assert r.status_code == 422 and field in r.text
    r = _open(client, env, **{field: ""})
    assert r.status_code == 422 and field in r.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(BenchBaseline)) == 0


def test_an_overlong_key_field_is_refused_naming_it(client, env):
    r = _post(client, env, vendor="v" * 33)
    assert r.status_code == 422 and "vendor" in r.text


def test_a_bad_outcome_is_refused(client, env):
    r = _post(client, env, outcomes=[{"task_id": "t1", "outcome": "great"}])
    assert r.status_code == 422


def test_mcp_bench_round_trip(client, env):
    def call(args):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "bench", "arguments": args}}
        return client.post("/api/mcp", json=body, headers=env["h"]).json()["result"]

    run = call({"action": "open_run", "project_id": env["proj"],
                "body": {**IDENT, "tasks": TASKS}})
    assert not run.get("isError"), run
    assert len(run["structuredContent"]["seats"]) == 2
    res = call({"action": "post_result", "project_id": env["proj"], "body": {
        **IDENT, "run_id": run["structuredContent"]["id"],
        "outcomes": [{"task_id": "t1", "outcome": "check_green"}]}})
    assert not res.get("isError"), res
    assert res["structuredContent"]["run_id"] == run["structuredContent"]["id"]
    bad = call({"action": "post_result", "project_id": env["proj"], "body": {}})
    assert bad.get("isError")


def test_open_run_returns_a_scratch_scoped_key_revoked_on_post(client, env):
    out = _open(client, env).json()
    key = out["api_key"]
    h = {"X-API-Key": key}
    code = out["seats"][0]["enrolment_code"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "register_agent", "arguments": {"enrolment_code": code}}}
    res = client.post("/api/mcp", json=body, headers=h).json()["result"]
    assert not res.get("isError"), res
    assert res["structuredContent"]["project_id"] == out["project_id"]
    from app.security.apikey import verify_api_key
    with SessionLocal() as db:
        assert verify_api_key(db, key) is not None
    src = client.get(f"/api/items?project_id={env['proj']}", headers=h)
    assert src.status_code in (401, 403, 404), src.text
    assert _post(client, env, run_id=out["id"]).status_code == 201
    from app.security.apikey import verify_api_key
    with SessionLocal() as db:
        assert verify_api_key(db, key) is None
    again = client.post("/api/mcp", json=body, headers=h)
    assert again.status_code == 401


def test_open_run_seats_review_tasks_as_review_work(client, env):
    tasks = [TASKS[0], {"id": "r1", "kind": "review", "title": "review it", "ref": "gb/x",
                        "expect": "bounce", "pr": PR}]
    out = _open(client, env, tasks=tasks).json()
    by = {s["task_id"]: s for s in out["seats"]}
    assert by["r1"]["kind"] == "review" and by["t1"]["kind"] == "build"
    from app.models import Enrolment
    with SessionLocal() as db:
        build, rev = db.get(Item, by["t1"]["item_id"]), db.get(Item, by["r1"]["item_id"])
        assert build.status == "next"
        assert rev.status == "review" and rev.branch == "gb/x" and rev.github_url == PR
        assert rev.built_by and rev.built_by != by["r1"].get("agent")
        seats = db.scalars(select(Enrolment).where(
            Enrolment.project_id == out["project_id"])).all()
        assert sorted(bool(e.review_only) for e in seats) == [False, True]
        assert [e.item_id for e in seats if e.review_only] == [None]


def test_review_outcomes_score_green_when_verdict_matches_expect(client, env):
    def o(tid, outcome, expect):
        return {"task_id": tid, "outcome": outcome, "capabilities": ["A1"],
                "kind": "review", "expect": expect}
    r = _post(client, env, outcomes=[o("a", "bounce", "bounce"), o("b", "sign_off", "bounce"),
                                     o("c", "sign_off", "sign_off")])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["rollups"]["A1"] == {"n": 3, "green": 2, "excluded": 0}
    assert out["outcomes"][0]["kind"] == "review" and out["outcomes"][0]["expect"] == "bounce"


def _mcp(client, key, name, args):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}
    res = client.post("/api/mcp", json=body, headers={"X-API-Key": key}).json()["result"]
    assert not res.get("isError"), res
    return res["structuredContent"]


def test_a_reviewer_on_the_review_seat_claims_the_review_item(client, env):
    """The bounce on GRPH-1878: the seat existed but claim_review withheld the item
    (`pr_unlinked`). Registering on the review_only seat must hand over that item."""
    task = {"id": "r1", "kind": "review", "title": "review it", "ref": "gb/x",
            "expect": "bounce", "pr": PR}
    out = _open(client, env, tasks=[task]).json()
    seat = out["seats"][0]
    me = _mcp(client, out["api_key"], "register_agent",
              {"enrolment_code": seat["enrolment_code"], "label": "reviewer"})
    got = _mcp(client, out["api_key"], "claim_review",
               {"agent_id": me["agent_id"], "wait_seconds": 0})
    assert got["claimed"] is True, got
    assert got["item"]["id"] == seat["item_id"]
    assert got["branch"] == "gb/x"


@pytest.mark.parametrize("drop", ["pr", "ref"])
def test_a_review_task_without_a_pr_or_ref_is_refused_by_name(client, env, drop):
    task = {"id": "r1", "kind": "review", "title": "t", "ref": "gb/x", "pr": PR,
            "expect": "bounce"}
    task.pop(drop)
    before = _counts(env["proj"])
    r = _open(client, env, tasks=[task])
    assert r.status_code == 422 and drop in r.text, r.text
    assert _counts(env["proj"])[:2] == before[:2]


def test_build_seat_cannot_claim_review_and_review_seat_gets_its_own_item(client, env):
    """GRPH-1883: a build child must not take the run's review items, and a review child
    gets its own task's item rather than the queue head."""
    tasks = [
        {"id": "b1", "title": "build it"},
        {"id": "r1", "kind": "review", "title": "older", "ref": "gb/x", "expect": "bounce", "pr": PR},
        {"id": "r2", "kind": "review", "title": "newer", "ref": "gb/y", "expect": "bounce", "pr": PR},
    ]
    out = _open(client, env, tasks=tasks).json()
    by = {s["task_id"]: s for s in out["seats"]}
    build = _mcp(client, out["api_key"], "register_agent",
                 {"enrolment_code": by["b1"]["enrolment_code"], "label": "builder"})
    got = _mcp(client, out["api_key"], "claim_review",
               {"agent_id": build["agent_id"], "wait_seconds": 0})
    assert got["claimed"] is False, got
    rev = _mcp(client, out["api_key"], "register_agent",
               {"enrolment_code": by["r2"]["enrolment_code"], "label": "reviewer"})
    got = _mcp(client, out["api_key"], "claim_review",
               {"agent_id": rev["agent_id"], "wait_seconds": 0})
    assert got["claimed"] is True, got
    assert got["item"]["id"] == by["r2"]["item_id"]


def _age_out_seats(proj_id):
    """Move the clock past ENROLMENT_TTL_MINUTES: every seat in the project is now old."""
    from datetime import datetime, timedelta, timezone
    from app.models import Enrolment
    from app.services import fleet as fleet_svc
    with SessionLocal() as db:
        for e in db.scalars(select(Enrolment).where(Enrolment.project_id == proj_id)):
            e.expires_at = datetime.now(timezone.utc) - timedelta(
                minutes=fleet_svc.ENROLMENT_TTL_MINUTES)
        db.commit()


def test_a_seat_requested_after_the_ttl_registers(client, env):
    """GRPH-1888: a task starting after minute 30 found its open_run seat expired."""
    out = _open(client, env).json()
    _age_out_seats(out["project_id"])
    stale = out["seats"][1]["enrolment_code"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "register_agent", "arguments": {"enrolment_code": stale}}}
    res = client.post("/api/mcp", json=body, headers={"X-API-Key": out["api_key"]}).json()
    assert "expired" in str(res), res
    r = client.post(f"/api/bench/seat?project_id={env['proj']}", headers=env["h"],
                    json={"run_id": out["id"], "task_id": "t2"})
    assert r.status_code == 200, r.text
    me = _mcp(client, out["api_key"], "register_agent",
              {"enrolment_code": r.json()["enrolment_code"], "label": "late"})
    assert me["assigned"]["state"] == "claimed"
    assert me["assigned"]["item"] == out["seats"][1]["item_id"]


def test_a_seat_for_an_unknown_task_is_refused(client, env):
    out = _open(client, env).json()
    r = client.post(f"/api/bench/seat?project_id={env['proj']}", headers=env["h"],
                    json={"run_id": out["id"], "task_id": "nope"})
    assert r.status_code == 404
