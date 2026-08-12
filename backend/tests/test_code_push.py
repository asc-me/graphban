"""AL-139: incremental local→cloud code-graph push — diff, incrementality, resumability,
staleness guard, and the trigger endpoint."""
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import CodeNode, CodeSyncState
from app.services import code_graph, code_sync


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class _Resp:
    def raise_for_status(self):
        pass


def _capture(monkeypatch, fail_on=None):
    """Patch the outbound POST; record request bodies. `fail_on=N` raises on the Nth call."""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        if fail_on is not None and len(calls) == fail_on:
            raise RuntimeError("network blip")
        return _Resp()

    monkeypatch.setattr(code_sync.httpx, "post", fake_post)
    return calls


def _describe(db, *paths):
    code_graph.describe_code(db, project_id="core", nodes=[
        {"path": p, "summary": f"summary of {p}", "content_hash": f"h-{p}"} for p in paths])
    db.commit()


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _paths_sent(calls):
    return sorted(n["path"] for c in calls if "nodes" in c for n in c["nodes"])


# ---- pure diff ----
def test_compute_diff():
    changed, removed = code_sync.compute_diff(
        local={"a": "1", "b": "2", "c": "3"}, pushed={"a": "1", "b": "OLD", "d": "9"})
    assert changed == ["b", "c"]  # b changed, c new
    assert removed == ["d"]       # d gone locally


# ---- incremental push ----
def test_first_push_sends_all_then_nothing(db, monkeypatch):
    _describe(db, "x.py", "y.py")
    calls = _capture(monkeypatch)

    r = code_sync.push(db, project_id="core", cloud_url="https://cloud", api_key="gb_sk_x")
    assert r["pushed"] == 2 and _paths_sent(calls) == ["x.py", "y.py"]
    assert set(db.get(CodeSyncState, "core").manifest) == {"x.py", "y.py"}

    calls2 = _capture(monkeypatch)  # nothing changed
    r2 = code_sync.push(db, project_id="core", cloud_url="https://cloud", api_key="gb_sk_x")
    assert r2["pushed"] == 0 and not any("nodes" in c for c in calls2)


def test_only_changed_node_ships(db, monkeypatch):
    _describe(db, "x.py", "y.py")
    _capture(monkeypatch)
    code_sync.push(db, project_id="core", cloud_url="https://c", api_key="k")

    code_graph.describe_code(db, project_id="core",
                             nodes=[{"path": "x.py", "summary": "NEW", "content_hash": "h2"}])
    db.commit()
    calls = _capture(monkeypatch)
    r = code_sync.push(db, project_id="core", cloud_url="https://c", api_key="k")
    assert r["pushed"] == 1 and _paths_sent(calls) == ["x.py"]


def test_removed_path_is_pruned(db, monkeypatch):
    _describe(db, "x.py", "y.py")
    _capture(monkeypatch)
    code_sync.push(db, project_id="core", cloud_url="https://c", api_key="k")

    y = db.scalars(select(CodeNode).where(CodeNode.project_id == "core", CodeNode.path == "y.py")).first()
    db.delete(y)
    db.commit()
    calls = _capture(monkeypatch)
    r = code_sync.push(db, project_id="core", cloud_url="https://c", api_key="k")
    assert r["removed"] == 1
    assert next(c for c in calls if "remove" in c)["remove"] == ["y.py"]
    assert "y.py" not in db.get(CodeSyncState, "core").manifest


def test_resumable_after_a_failed_batch(db, monkeypatch):
    _describe(db, "a.py", "b.py", "c.py")
    _capture(monkeypatch, fail_on=2)  # 1st node confirmed, 2nd batch fails
    with pytest.raises(RuntimeError):
        code_sync.push(db, project_id="core", cloud_url="https://c", api_key="k", batch_size=1)
    assert len(db.get(CodeSyncState, "core").manifest) == 1  # only the confirmed batch persisted

    calls = _capture(monkeypatch)  # resume — only the unconfirmed nodes ship
    r = code_sync.push(db, project_id="core", cloud_url="https://c", api_key="k", batch_size=1)
    assert r["pushed"] == 2 and len(_paths_sent(calls)) == 2


def test_push_without_a_link_raises_notlinked(db):
    with pytest.raises(code_sync.NotLinked):
        code_sync.push(db, project_id="core", cloud_url="", api_key="")


# ---- trigger endpoint ----
def _link(monkeypatch, url="https://cloud", key="gb_sk_x"):
    monkeypatch.setattr(code_sync.settings, "sync_cloud_url", url)
    monkeypatch.setattr(code_sync.settings, "sync_api_key", key)


def test_trigger_push_endpoint(client, auth, monkeypatch):
    _capture(monkeypatch)
    _link(monkeypatch)
    r = client.post("/api/sync/push", json={"project_id": "core"}, headers=auth)
    assert r.status_code == 200 and r.json()["project_id"] == "core"


def test_trigger_push_not_linked_is_409(client, auth, monkeypatch):
    _link(monkeypatch, url="", key="")
    r = client.post("/api/sync/push", json={"project_id": "core"}, headers=auth)
    assert r.status_code == 409


def test_trigger_push_requires_write(client, monkeypatch):
    _link(monkeypatch)
    ops = _login(client, "ops@ascme-labs.com")  # read-only on core
    r = client.post("/api/sync/push", json={"project_id": "core"}, headers=ops)
    assert r.status_code == 403


# ---- privacy (D8) + purge ----
def test_push_skipped_when_graph_privacy_is_on(client, auth, db, monkeypatch):
    _describe(db, "x.py")
    client.patch("/api/platform", json={"sync_graph": False}, headers=auth)
    db.expire_all()  # re-read the flag committed by the PATCH
    calls = _capture(monkeypatch)
    r = code_sync.push(db, project_id="core", cloud_url="https://c", api_key="k")
    assert r.get("skipped") is True and calls == []  # nothing left the box


class _PurgeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"project_id": "core", "deleted_nodes": 3, "deleted_edges": 1}


def test_trigger_purge_endpoint(client, auth, monkeypatch):
    monkeypatch.setattr(code_sync.httpx, "request", lambda *a, **k: _PurgeResp())
    _link(monkeypatch)
    r = client.post("/api/sync/purge", json={"project_id": "core"}, headers=auth)
    assert r.status_code == 200 and r.json()["deleted_nodes"] == 3


def test_trigger_purge_not_linked_is_409(client, auth, monkeypatch):
    _link(monkeypatch, url="", key="")
    assert client.post("/api/sync/purge", json={"project_id": "core"}, headers=auth).status_code == 409
