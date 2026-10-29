"""GRPH-1181: shard class (pitfall / convention / principle) — stored, patchable, listed."""
from app.db import SessionLocal
from app.models import MemoryShard
from app.services import memory as mem_svc
from test_memory_queues import _age, _published, _proj


def _patch(client, auth, sid, body):
    return client.patch(f"/api/memory/shards/{sid}", json=body, headers=auth)


def test_patch_stores_class_and_returns_it_on_shard_and_list_rows(client, auth):
    pid = _proj(client, auth, "ClassStore")
    sid = _published(client, auth, pid)
    r = _patch(client, auth, sid, {"shard_class": "pitfall"})
    assert r.status_code == 200, r.text
    assert r.json()["shard_class"] == "pitfall"
    listed = client.get(f"/api/memory/shards?project_id={pid}", headers=auth).json()
    assert [s["shard_class"] for s in listed if s["id"] == sid] == ["pitfall"]
    _age(sid, created_days=200)
    stale = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()
    assert stale[0]["shard"]["shard_class"] == "pitfall"
    # Clearing sets it back to unclassified.
    assert _patch(client, auth, sid, {"shard_class": ""}).json()["shard_class"] == ""


def test_unknown_class_is_refused_with_422(client, auth):
    pid = _proj(client, auth, "ClassBad")
    sid = _published(client, auth, pid)
    assert _patch(client, auth, sid, {"shard_class": "rumour"}).status_code == 422
    assert _patch(client, auth, sid, {}).status_code == 422


def test_class_change_does_not_reembed(client, auth, monkeypatch):
    pid = _proj(client, auth, "ClassEmbed")
    sid = _published(client, auth, pid)
    calls = []
    monkeypatch.setattr(mem_svc, "safe_embed", lambda t: calls.append(t))
    assert _patch(client, auth, sid, {"shard_class": "convention"}).status_code == 200
    assert calls == []
    assert _patch(client, auth, sid, {"text": "new words"}).status_code == 200
    assert calls == ["new words"]


def test_unclassified_shard_has_empty_class(client, auth):
    pid = _proj(client, auth, "ClassNone")
    sid = _published(client, auth, pid)
    assert client.get(f"/api/memory/shards?project_id={pid}", headers=auth).json()[0]["shard_class"] == ""
    db = SessionLocal()
    try:
        assert db.get(MemoryShard, sid).shard_class == ""
    finally:
        db.close()
