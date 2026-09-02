"""LLM review-judge signals on the memory review queue (GRPH-79).

AL-151 already scores candidates by similarity. This layer adds what similarity
cannot: groundedness (contradiction) and readiness (specific/durable). Stub and
a split judge are ungraded, never a fabricated 0. Similarity vetoes still win
without spending a judge call.
"""
import pytest

from app.services import memory as mem_svc
from app.services.platform import Resolved


def _proj(client, auth, name):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


def _key(client, auth, **body):
    return client.post("/api/api-keys", json={"name": "mem", **body},
                       headers=auth).json()["plaintext"]


def _mcp(client, key, tool, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]["structuredContent"]


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_scored_queue_is_ungraded_when_the_judge_is_off(client, auth):
    pid = _proj(client, auth, "JudgeOff")
    key = _key(client, auth, project_id=pid)
    _mcp(client, key, "add_memory", {"text": "the flux capacitor prefers 1.21 gigawatts"})
    scored = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()
    assert len(scored) == 1
    assert scored[0]["judged"] is False
    assert scored[0]["grounded"] is None
    assert "off" in scored[0]["ungraded_reason"]


def test_scored_queue_is_ungraded_on_stub_even_when_the_judge_is_on(client, auth):
    pid = _proj(client, auth, "JudgeStub")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _mcp(client, key, "add_memory", {"text": "the flux capacitor prefers 1.21 gigawatts"})
    scored = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()
    assert scored[0]["judged"] is False
    assert scored[0]["grounded"] is None
    assert "no independent chat model" in scored[0]["ungraded_reason"]


def test_similarity_veto_does_not_call_the_review_judge(client, auth, db, monkeypatch):
    pid = _proj(client, auth, "JudgeVeto")
    client.patch(
        f"/api/projects/{pid}",
        json={"memory_llm_judge": True, "memory_auto_reject": False},
        headers=auth,
    )
    key = _key(client, auth, project_id=pid)
    pub = _mcp(client, key, "add_memory", {"text": "prefer idempotency keys on writes"})
    client.post(f"/api/memory/shards/{pub['id']}/publish", headers=auth)
    _mcp(client, key, "add_memory", {"text": "prefer idempotency keys on writes"})

    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("review judge must not run on a similarity veto")

    monkeypatch.setattr(mem_svc, "review_judge", boom)
    scored = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()
    assert scored[0]["suggestion"] == "reject"
    assert called["n"] == 0
    assert scored[0]["judged"] is False


def test_ungrounded_demotes_an_accept_to_review(client, auth, monkeypatch):
    pid = _proj(client, auth, "JudgeUngrounded")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    text = "always set a timeout on outbound http"
    for _ in range(3):
        _mcp(client, key, "add_memory", {"text": text})

    def ungrounded(db, shard, **k):
        return {"grounded": False, "ready": True, "conflicts": ["published: no timeouts"],
                "reason": "contradicts published memory", "samples": 3}, "ok"

    monkeypatch.setattr(mem_svc, "review_judge", ungrounded)
    scored = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()
    accepts = [s for s in scored if s["suggestion"] == "accept"]
    reviews = [s for s in scored if s["suggestion"] == "review" and s["judged"]]
    assert accepts == []
    assert reviews
    assert reviews[0]["grounded"] is False
    assert reviews[0]["conflicts"]
    # Ungrounded rows sort first.
    assert scored[0]["grounded"] is False


def test_the_scored_endpoint_calls_review_judge(client, auth, monkeypatch):
    """Sabotage the CALL: attaching fields without asking the judge would look judged."""
    pid = _proj(client, auth, "JudgeCall")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _mcp(client, key, "add_memory", {"text": "the flux capacitor prefers 1.21 gigawatts"})
    called = {"n": 0}

    def fake(db, shard, **k):
        called["n"] += 1
        return {"grounded": True, "ready": True, "conflicts": [],
                "reason": "specific and consistent", "samples": 3}, "ok"

    monkeypatch.setattr(mem_svc, "review_judge", fake)
    scored = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()
    assert called["n"] == 1
    assert scored[0]["judged"] is True
    assert scored[0]["grounded"] is True
    assert scored[0]["ready"] is True
    assert scored[0]["ungraded_reason"] == ""
