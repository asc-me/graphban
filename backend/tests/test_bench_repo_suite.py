"""Server repo-suite store: versioned adopt/remove (GRPH-1857, GRPH-P98 §3)."""
import pytest

from app.services import bench as bench_svc


@pytest.fixture()
def env(client, auth):
    proj = client.post("/api/projects", json={"name": "SuiteSrc"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "suite", "scopes": ["read", "write"]},
                      headers=auth).json()["plaintext"]
    return {"proj": proj, "h": {"X-API-Key": key}, "auth": auth}


def task(tid="t1", **over):
    return {"id": tid, "kind": "build", "base_sha": "abc123", "title": "fix",
            "capabilities": ["A1"], "check": {"command": "pytest", "tests": ["a"]}, **over}


def _suite(client, env):
    return client.get(f"/api/bench/suite?project_id={env['proj']}", headers=env["auth"]).json()


def _adopt(client, env, tasks, version):
    return client.post(f"/api/bench/suite/adopt?project_id={env['proj']}", headers=env["h"],
                       json={"expected_version": version, "tasks": tasks})


def _remove(client, env, ids, version):
    return client.post(f"/api/bench/suite/remove?project_id={env['proj']}", headers=env["h"],
                       json={"expected_version": version, "task_ids": ids})


def test_adopt_then_remove_each_bump_the_version(client, env):
    assert _suite(client, env) == {"version": 0, "tasks": []}
    r = _adopt(client, env, [task("t1"), task("t2")], 0)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    got = _suite(client, env)
    assert got["version"] == 1 and [t["id"] for t in got["tasks"]] == ["t1", "t2"]
    assert got["tasks"][0]["base_sha"] == "abc123"
    r = _remove(client, env, ["t1"], 1)
    assert r.status_code == 200 and r.json()["version"] == 2
    got = _suite(client, env)
    assert got["version"] == 2 and [t["id"] for t in got["tasks"]] == ["t2"]


def test_stale_expected_version_is_409_with_the_current_version(client, env):
    assert _adopt(client, env, [task("t1")], 0).status_code == 200
    r = _adopt(client, env, [task("t2")], 0)
    assert r.status_code == 409
    assert r.json()["current_version"] == 1
    assert [t["id"] for t in _suite(client, env)["tasks"]] == ["t1"]
    r = _remove(client, env, ["t1"], 0)
    assert r.status_code == 409 and r.json()["current_version"] == 1
    assert _suite(client, env)["version"] == 1


def test_refused_adopt_does_not_bump(client, env):
    bad = task("t1")
    del bad["base_sha"]
    r = _adopt(client, env, [bad], 0)
    assert r.status_code == 422 and "base_sha" in r.json()["detail"]
    r = _adopt(client, env, [task("t1", capabilities=["Z9"])], 0)
    assert r.status_code == 422 and "capabilities" in r.json()["detail"]
    assert _remove(client, env, ["nope"], 0).status_code == 404
    assert _suite(client, env)["version"] == 0


def test_a_session_cannot_write(client, env):
    r = client.post(f"/api/bench/suite/adopt?project_id={env['proj']}", headers=env["auth"],
                    json={"expected_version": 0, "tasks": [task()]})
    assert r.status_code == 401


def test_readopting_an_id_replaces_it(client, env):
    assert _adopt(client, env, [task("t1", title="old")], 0).status_code == 200
    r = _adopt(client, env, [task("t1", title="new")], 1)
    assert r.status_code == 200 and r.json()["version"] == 2
    assert [(t["id"], t["title"]) for t in _suite(client, env)["tasks"]] == [("t1", "new")]


def test_a_build_task_needs_a_check_and_a_review_task_an_expect(client, env):
    no_check = task("t1")
    del no_check["check"]
    r = _adopt(client, env, [no_check], 0)
    assert r.status_code == 422 and "check" in r.json()["detail"]
    r = _adopt(client, env, [task("t2", kind="review", check=None)], 0)
    assert r.status_code == 422 and "expect" in r.json()["detail"]
    ok = _adopt(client, env, [task("t3", kind="review", check=None, expect="bounce", ref="b")], 0)
    assert ok.status_code == 200, ok.text


def test_mcp_suite_round_trip_and_stale_version(client, env):
    def call(args):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "bench", "arguments": {"project_id": env["proj"], **args}}}
        return client.post("/api/mcp", json=body, headers=env["h"]).json()["result"]

    got = call({"action": "adopt_tasks", "body": {"expected_version": 0, "tasks": [task()]}})
    assert not got.get("isError"), got
    assert got["structuredContent"]["version"] == 1
    stale = call({"action": "adopt_tasks",
                  "body": {"expected_version": 0, "tasks": [task("t2")]}})
    assert stale.get("isError") and "version 1" in stale["content"][0]["text"]
    read = call({"action": "get_suite", "body": {}})
    assert read["structuredContent"]["version"] == 1
    gone = call({"action": "remove_tasks", "body": {"expected_version": 1, "task_ids": ["t1"]}})
    assert gone["structuredContent"] == {"version": 2, "tasks": []}
