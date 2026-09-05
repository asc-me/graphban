"""The review judge answers a question once (follow-up to #615's measurements).

One GET of the memory review queue could ask the chat model up to 24 times —
`REVIEW_JUDGE_MAX` (8) candidates x `JUDGE_SAMPLES` (3) — uncached, on every load.

Measured on ms-s1-ubt, the host serving the live grill: it serves exactly one request at
a time. Eight concurrent 64-token generations complete at 4.2s, 8.4, 12.6, 16.9, 21.1,
25.3, 29.5, 33.7 — perfectly linear at every depth. So the sweep holds the only slot for
~100s, and an interactive grill arriving in that window waits behind all of it with a 90s
budget. Opening a page could time out somebody's grill.

The second bug is quieter and was never about latency: three fresh samples can split
where the last three agreed, so refreshing the queue could change a candidate's
suggestion while nothing about the candidate changed.
"""
from __future__ import annotations

import pytest

from app.models import MemoryShard
from app.services import memory as mem_svc
from app.services import platform as plat_svc
from app.services.platform import Resolved


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class _Chat:
    """A judge that answers, and counts how often it was asked."""

    model = "fake-judge"

    def __init__(self, reply='{"grounded": true, "ready": true, "conflicts": [], '
                             '"reason": "specific and consistent"}'):
        self.reply = reply
        self.calls = 0

    def chat(self, **kw):
        self.calls += 1
        return self.reply


def _judge(monkeypatch, chat) -> None:
    monkeypatch.setattr(plat_svc, "resolve_role",
                        lambda db, pid, role: Resolved(provider_id="fake", chat=chat,
                                                       credential_id=None,
                                                       fell_back_from=""))


def _shard(db, text: str, sid: str = "s_cache") -> MemoryShard:
    shard = MemoryShard(id=sid, project_id="core", text=text)
    db.add(shard)
    db.commit()
    return shard


def test_the_same_question_is_asked_once(client, auth, db, monkeypatch):
    chat = _Chat()
    _judge(monkeypatch, chat)
    shard = _shard(db, "always set a timeout on outbound http")

    first, cause_a = mem_svc.review_judge(db, shard)
    second, cause_b = mem_svc.review_judge(db, shard)

    assert cause_a == "ok" and cause_b == "cached"
    assert chat.calls == mem_svc.JUDGE_SAMPLES, (
        f"the judge was asked {chat.calls} times for one unchanged candidate"
    )
    assert first == second, "a cached verdict that differs from the one stored is not a cache"


def test_changing_what_the_judge_sees_asks_again(client, auth, db, monkeypatch):
    """The key is the CONTEXT, not the shard id. Published memory is part of what
    groundedness is judged against, so a new published note is a new question."""
    chat = _Chat()
    _judge(monkeypatch, chat)
    shard = _shard(db, "always set a timeout on outbound http")

    mem_svc.review_judge(db, shard, published_texts=[])
    baseline = chat.calls
    _, cause = mem_svc.review_judge(db, shard, published_texts=["never set timeouts"])

    assert cause == "ok"
    assert chat.calls == baseline + mem_svc.JUDGE_SAMPLES


def test_editing_the_candidate_asks_again(client, auth, db, monkeypatch):
    chat = _Chat()
    _judge(monkeypatch, chat)
    shard = _shard(db, "always set a timeout on outbound http")
    mem_svc.review_judge(db, shard)
    baseline = chat.calls

    shard.text = "always set a timeout on outbound http, default 10s"
    db.commit()
    _, cause = mem_svc.review_judge(db, shard)

    assert cause == "ok"
    assert chat.calls == baseline + mem_svc.JUDGE_SAMPLES


@pytest.mark.parametrize("reply,expected_cause", [
    ("not json at all", "unparseable"),
    ("No content was provided to rate.", "no_input"),
])
def test_a_failed_asking_is_not_cached(client, auth, db, monkeypatch, reply, expected_cause):
    """THE one that decides whether this is a cache or a bug.

    A judge that could not be reached, split with itself, or answered unparseably said
    something about that MOMENT. Storing it would make a transient failure a permanent
    property of the candidate — the oldest defect in this codebase, wearing a cache.
    """
    chat = _Chat(reply=reply)
    _judge(monkeypatch, chat)
    shard = _shard(db, "always set a timeout on outbound http")

    verdict, cause = mem_svc.review_judge(db, shard)
    assert verdict is None and cause == expected_cause
    assert shard.review_judge_key == "", "a failure was written to the cache"
    assert shard.review_judge_verdict is None

    # And the next round tries again rather than serving the failure back.
    before = chat.calls
    mem_svc.review_judge(db, shard)
    assert chat.calls > before


def test_a_split_judge_is_not_cached(client, auth, db, monkeypatch):
    """Unanimity is the bar. A split means no adjudication — not a stored verdict of
    'no adjudication', which would never be revisited."""
    replies = iter([
        '{"grounded": true, "ready": true, "conflicts": [], "reason": "a"}',
        '{"grounded": false, "ready": true, "conflicts": [], "reason": "b"}',
    ])

    class _Split(_Chat):
        def chat(self, **kw):
            self.calls += 1
            return next(replies)

    chat = _Split()
    _judge(monkeypatch, chat)
    shard = _shard(db, "always set a timeout on outbound http")

    verdict, cause = mem_svc.review_judge(db, shard)
    assert verdict is None and cause == "split"
    assert shard.review_judge_key == ""


def test_a_cache_hit_does_not_spend_the_cap(client, auth, db, monkeypatch):
    """The cap counts MODEL CALLS, not rows. Counting cached rows against it would mean a
    queue whose verdicts are all known keeps re-buying the same ones and never reaches the
    candidates nobody has judged yet."""
    chat = _Chat()
    _judge(monkeypatch, chat)
    shard = _shard(db, "always set a timeout on outbound http")
    mem_svc.review_judge(db, shard)

    verdict, cause = mem_svc.review_judge(db, shard, allow_call=False)

    assert cause == "cached", "a known verdict was withheld because the budget was spent"
    assert verdict is not None


def test_an_unknown_verdict_past_the_cap_is_not_asked(client, auth, db, monkeypatch):
    """The control for the test above: `allow_call=False` still has to stop a real call."""
    chat = _Chat()
    _judge(monkeypatch, chat)
    shard = _shard(db, "a candidate nobody has judged")

    verdict, cause = mem_svc.review_judge(db, shard, allow_call=False)

    assert (verdict, cause) == (None, "capped")
    assert chat.calls == 0


def _proj(client, auth, name):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


def test_a_second_look_at_the_queue_judges_what_is_still_unknown(client, auth, monkeypatch):
    """The whole point, driven through the endpoint the reviewer actually loads.

    The cap bounds MODEL CALLS per pass, so a queue longer than the cap used to be stuck:
    every load re-bought the same first eight verdicts and the ninth candidate was never
    judged, however many times you opened the page. With verdicts remembered, a second
    look costs only the candidates nobody has judged yet.
    """
    chat = _Chat()
    _judge(monkeypatch, chat)
    pid = _proj(client, auth, "JudgeCap")
    client.patch(f"/api/projects/{pid}",
                 json={"memory_llm_judge": True, "memory_auto_reject": False}, headers=auth)
    key = client.post("/api/api-keys", json={"name": "mem", "project_id": pid},
                      headers=auth).json()["plaintext"]
    extra = 2
    for i in range(mem_svc.REVIEW_JUDGE_MAX + extra):
        client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "add_memory",
                                     "arguments": {"text": f"convention {i}: prefer approach "
                                                           f"number {i} when wiring subsystem {i}"}}},
                    headers={"X-API-Key": key})

    first = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()
    after_first = chat.calls
    assert after_first == mem_svc.REVIEW_JUDGE_MAX * mem_svc.JUDGE_SAMPLES, (
        "the first pass should spend exactly its cap"
    )
    assert sum(1 for r in first if r["judged"]) == mem_svc.REVIEW_JUDGE_MAX

    second = client.get(f"/api/memory/candidates/scored?project_id={pid}", headers=auth).json()

    assert chat.calls - after_first == extra * mem_svc.JUDGE_SAMPLES, (
        f"the second pass spent {chat.calls - after_first} calls; cached rows are being "
        f"re-bought and the unjudged tail never gets reached"
    )
    assert sum(1 for r in second if r["judged"]) == mem_svc.REVIEW_JUDGE_MAX + extra
