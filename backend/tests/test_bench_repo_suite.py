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
    assert r.json()["detail"]["current_version"] == 1
    assert [t["id"] for t in _suite(client, env)["tasks"]] == ["t1"]
    r = _remove(client, env, ["t1"], 0)
    assert r.status_code == 409 and r.json()["detail"]["current_version"] == 1
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


def test_the_version_check_is_what_refuses(client, env, monkeypatch):
    """Sabotage target: with the compare skipped, the stale adopt is no longer refused."""
    assert _adopt(client, env, [task("t1")], 0).status_code == 200
    assert _adopt(client, env, [task("t2")], 0).status_code == 409
