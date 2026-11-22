"""Server bench runs: a scratch project and seats for a run, and baseline records (GRPH-1856)."""
import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Agent, AttemptTelemetry, BenchBaseline, BenchRun, Item, Project
from app.services import bench as bench_svc

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
