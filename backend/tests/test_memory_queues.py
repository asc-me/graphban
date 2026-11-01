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


def _refresh(pid):
    db = SessionLocal()
    try:
        return mem_svc.refresh_judgements(db, pid)
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
    _refresh(pid)  # the GET reads stored verdicts; the background pass is what asks
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


def _report_wrong(shard_id, *, days_ago=0):
    from app.models import LessonOutcome
    db = SessionLocal()
    try:
        db.add(LessonOutcome(shard_id=shard_id, kind="contradicted", source="human",
                             created_at=datetime.now(timezone.utc) - timedelta(days=days_ago)))
        db.commit()
    finally:
        db.close()


def test_keep_clears_a_reported_wrong_shard_until_reported_again(client, auth):
    """Bounce: Keep must clear the wrong-report reason too, not only the age clock."""
    pid = _proj(client, auth, "KeepWrong")
    sid = _published(client, auth, pid)
    _report_wrong(sid, days_ago=1)
    stale = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()
    assert [r["shard"]["id"] for r in stale] == [sid]
    assert client.post(f"/api/memory/shards/{sid}/keep", headers=auth).status_code == 200
    assert client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json() == []
    assert client.get(f"/api/memory/queues?project_id={pid}", headers=auth).json()["stale_or_wrong"]["count"] == 0
    _report_wrong(sid)  # a fresh report after the Keep puts it back
    stale = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()
    assert [r["shard"]["id"] for r in stale] == [sid]
    assert stale[0]["why"] == ["reported wrong or contradicted 1x"]


def test_keep_clears_a_shard_whose_origin_code_is_gone(client, auth):
    from app.models import CodeNode
    from app.services import items as items_svc
    pid = _proj(client, auth, "KeepGone")
    sid = _published(client, auth, pid)
    db = SessionLocal()
    try:
        item = items_svc.create_item(db, title="origin", project_id=pid,
                                     touchpoints=["app/services/memory.py"])
        db.add(CodeNode(id="cn_keepgone", project_id=pid, path="web/src/App.tsx",
                        kind="file", name="App"))
        db.get(MemoryShard, sid).item_id = item.id
        db.commit()
    finally:
        db.close()
    stale = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()
    assert stale[0]["why"] == ["the code it came from no longer exists"]
    assert client.post(f"/api/memory/shards/{sid}/keep", headers=auth).status_code == 200
    assert client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json() == []


def _shard_with_node(client, auth, name, *, content_hash="h1", setting=None):
    """A published shard whose item cites app/x.py, published while the code-graph node
    behind it carries `content_hash` — so publish takes that as the baseline."""
    from app.services import code_graph as cg_svc
    from app.services import items as items_svc
    pid = _proj(client, auth, name)
    if setting is not None:
        client.patch(f"/api/projects/{pid}", json={"memory_flag_on_code_change": setting}, headers=auth)
    db = SessionLocal()
    try:
        item = items_svc.create_item(db, title="origin", project_id=pid, touchpoints=["app/x.py"])
        cg_svc.upsert_node(db, project_id=pid, path="app/x.py", name="x",
                           content_hash=content_hash, revision="aaa111")
        db.commit()
        item_id = item.id
    finally:
        db.close()
    r = client.post("/api/memory/shards", json={"text": "pin pg16 in CI", "project_id": pid,
                                                "item_id": item_id}, headers=auth)
    assert r.status_code == 201, r.text
    return pid, r.json()["id"]


def _redescribe(pid, *, content_hash, revision="bbb222", summary="re-described"):
    from app.services import code_graph as cg_svc
    db = SessionLocal()
    try:
        cg_svc.upsert_node(db, project_id=pid, path="app/x.py", name="x", summary=summary,
                           content_hash=content_hash, revision=revision)
        db.commit()
    finally:
        db.close()


def _state(sid):
    db = SessionLocal()
    try:
        return mem_svc.origin_path_state(db, db.get(MemoryShard, sid))
    finally:
        db.close()


def _stale(client, auth, pid):
    return client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()


def test_code_changed_after_publish_is_flagged(client, auth):
    pid, sid = _shard_with_node(client, auth, "ChgOn")
    assert _state(sid) == "ok"
    _redescribe(pid, content_hash="h2")
    assert _state(sid) == "changed"
    assert [r["why"] for r in _stale(client, auth, pid)] == [["cited code changed since publish"]]


def test_redescribe_with_the_same_hash_is_not_a_change(client, auth):
    # A describe pass at a new commit with identical source moves updated_at, revision and
    # summary — none of which is the code changing.
    pid, sid = _shard_with_node(client, auth, "ChgSame")
    _redescribe(pid, content_hash="h1", revision="ccc333", summary="a new summary")
    assert _state(sid) == "ok"
    assert _stale(client, auth, pid) == []


def test_code_changed_is_not_flagged_when_the_setting_is_off(client, auth):
    pid, sid = _shard_with_node(client, auth, "ChgOff", setting=False)
    _redescribe(pid, content_hash="h2")
    assert _state(sid) == "changed"
    assert _stale(client, auth, pid) == []


def test_no_change_signal_reports_unknown_not_ok(client, auth):
    # The node carries no content hash, so publish has no baseline to compare against.
    pid, sid = _shard_with_node(client, auth, "ChgUnk", content_hash="")
    assert _state(sid) == "unknown"
    _redescribe(pid, content_hash="")
    assert _state(sid) == "unknown"
    assert _stale(client, auth, pid) == []


def test_keep_clears_a_code_changed_flag(client, auth):
    pid, sid = _shard_with_node(client, auth, "ChgKeep")
    _redescribe(pid, content_hash="h2")
    assert _stale(client, auth, pid) != []
    assert client.post(f"/api/memory/shards/{sid}/keep", headers=auth).status_code == 200
    assert _state(sid) == "ok"
    assert _stale(client, auth, pid) == []
    # Kept against h2: a further change flags it again.
    _redescribe(pid, content_hash="h3")
    assert [r["why"] for r in _stale(client, auth, pid)] == [["cited code changed since publish"]]


# ---- GRPH-1227: counting a queue never waits on a model, and similarity is fast ----------------

def _vec(seed: int, dim: int):
    import random
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(dim)]


def _naive_best(vec, pool):
    """The pre-GRPH-1227 per-pair loop, kept as the oracle."""
    best, score = None, 0.0
    for shard, svec in pool:
        dot = sum(x * y for x, y in zip(vec, svec))
        na = sum(x * x for x in vec) ** 0.5
        nb = sum(y * y for y in svec) ** 0.5
        sim = 0.0 if na == 0 or nb == 0 else dot / (na * nb)
        if sim > score:
            best, score = shard, sim
    return best, score


def test_best_match_equals_the_per_pair_loop():
    plain = [(MemoryShard(id=f"s{i}"), _vec(i, 64)) for i in range(40)]
    plain.append((MemoryShard(id="zero"), [0.0] * 64))
    for pool in (plain, mem_svc.VecPool(plain)):
        for seed in (100, 101, 102):
            q = _vec(seed, 64)
            got, want = mem_svc._best_match(q, pool), _naive_best(q, pool)
            assert got[0] is want[0] and abs(got[1] - want[1]) < 1e-12
        assert mem_svc._best_match([0.0] * 64, pool) == (None, 0.0)
    assert mem_svc._best_match(plain[0][1], []) == (None, 0.0)
    assert mem_svc._best_match(plain[0][1], mem_svc.VecPool()) == (None, 0.0)


def test_vec_pool_append_is_not_served_a_stale_matrix():
    pool = mem_svc.VecPool([(MemoryShard(id="a"), [1.0, 0.0])])
    assert mem_svc._best_match([0.0, 1.0], pool) == (None, 0.0)
    b = MemoryShard(id="b")
    pool.append((b, [0.0, 1.0]))
    got = mem_svc._best_match([0.0, 1.0], pool)
    assert got[0] is b and abs(got[1] - 1.0) < 1e-12


def test_score_candidates_hands_the_scorer_cached_pools(client, auth, monkeypatch):
    """The speed below only holds for a `VecPool`: a plain list is re-normalised on every
    call, which on the CI runner is as slow as the loop it replaced."""
    pid = _proj(client, auth, "QPools")
    _seed_candidates(pid)
    seen = []
    real = mem_svc._score_shard

    def spy(cv, published, rejected, *a, **kw):
        seen.append((type(published), type(rejected), type(a[2] if len(a) > 2 else kw.get("corroborating"))))
        return real(cv, published, rejected, *a, **kw)

    monkeypatch.setattr(mem_svc, "_score_shard", spy)
    db = SessionLocal()
    try:
        mem_svc.score_candidates(db, project_id=pid, judge=False)
    finally:
        db.close()
    assert seen and all(t == (mem_svc.VecPool,) * 3 for t in seen)


def test_similarity_pass_is_fast_at_real_scale():
    """~1,300 published x 80 candidates at 1024 dims took ~34s in pure Python. Timed on the
    pool type `score_candidates` actually passes (see the test above)."""
    import time
    pool = mem_svc.VecPool((MemoryShard(id=f"s{i}"), _vec(i, 1024)) for i in range(1300))
    cands = [_vec(10_000 + i, 1024) for i in range(80)]
    t0 = time.monotonic()
    for c in cands:
        mem_svc._best_match(c, pool)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"similarity pass took {elapsed:.2f}s"


def _seed_candidates(pid):
    """Near-duplicate, conflicting-candidate and novel rows, with chosen embeddings."""
    db = SessionLocal()
    try:
        dim = len(db.scalar(__import__("sqlalchemy").select(MemoryShard.embedding)
                            .where(MemoryShard.embedding.is_not(None)).limit(1)) or _vec(0, 384))
        base = _vec(1, dim)
        ids = {}
        for name, status, emb in (
            ("pub", "published", base),
            ("dup", "candidate", [x + 1e-4 for x in base]),
            ("novel", "candidate", _vec(2, dim)),
            ("other", "candidate", _vec(3, dim)),
        ):
            s = mem_svc.add_memory(db, text_body=f"{name} lesson text", project_id=pid,
                                   status=status, auto_triage=False)
            s.embedding = emb
            if name == "other":
                s.review_judge_verdict = {"grounded": True, "ready": True, "reason": "x",
                                          "conflicts": ["pub"]}
                s.review_judge_key = "stale"
            ids[name] = s.id
        db.commit()
        return ids
    finally:
        db.close()


def test_queue_counts_matches_score_candidates_membership(client, auth):
    pid = _proj(client, auth, "QSame", judge=True)
    _seed_candidates(pid)
    db = SessionLocal()
    try:
        full = mem_svc.score_candidates(db, project_id=pid)
        counted = mem_svc.score_candidates(db, project_id=pid, judge=False)
        key = lambda rows: sorted((r["shard"].id, r["suggestion"], r["duplicate_of"]) for r in rows)  # noqa: E731
        assert key(full) == key(counted) and len(full) == 3
        dup = next(r for r in counted if r["shard"].text.startswith("dup"))
        assert dup["suggestion"] == "reject" and dup["duplicate_of"]
        counts = mem_svc.queue_counts(db, pid)
        assert counts["needs_decision"]["count"] == len(full)
        assert counts["conflicts"]["count"] == 1  # the stored verdict, read without a call
    finally:
        db.close()


def test_queue_counts_never_calls_the_review_judge(client, auth, monkeypatch):
    pid = _proj(client, auth, "QNoJudge", judge=True)
    _seed_candidates(pid)

    def boom(*a, **k):
        raise AssertionError("queue_counts must not call the review judge")

    monkeypatch.setattr(mem_svc, "review_judge", boom)
    body = client.get(f"/api/memory/queues?project_id={pid}", headers=auth).json()
    assert body["needs_decision"] == {"count": 3, "state": "ok", "error": ""}


# ---- GRPH-1242: the memory pages never wait on a model ----------------------------------

# One reply both parsers accept: keep/quality for the stale queue, grounded/ready for review.
_BOTH = ('{"keep": true, "quality": 0.95, "grounded": true, "ready": true, '
         '"conflicts": [], "reason": "still right"}')


class _CountingChat:
    def __init__(self, reply: str = _BOTH, exc: Exception | None = None):
        self.reply, self.exc, self.calls = reply, exc, 0

    def chat(self, *, system, context, question, temperature=None) -> str:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.reply


def _chat(monkeypatch, chat):
    from app.services import platform as platform_svc
    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("anthropic", chat))


def _candidate(client, auth, pid, text):
    key = client.post("/api/api-keys", json={"name": "mem", "project_id": pid},
                      headers=auth).json()["plaintext"]
    body = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "add_memory", "arguments": {"text": text}}},
        headers={"X-API-Key": key},
    ).json()["result"]["structuredContent"]
    assert body["status"] == "candidate"
    return body["id"]


def _count_judges(monkeypatch):
    """Count every judge entry point, so a decider path is counted as well as a chat one."""
    n = {"keep": 0, "review": 0}
    keep, review = mem_svc.judge_verdict, mem_svc.review_judge

    def counted_keep(db, shard):
        n["keep"] += 1
        return keep(db, shard)

    def counted_review(db, shard, **k):
        n["review"] += 1
        return review(db, shard, **k)

    monkeypatch.setattr(mem_svc, "judge_verdict", counted_keep)
    monkeypatch.setattr(mem_svc, "review_judge", counted_review)
    return n


def _stale_project(client, auth, name, n=2):
    pid = _proj(client, auth, name, judge=True)
    sids = [_published(client, auth, pid, text=f"lesson {i}: pin pgvector to pg16 in CI job {i}")
            for i in range(n)]
    for sid in sids:
        _age(sid, created_days=200)
    return pid, sids


def test_the_pages_answer_from_stored_verdicts_while_the_judge_raises(client, auth, monkeypatch):
    pid, sids = _stale_project(client, auth, "JudgeDown")
    cid = _candidate(client, auth, pid, "retry budgets reset at midnight UTC, not local time")
    _chat(monkeypatch, _CountingChat())
    monkeypatch.setattr(mem_svc, "STALE_JUDGE_MAX", 1)  # leave one stale row unjudged
    _refresh(pid)

    boom = _CountingChat(exc=RuntimeError("model down"))
    _chat(monkeypatch, boom)
    r = client.get(f"/api/memory/stale?project_id={pid}", headers=auth)
    assert r.status_code == 200
    rows = {row["shard"]["id"]: row for row in r.json()}
    assert set(rows) == set(sids)
    judged = [row for row in rows.values() if not row["ungraded"]]
    unjudged = [row for row in rows.values() if row["ungraded"]]
    assert len(judged) == 1 and len(unjudged) == 1
    assert judged[0]["recommendation"]["action"] == "keep"
    assert unjudged[0]["recommendation"] is None
    assert "not yet judged" in unjudged[0]["ungraded_reason"]

    r = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth)
    assert r.status_code == 200
    row = next(x for x in r.json() if x["shard"]["id"] == cid)
    assert row["judged"] is True and row["grounded"] is True and row["ready"] is True
    assert boom.calls == 0


def test_the_refresh_fills_a_verdict_and_a_later_get_shows_it(client, auth, monkeypatch):
    pid, (sid,) = _stale_project(client, auth, "JudgeFill", n=1)
    cid = _candidate(client, auth, pid, "the ingest runner must dedupe on origin, not text")
    _chat(monkeypatch, _CountingChat())

    before = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()
    assert before[0]["ungraded"] is True and "not yet judged" in before[0]["ungraded_reason"]
    cand = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()
    assert next(x for x in cand if x["shard"]["id"] == cid)["judged"] is False

    assert _refresh(pid) == 1

    after = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()
    assert after[0]["ungraded"] is False
    assert after[0]["recommendation"]["action"] == "keep"
    cand = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()
    assert next(x for x in cand if x["shard"]["id"] == cid)["judged"] is True


def test_the_pages_make_zero_model_calls_per_request(client, auth, monkeypatch):
    """Sabotage: put `judge_verdict` back into `recommendations` on the GET (or score the
    queue with `judge=True`) and the counts below go non-zero."""
    pid, _ = _stale_project(client, auth, "JudgeZero", n=3)
    _candidate(client, auth, pid, "migrations add columns nullable first, backfill second")
    chat = _CountingChat()
    _chat(monkeypatch, chat)
    n = _count_judges(monkeypatch)
    for _ in range(3):
        assert client.get(f"/api/memory/stale?project_id={pid}", headers=auth).status_code == 200
        assert client.get(f"/api/memory/candidates/scored?project_id={pid}",
                          headers=auth).status_code == 200
    assert n == {"keep": 0, "review": 0} and chat.calls == 0
    # The counter can count: the refresh is where the asking went.
    _refresh(pid)
    assert n["keep"] == 3 and n["review"] >= 1 and chat.calls > 0


def test_a_second_refresh_over_a_judged_queue_asks_nothing(client, auth, monkeypatch):
    pid, _ = _stale_project(client, auth, "JudgeCached")
    _candidate(client, auth, pid, "feature flags default off in the test suite")
    chat = _CountingChat()
    _chat(monkeypatch, chat)
    _refresh(pid)
    spent = chat.calls
    assert spent > 0
    _refresh(pid)
    assert chat.calls == spent


def test_stored_verdicts_leave_queue_and_decisions_unchanged(client, auth, monkeypatch):
    """Once verdicts are stored, the page's read-only scoring is the judging pass's answer."""
    pid, _ = _stale_project(client, auth, "JudgeSame")
    for t in ("dedupe candidates on origin before text", "cache keys include the model name"):
        _candidate(client, auth, pid, t)
    _chat(monkeypatch, _CountingChat())
    db = SessionLocal()
    try:
        judged = mem_svc.score_candidates(db, project_id=pid, judge=True)
        stored = mem_svc.score_candidates(db, project_id=pid, judge=False)
    finally:
        db.close()

    def shape(rows):
        return [(r["shard"].id, r["suggestion"], r["confidence"], r.get("judged"),
                 r.get("grounded"), r.get("ready")) for r in rows]

    assert shape(stored) == shape(judged)


def test_a_verdict_about_old_text_reads_as_not_yet_judged(client, auth, monkeypatch):
    pid, (sid,) = _stale_project(client, auth, "JudgeEdited", n=1)
    _chat(monkeypatch, _CountingChat())
    _refresh(pid)
    assert client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()[0]["ungraded"] is False
    db = SessionLocal()
    try:
        db.get(MemoryShard, sid).text = "pin pgvector to pg17 now"
        db.commit()
    finally:
        db.close()
    row = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()[0]
    assert row["ungraded"] is True and "not yet judged" in row["ungraded_reason"]


def test_a_project_with_no_judge_model_says_so_rather_than_not_yet(client, auth):
    pid, _ = _stale_project(client, auth, "JudgeStubStale", n=1)
    row = client.get(f"/api/memory/stale?project_id={pid}", headers=auth).json()[0]
    assert row["ungraded"] is True
    assert row["ungraded_reason"] == mem_svc.JUDGE_CAUSES["no_provider"]


def test_the_refresh_is_single_runner_on_postgres(client, auth, monkeypatch):
    from sqlalchemy import text

    from app.db import engine
    if engine.dialect.name != "postgresql":
        import pytest
        pytest.skip("the advisory lock is Postgres-only; SQLite is one process")
    pid, _ = _stale_project(client, auth, "JudgeLock", n=1)
    chat = _CountingChat()
    _chat(monkeypatch, chat)
    with engine.connect() as other, other.begin():
        assert other.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                             {"k": mem_svc._JUDGE_REFRESH_LOCK}).scalar()
        db = SessionLocal()
        try:
            assert mem_svc.run_judge_refresh(db) == 0
        finally:
            db.close()
    assert chat.calls == 0
    db = SessionLocal()
    try:
        assert mem_svc.run_judge_refresh(db) >= 1  # released with the holder's transaction
    finally:
        db.close()
    assert chat.calls > 0
