"""GRPH-650: on-demand LLM judge for the Memory review queue.

The write-path judge (AL-227) already scores on add_memory. This is the human-checks
side: POST /memory/shards/{id}/judge returns a verdict or a cause, never mutates, and
GET /candidates/scored stays similarity-only while the judge toggle is off.
GRPH-79 asks review_judge on that GET when the toggle is on.
"""
from app.services import memory as mem_svc
from app.services.platform import Resolved


def _mcp(client, key, tool, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]["structuredContent"]


def _key(client, auth, **body):
    return client.post("/api/api-keys", json={"name": "mem", **body}, headers=auth).json()["plaintext"]


def _proj(client, auth, name):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


class _FakeChat:
    def __init__(self, reply: str):
        self._reply = reply
        self.calls = 0

    def chat(self, *, system: str, context: str, question: str,
             temperature: float | None = None) -> str:
        self.calls += 1
        return self._reply


def _patch_judge(monkeypatch, reply: str) -> _FakeChat:
    fake = _FakeChat(reply)
    from app.services import platform as platform_svc
    monkeypatch.setattr(
        platform_svc, "resolve_chat",
        lambda db, pid: Resolved("anthropic", fake),
    )
    return fake


def _candidate(client, auth, name, text="always pin the pgvector image to pg16 in CI"):
    pid = _proj(client, auth, name)
    key = _key(client, auth, project_id=pid)
    s = _mcp(client, key, "add_memory", {"text": text})
    assert s["status"] == "candidate"
    return pid, s


def test_scored_list_does_not_call_the_chat_model_when_judge_is_off(client, auth, monkeypatch):
    """Default page load stays similarity-only. GRPH-79 asks `review_judge` on GET
    scored only when `memory_llm_judge` is on (capped). POST /judge remains the
    on-demand re-ask (keep/quality). This pin is the cheap default, not a ban
    on the review judge."""
    pid, _s = _candidate(client, auth, "ScoredNoChat")

    from app.services import platform as platform_svc
    calls = {"n": 0}
    real = platform_svc.resolve_chat

    def wrapped(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(platform_svc, "resolve_chat", wrapped)
    r = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth)
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert calls["n"] == 0, "GET scored resolved a chat model while the judge toggle is off"


def test_advisory_judge_off_is_unavailable_not_a_score(client, auth):
    pid, s = _candidate(client, auth, "JudgeOff")
    r = client.post(f"/api/memory/shards/{s['id']}/judge", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] is None
    assert body["cause"] == "judge_off"
    assert "similarity" in body["cause_detail"]
    cands = client.get(f"/api/memory/candidates?project_id={pid}", headers=auth).json()
    assert any(c["id"] == s["id"] and c["status"] == "candidate" for c in cands)


def test_advisory_judge_stub_is_unavailable_not_a_score(client, auth):
    pid, s = _candidate(client, auth, "JudgeStubOnDemand")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    r = client.post(f"/api/memory/shards/{s['id']}/judge", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] is None
    assert body["cause"] == "no_provider"
    assert "quality" not in (body.get("verdict") or {})


def test_advisory_judge_returns_verdict_and_does_not_mutate(client, auth, monkeypatch):
    pid, s = _candidate(client, auth, "JudgeAsk")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    _patch_judge(monkeypatch, '{"keep": true, "quality": 0.9, "reason": "durable specific convention"}')
    r = client.post(f"/api/memory/shards/{s['id']}/judge", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["cause"] is None
    assert body["verdict"]["keep"] is True
    assert abs(body["verdict"]["quality"] - 0.9) < 1e-6
    assert "durable" in body["verdict"]["reason"]
    # Still a candidate — advisory only.
    scored = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()
    assert any(row["shard"]["id"] == s["id"] for row in scored)
    assert all(row["shard"]["status"] == "candidate" for row in scored if row["shard"]["id"] == s["id"])


def test_advisory_judge_published_is_not_a_score(client, auth, monkeypatch):
    pid, s = _candidate(client, auth, "JudgePublished")
    client.post(f"/api/memory/shards/{s['id']}/publish", headers=auth)
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    _patch_judge(monkeypatch, '{"keep": true, "quality": 0.99, "reason": "great"}')
    r = client.post(f"/api/memory/shards/{s['id']}/judge", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] is None
    assert body["cause"] == "not_candidate"


def test_advisory_judge_unknown_shard_404(client, auth):
    r = client.post("/api/memory/shards/does-not-exist/judge", headers=auth)
    assert r.status_code == 404


def test_judge_off_is_not_in_judge_causes():
    """agent_publish reads JUDGE_CAUSES. Mixing judge_off in would tell an operator
    the model is missing when they left the toggle off."""
    assert "judge_off" not in mem_svc.JUDGE_CAUSES
    assert "not_candidate" not in mem_svc.JUDGE_CAUSES
    assert "judge_off" in mem_svc.ADVISORY_CAUSES
