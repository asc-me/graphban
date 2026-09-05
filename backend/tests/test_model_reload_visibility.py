"""What a call spent loading the model, and whether this box is paying for it.

Ollama reports `load_duration` on the same final line the adapter already parsed for
token counts, and it was thrown away. Measured on ms-s1-ubt while chasing #615: a cold
call spends 10.24s of an 11.86s call — 86% of it — and a warm call reports a real 0.0.

The point is not the number. `OLLAMA_KEEP_ALIVE` ships defaulting to "say nothing"
because RAM policy is the operator's, and nobody can choose 30m over 5m without knowing
whether their calls are arriving warm. A knob nobody can set well is worse than no knob;
this is the reading that makes it decidable.
"""
from __future__ import annotations

import httpx
import pytest

from app.providers import llm_meter, ollama


class _FakeStream:
    def __init__(self, lines): self._lines = lines
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_lines(self): yield from self._lines


DONE_COLD = ('{"message": {"content": ""}, "done": true, "prompt_eval_count": 359, '
             '"eval_count": 10, "load_duration": 10240000000, "total_duration": 11860000000}')
DONE_WARM = ('{"message": {"content": ""}, "done": true, "prompt_eval_count": 359, '
             '"eval_count": 10, "load_duration": 0, "total_duration": 670000000}')
DONE_SILENT = '{"message": {"content": ""}, "done": true, "eval_count": 10}'


def _chat(monkeypatch, done_line: str) -> dict:
    """One metered ollama call; returns what the sink collected."""
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(
        ['{"message": {"content": "answer"}}', done_line]))
    seen: dict = {}
    token = llm_meter._sink_var.set(seen)
    try:
        ollama.OllamaChat("https://gw.example", "m").chat(system="s", context="c", question="q")
    finally:
        llm_meter._sink_var.reset(token)
    return seen


def test_a_cold_call_reports_what_the_load_cost(monkeypatch):
    assert _chat(monkeypatch, DONE_COLD)["load_ms"] == 10240.0


def test_a_warm_call_reports_a_real_zero(monkeypatch):
    """0.0 is a measurement — the model was already resident. It is not the same fact as
    a server that never mentions loading, and the two must not collapse."""
    assert _chat(monkeypatch, DONE_WARM)["load_ms"] == 0.0


def test_a_server_that_says_nothing_reports_nothing(monkeypatch):
    assert "load_ms" not in _chat(monkeypatch, DONE_SILENT)


def test_load_time_does_not_make_a_span_look_token_reported(client, monkeypatch):
    """`tokens_source` is decided by whether the provider reported TOKENS.

    Dropping a non-token measurement into the same sink flips that flag: a server that
    times its load but counts no tokens would be recorded as having REPORTED tokens, with
    None in both token columns. A claim about the provider the provider never made — and
    invisible, because the row looks like every other reported one.

    Driven through `build_chat` so the span is the one that actually gets stored; asserting
    on the sink alone would pass with the flag broken.
    """
    import uuid

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import LlmCallSpan
    from app import providers

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream([
        '{"message": {"content": "answer"}}',
        '{"message": {"content": ""}, "done": true, "load_duration": 10240000000}',
    ]))
    tok = "load-" + uuid.uuid4().hex[:12]
    providers.build_chat("ollama", base_url="https://gw.example", model="m",
                         project_id=tok).chat(system="s", context="c", question="q")

    db = SessionLocal()
    try:
        row = db.scalars(select(LlmCallSpan).where(LlmCallSpan.project_id == tok)).one()
    finally:
        db.close()

    assert row.load_ms == 10240.0
    assert row.tokens_source != "reported", (
        "a load time with no token counts was recorded as reported tokens"
    )
    assert row.input_tokens is None and row.output_tokens is None


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _span(db, *, load_ms, provider="ollama", model="mistral-small3.1:24b"):
    from app.models import LlmCallSpan
    db.add(LlmCallSpan(provider=provider, model=model, base_url="", kind="chat",
                       feature="grill", project_id="core", tokens_source="reported",
                       latency_ms=100.0, load_ms=load_ms, ok=True))
    db.commit()


def test_the_summary_counts_only_calls_that_paid(client, auth, db):
    _span(db, load_ms=10240.0)
    _span(db, load_ms=0.0)
    _span(db, load_ms=0.0)

    out = client.get("/api/platform/model-loads?project_id=core", headers=auth).json()

    assert out["reporting"] == 3
    assert out["reloads"] == 1
    assert out["reload_ms_total"] == 10240.0
    assert out["worst_ms"] == 10240.0
    assert out["models"] == ["ollama:mistral-small3.1:24b"]


def test_providers_that_cannot_measure_a_reload_are_excluded_not_counted_as_warm(
    client, auth, db,
):
    """THE one that decides whether this reading can be trusted.

    Anthropic and OpenAI never report loading. Counting their NULLs as zero would push
    the share toward zero and report a box that reloads on every call as healthy — the
    absence-reads-as-clean failure, in the very panel added to end it.
    """
    _span(db, load_ms=10240.0)
    for _ in range(20):
        _span(db, load_ms=None, provider="anthropic", model="claude-opus-4-8")

    out = client.get("/api/platform/model-loads?project_id=core", headers=auth).json()

    assert out["reporting"] == 1, "spans that cannot measure a load were counted as evidence"
    assert out["reloads"] == 1


def test_nothing_measurable_is_not_a_clean_bill(client, auth, db):
    """Zero reloads out of zero reporting calls must be distinguishable from zero out of
    fifty. `reporting` is what tells them apart, so it has to be there and be honest."""
    out = client.get("/api/platform/model-loads?project_id=core", headers=auth).json()
    assert out["reporting"] == 0
    assert out["reloads"] == 0


def test_another_org_cannot_read_this_box_s_model_loads(client, auth, db):
    """CI caught this, not me. The first cut took `project_id` and answered for whoever
    was named — and on a hosted box the count alone reports how much traffic the other
    orgs are putting through it, while `models` names what they run. Scoped through a
    project the caller can read, exactly like `list_credentials`.
    """
    r = client.get("/api/platform/model-loads?project_id=someone-elses-project", headers=auth)
    assert r.status_code in (403, 404), (
        f"a project the caller cannot read answered with {r.status_code}"
    )
