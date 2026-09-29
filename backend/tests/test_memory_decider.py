"""PRD-45 S3 — memory adjudication on the decider.

Each of §13.4–7 is a test whose sabotage is named in the docstring. FakeDecider is
the only judge; nothing here talks to laya. Vetoes still win; an abstain is not a
reject; the queue names what it did not score; a moved shard says `decider`.
"""
from __future__ import annotations

import pytest

from app.providers.decide import Answer, Decision
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


class FakeDecider:
    """Answers keep/quality and grounded/ready/contradicts from constructor knobs."""

    def __init__(self, *, keep=0.99, quality=4.0, grounded=0.9, ready=0.9,
                 contradicts="none", contradicts_conf=0.95, model="typed-decisions"):
        self.keep = keep
        self.quality = quality
        self.grounded = grounded
        self.ready = ready
        self.contradicts = contradicts
        self.contradicts_conf = contradicts_conf
        self.model = model
        self.calls = 0

    def decide(self, *, state, questions):
        self.calls += 1
        keys = [q.key if hasattr(q, "key") else q for q in questions]
        answers = {}
        if "keep" in keys:
            answers["keep"] = Answer(value=float(self.keep), confidence=1.0)
        if "quality" in keys:
            answers["quality"] = Answer(value=float(self.quality), confidence=1.0)
        if "grounded" in keys:
            answers["grounded"] = Answer(value=float(self.grounded), confidence=1.0)
        if "ready" in keys:
            answers["ready"] = Answer(value=float(self.ready), confidence=1.0)
        if "contradicts" in keys:
            answers["contradicts"] = Answer(
                value=self.contradicts, confidence=self.contradicts_conf,
            )
        return Decision(answers=answers, model=self.model)


class BoomDecider:
    model = "typed-decisions"
    calls = 0

    def decide(self, *, state, questions):
        self.calls += 1
        raise RuntimeError("decider down")


def _patch_decider(monkeypatch, decider):
    from app.services import platform as platform_svc

    def fake_resolve(db, pid):
        return Resolved(
            provider_id="systemone",
            chat=None,
            model=getattr(decider, "model", "typed-decisions"),
            source="deployment",
            decider=decider,
        )

    monkeypatch.setattr(platform_svc, "resolve_decider", fake_resolve)
    return decider


def _patch_chat(monkeypatch, reply='{"keep": true, "quality": 0.99, "reason": "glowing"}'):
    """If the decider path leaked into chat, this model would answer — and the test would see it."""
    from app.services import platform as platform_svc

    class _Chat:
        calls = 0

        def chat(self, *, system, context, question, temperature=None):
            self.calls += 1
            return reply

    chat = _Chat()
    monkeypatch.setattr(
        platform_svc, "resolve_chat",
        lambda db, pid: Resolved("anthropic", chat),
    )
    monkeypatch.setattr(
        platform_svc, "resolve_role",
        lambda db, pid, role: Resolved("anthropic", chat),
    )
    return chat


# ---- §13.4 vetoes --------------------------------------------------------------------


def test_resembles_rejected_does_not_call_the_decider(client, auth, monkeypatch):
    """A shard that resembles a rejected shard is rejected without the decider being called."""
    pid = _proj(client, auth, "DeciderVetoRej")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    bad = _mcp(client, key, "add_memory", {"text": "disable auth in dev to move faster"})
    client.post(f"/api/memory/shards/{bad['id']}/reject", headers=auth)

    decider = _patch_decider(monkeypatch, FakeDecider(keep=0.99, quality=4.0))
    again = _mcp(client, key, "add_memory", {"text": "disable auth in dev to move faster"})
    assert again["status"] == "rejected"
    assert again["scoring_source"] == "similarity"
    assert decider.calls == 0


def test_auto_extract_is_not_published_even_at_keep_99(client, auth, db, monkeypatch):
    """An `agent:auto-extract` shard the decider scores keep 0.99 · quality 4 is not
    auto-published. Sabotage: return keep 0.99 from a fake decider for every shard;
    no `auto_publish_shard` event may appear for an auto-extract origin."""
    pid = _proj(client, auth, "DeciderVetoExtract")
    client.patch(
        f"/api/projects/{pid}",
        json={"memory_llm_judge": True, "memory_write_mode": "auto"},
        headers=auth,
    )
    _patch_decider(monkeypatch, FakeDecider(keep=0.99, quality=4.0))

    shard = mem_svc.add_memory(
        db,
        text_body="always pin the pgvector image to pg16 in CI",
        scope="item",
        project_id=pid,
        source="lesson from X-1",
        status="candidate",
        origin="agent:auto-extract",
    )
    assert shard.status == "candidate"
    events = client.get(f"/api/events?project_id={pid}", headers=auth).json()["results"]
    assert not any(e["action"] == "auto_publish_shard" for e in events), (
        "the extract veto must hold even when the decider is certain"
    )


# ---- §13.5 abstain -------------------------------------------------------------------


def test_keep_inside_the_band_is_undecided_not_reject(client, auth, monkeypatch):
    """A keep inside the band yields cause: undecided and the similarity suggestion,
    not keep=False. Sabotage: collapse the band to a single threshold; this test
    must fail (keep 0.30 would become a reject)."""
    pid = _proj(client, auth, "DeciderAbstain")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    # 0.30 sits in the shipped band (0.15, 0.55). Collapsing KEEP_MIN == REJECT_MAX
    # would classify it as reject.
    _patch_decider(monkeypatch, FakeDecider(keep=0.30, quality=4.0))
    s = _mcp(client, key, "add_memory",
             {"text": "always pin the pgvector image to pg16 in CI"})
    assert s["status"] == "candidate"
    assert s["scoring_source"] != "decider"
    assert mem_svc.DECIDER_REJECT_MAX < 0.30 < mem_svc.DECIDER_KEEP_MIN


def test_undecided_ignores_quality(client, auth, db, monkeypatch):
    """Quality is unread when keep abstains — a missing score is still undecided."""
    pid = _proj(client, auth, "DeciderAbstainQ")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)

    class KeepOnly:
        model = "typed-decisions"

        def decide(self, *, state, questions):
            return Decision(answers={"keep": Answer(value=0.30)}, model=self.model)

    _patch_decider(monkeypatch, KeepOnly())
    shard = mem_svc.add_memory(
        db, text_body="a novel convention about timeouts",
        project_id=pid, status="candidate", auto_triage=False,
    )
    verdict, cause = mem_svc.judge_verdict(db, shard)
    assert verdict is None
    assert cause == "undecided"


# ---- §13.6 queue ---------------------------------------------------------------------


def test_twelve_candidates_are_all_scored_when_the_decider_is_fast(
        client, auth, monkeypatch):
    """With a decider resolved and twelve candidates, all twelve carry a score.
    Sabotage: restore REVIEW_JUDGE_MAX on the decider branch; only eight would
    be judged and this test must fail."""
    pid = _proj(client, auth, "DeciderQueue")
    client.patch(
        f"/api/projects/{pid}",
        json={"memory_llm_judge": True, "memory_auto_reject": False},
        headers=auth,
    )
    key = _key(client, auth, project_id=pid)
    n = mem_svc.REVIEW_JUDGE_MAX + 4
    assert n == 12
    for i in range(n):
        _mcp(client, key, "add_memory",
             {"text": f"unique flubber-{i} sprocket: never reuse token {i}xyz when assembling gadget {i}q"})
    _patch_decider(monkeypatch, FakeDecider())
    scored = client.get(
        f"/api/memory/candidates/scored?project_id={pid}", headers=auth,
    ).json()
    assert len(scored) == n
    judged = [r for r in scored if r["judged"]]
    assert len(judged) == n, (
        f"decider branch must score every candidate; got {len(judged)}/{n}. "
        "If REVIEW_JUDGE_MAX is back on this branch, the tail is silently capped."
    )
    assert all(r["judge_source"] == "decider" for r in judged)
    assert all(r["ungraded_reason"] == "" for r in judged)


def test_budget_remainder_is_named_unscored_not_silent(client, auth, monkeypatch):
    """Remainder of a spent budget is `unscored_budget`, never a quiet ungraded row."""
    pid = _proj(client, auth, "DeciderBudget")
    client.patch(
        f"/api/projects/{pid}",
        json={"memory_llm_judge": True, "memory_auto_reject": False},
        headers=auth,
    )
    key = _key(client, auth, project_id=pid)
    for i in range(3):
        _mcp(client, key, "add_memory",
             {"text": f"unique lesson {i} about subsystem {i} timeouts"})
    monkeypatch.setattr(mem_svc, "DECIDER_QUEUE_BUDGET_S", 0.0)
    _patch_decider(monkeypatch, FakeDecider())
    scored = client.get(
        f"/api/memory/candidates/scored?project_id={pid}", headers=auth,
    ).json()
    assert len(scored) == 3
    assert all(r["judged"] is False for r in scored)
    assert all("not scored yet" in r["ungraded_reason"] for r in scored)
    assert all("capped so a large queue" not in r["ungraded_reason"] for r in scored)


# ---- §13.7 naming --------------------------------------------------------------------


def test_a_shard_the_decider_moves_is_sourced_decider_and_names_the_head(
        client, auth, monkeypatch):
    """Every shard the decider moves has scoring_source = "decider" and an event
    naming the head. Sabotage: write "llm"; this test must fail."""
    pid = _proj(client, auth, "DeciderName")
    client.patch(
        f"/api/projects/{pid}",
        json={"memory_llm_judge": True, "memory_write_mode": "auto"},
        headers=auth,
    )
    key = _key(client, auth, project_id=pid)
    _patch_decider(monkeypatch, FakeDecider(keep=0.99, quality=4.0, model="typed-decisions"))
    s = _mcp(client, key, "add_memory",
             {"text": "always pin the pgvector image to pg16 in CI"})
    assert s["status"] == "published"
    assert s["scoring_source"] == "decider"
    events = client.get(f"/api/events?project_id={pid}", headers=auth).json()["results"]
    pub = [e for e in events if e["action"] == "auto_publish_shard"]
    assert pub, "the publish must be audited"
    assert pub[0]["meta"]["source"] == "decider"
    assert pub[0]["meta"]["head"] == "typed-decisions"


# ---- one judge per verdict; no silent chat fallback ---------------------------------


def test_a_resolved_decider_is_the_only_judge(client, auth, monkeypatch):
    pid = _proj(client, auth, "DeciderOnly")
    client.patch(
        f"/api/projects/{pid}",
        json={"memory_llm_judge": True, "memory_write_mode": "auto"},
        headers=auth,
    )
    key = _key(client, auth, project_id=pid)
    chat = _patch_chat(monkeypatch)
    decider = _patch_decider(monkeypatch, FakeDecider(keep=0.99, quality=4.0))
    s = _mcp(client, key, "add_memory",
             {"text": "always pin the pgvector image to pg16 in CI"})
    assert s["scoring_source"] == "decider"
    assert decider.calls == 1
    assert chat.calls == 0


def test_a_decider_error_does_not_fall_through_to_chat(client, auth, monkeypatch):
    pid = _proj(client, auth, "DeciderErr")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    chat = _patch_chat(monkeypatch)
    boom = BoomDecider()
    _patch_decider(monkeypatch, boom)
    s = _mcp(client, key, "add_memory",
             {"text": "always pin the pgvector image to pg16 in CI"})
    assert s["status"] == "candidate"
    assert s["scoring_source"] != "llm"
    assert boom.calls == 1
    assert chat.calls == 0


def test_low_keep_auto_rejects_with_decider_source(client, auth, monkeypatch):
    pid = _proj(client, auth, "DeciderReject")
    client.patch(f"/api/projects/{pid}", json={"memory_llm_judge": True}, headers=auth)
    key = _key(client, auth, project_id=pid)
    _patch_decider(monkeypatch, FakeDecider(keep=0.05, quality=0.0))
    s = _mcp(client, key, "add_memory", {"text": "the build felt slow today"})
    assert s["status"] == "rejected"
    assert s["scoring_source"] == "decider"
