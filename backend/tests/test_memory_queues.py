"""GRPH-1175: Memory triage queues — counts, stale-or-wrong with recommendations,
retire/restore with a 14-day window, apply-recommendations with a confidence floor."""
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import MemoryShard
from app.services import memory as mem_svc
from app.services.platform import Resolved


class _FakeChat:
    def __init__(self, reply: str):
        self._reply = reply

    def chat(self, *, system, context, question, temperature=None) -> str:
        return self._reply


def _proj(client, auth, name, judge=False):
    pid = client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]
    if judge:
        client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    return pid


def _judge(monkeypatch, reply):
    from app.services import platform as platform_svc
    monkeypatch.setattr(platform_svc, "resolve_chat",
                        lambda db, pid: Resolved("anthropic", _FakeChat(reply)))


def _published(client, auth, pid, text="pin the pgvector image to pg16 in CI"):
    r = client.post("/api/memory/shards", json={"text": text, "project_id": pid}, headers=auth)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "published"
    return r.json()["id"]


def _age(shard_id, *, created_days=None, retired_days=None):
    db = SessionLocal()
    try:
        s = db.get(MemoryShard, shard_id)
        now = datetime.now(timezone.utc)
        if created_days is not None:
            s.created_at = now - timedelta(days=created_days)
        if retired_days is not None:
            s.retired_at = now - timedelta(days=retired_days)
        db.commit()
    finally:
        db.close()


def test_queue_counts_in_one_call(client, auth):
    pid = _proj(client, auth, "QCounts")
    sid = _published(client, auth, pid)
    _age(sid, created_days=mem_svc.PUBLISHED_STALE_DAYS + 5)
    body = client.get(f"/api/memory/queues?project_id={pid}", headers=auth).json()
    assert set(body) == set(mem_svc.QUEUES)
    assert body["stale_or_wrong"] == {"count": 1, "state": "ok", "error": ""}
    assert body["needs_decision"]["count"] == 0


def test_a_failed_queue_is_named_not_counted_as_zero(client, auth, monkeypatch):
    pid = _proj(client, auth, "QFail")

    def boom(*a, **k):
        raise RuntimeError("stale scan exploded")

    monkeypatch.setattr(mem_svc, "stale_or_wrong", boom)
    body = client.get(f"/api/memory/queues?project_id={pid}", headers=auth).json()
    assert body["stale_or_wrong"]["state"] == "failed"
    assert body["stale_or_wrong"]["count"] is None
    assert "exploded" in body["stale_or_wrong"]["error"]
    assert body["needs_decision"]["state"] == "ok"


def test_stale_rows_carry_recommendations(client, auth, monkeypatch):
    pid = _proj(client, auth, "StaleRec", judge=True)
    sid = _published(client, auth, pid)
    _age(sid, created_days=200)
    _judge(monkeypatch, '{"keep": false, "quality": 0.05, "reason": "obsolete"}')
    rows = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()
    assert len(rows) == 1
    assert rows[0]["ungraded"] is False
    rec = rows[0]["recommendation"]
    assert rec["action"] == "retire" and rec["confidence"] >= 0.9
    assert rows[0]["why"]


def test_ungraded_judge_yields_ungraded_not_a_recommendation(client, auth):
    pid = _proj(client, auth, "StaleOff")  # judge off
    sid = _published(client, auth, pid)
    _age(sid, created_days=200)
    rows = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()
    assert rows[0]["ungraded"] is True
    assert rows[0]["recommendation"] is None
    assert rows[0]["ungraded_reason"]


def test_retire_then_restore_within_window(client, auth):
    pid = _proj(client, auth, "Retire")
    sid = _published(client, auth, pid)
    r = client.post(f"/api/memory/shards/{sid}/retire", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "retired"
    hits = client.post("/api/memory/search", json={"query": "pgvector", "project_id": pid}, headers=auth).json()
    assert all(h["shard"]["id"] != sid for h in hits) if hits and "shard" in hits[0] else True
    _age(sid, retired_days=13)
    r = client.post(f"/api/memory/shards/{sid}/restore", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "published"


def test_restore_refused_after_14_days(client, auth):
    pid = _proj(client, auth, "RetireFinal")
    sid = _published(client, auth, pid)
    client.post(f"/api/memory/shards/{sid}/retire", headers=auth)
    _age(sid, retired_days=15)
    r = client.post(f"/api/memory/shards/{sid}/restore", headers=auth)
    assert r.status_code == 409
    assert "final" in r.json()["detail"]
    assert r.json()["rule_id"] == "memory.retire_window"


def test_apply_recommendations_honours_the_floor(client, auth, monkeypatch):
    pid = _proj(client, auth, "Apply", judge=True)
    sid = _published(client, auth, pid)
    # retire at confidence 0.7: below the default 0.85 floor
    _judge(monkeypatch, '{"keep": false, "quality": 0.3, "reason": "probably obsolete"}')
    r = client.post("/api/memory/apply-recommendations", json={"shard_ids": [sid]}, headers=auth).json()
    assert r[0]["applied"] is False and r[0]["action"] == "retire"
    assert client.get(f"/api/memory/shards?project_id={pid}&status=published", headers=auth).json()
    # lower the floor and it applies, with an undo handle
    r = client.post("/api/memory/apply-recommendations",
                    json={"shard_ids": [sid], "min_confidence": 0.6}, headers=auth).json()
    assert r[0]["applied"] is True and r[0]["undo"] == "restore"
    assert client.get(f"/api/memory/shards?project_id={pid}&status=retired", headers=auth).json()


def test_apply_skips_ungraded(client, auth):
    pid = _proj(client, auth, "ApplyOff")
    sid = _published(client, auth, pid)
    r = client.post("/api/memory/apply-recommendations", json={"shard_ids": [sid]}, headers=auth).json()
    assert r[0]["applied"] is False and r[0]["reason"].startswith("ungraded")


def _events(action):
    from app.models import Event
    db = SessionLocal()
    try:
        return [(e.actor_id, e.target_id) for e in db.query(Event).filter(Event.action == action).all()]
    finally:
        db.close()


def test_keep_resets_staleness_and_leaves_published(client, auth):
    pid = _proj(client, auth, "Keep")
    sid = _published(client, auth, pid)
    _age(sid, created_days=mem_svc.PUBLISHED_STALE_DAYS + 5)
    assert [r["shard"]["id"] for r in client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()] == [sid]
    r = client.post(f"/api/memory/shards/{sid}/keep", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json() == []
    body = client.get(f"/api/memory/queues?project_id={pid}", headers=auth).json()
    assert body["stale_or_wrong"]["count"] == 0


def test_keep_refused_when_not_published(client, auth):
    pid = _proj(client, auth, "KeepRefused")
    sid = _published(client, auth, pid)
    client.post(f"/api/memory/shards/{sid}/retire", headers=auth)
    r = client.post(f"/api/memory/shards/{sid}/keep", headers=auth)
    assert r.status_code == 409
    assert r.json()["rule_id"] == "memory.retire_window"


def test_keep_records_an_event_naming_user_and_shard(client, auth):
    pid = _proj(client, auth, "KeepEvent")
    sid = _published(client, auth, pid)
    client.post(f"/api/memory/shards/{sid}/keep", headers=auth)
    events = _events("keep_shard")
    assert len(events) == 1 and events[0][0] and events[0][1] == sid


def test_apply_recommendations_keep_uses_the_keep_path(client, auth, monkeypatch):
    pid = _proj(client, auth, "ApplyKeep", judge=True)
    sid = _published(client, auth, pid)
    _age(sid, created_days=mem_svc.PUBLISHED_STALE_DAYS + 5)
    _judge(monkeypatch, '{"keep": true, "quality": 0.95, "reason": "still right"}')
    r = client.post("/api/memory/apply-recommendations", json={"shard_ids": [sid]}, headers=auth).json()
    assert r[0]["applied"] is True and r[0]["action"] == "keep"
    assert client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json() == []
    assert [e[1] for e in _events("keep_shard")] == [sid]
