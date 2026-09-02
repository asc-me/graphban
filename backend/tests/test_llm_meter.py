"""Per-call LLM spans (GRPH-225).

The module docstring of `app/providers/llm_meter.py` states three invariants and claims
each is a test. This file is where they are actually enforced, plus the absence
conventions — the defect class this repo keeps re-learning, in the place most tempting
to fake a number: a cost table.

HTTP-facing tests run against a scripted local server rather than a mock: the claim is
about what the ADAPTER does with a real response (parses the usage tail, raises on 429),
and a mock that agrees with the adapter's own assumptions proves nothing.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import LlmCallSpan
from app.providers import llm_meter
from app import providers


def _tok() -> str:
    """A unique project stamp for one test's spans.

    CI's Postgres job runs `-n auto` xdist against ONE shared database — every other
    worker's LLM traffic lands in `llm_call_spans` while this file asserts on it (the
    SQLite job got away with global counts because each worker has its own file). The
    construction-time project binding is the natural per-test key: meter every call
    under a fresh token and read the table by it."""
    return "meter-" + uuid.uuid4().hex[:12]


def _spans(kind: str | None = None, project: str | None = None) -> list[LlmCallSpan]:
    db = SessionLocal()
    try:
        rows = db.scalars(select(LlmCallSpan).order_by(LlmCallSpan.id)).all()
    finally:
        db.close()
    return [r for r in rows
            if (kind is None or r.kind == kind)
            and (project is None or r.project_id == project)]


def _sse_chunks(text: str = "the answer", usage: tuple | None = (11, 7)) -> list[str]:
    """OpenAI-compat SSE: a content delta, the `include_usage` tail chunk, [DONE]."""
    out = ["data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"]
    if usage:
        out.append("data: " + json.dumps(
            {"choices": [], "usage": {"prompt_tokens": usage[0],
                                       "completion_tokens": usage[1]}}) + "\n\n")
    out.append("data: [DONE]\n\n")
    return out


def _serve(script: list):
    """script: list of (status, "sse"|"json", payload) consumed one per request."""

    class Handler(BaseHTTPRequestHandler):
        requests: list = []

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                body = {}
            type(self).requests.append((self.path, body))
            status, ctype, payload = script.pop(0) if script else (500, "json", {})
            self.send_response(status)
            if ctype == "json":
                raw = json.dumps(payload).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for line in payload:
                    self.wfile.write(line.encode())

        def log_message(self, *_args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, Handler


# --------------------------------------------------------------- one span per call

def test_a_chat_call_produces_exactly_one_span_with_reported_tokens_and_cost(client):
    """The headline claim: one provider call → one row, with the provider's OWN numbers.

    `chat()` on this adapter is assembled from `stream()`, and BOTH are wrapped — the
    nested call must stay silent (the _inside guard), or every chat would count twice.
    """
    httpd, Handler = _serve([(200, "sse", _sse_chunks())])
    tok = _tok()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        chat = providers.build_chat("custom", base_url=url, api_key="k", model="gpt-4.1",
                                    project_id=tok)
        answer = chat.chat(system="s", context="c", question="q")
    finally:
        httpd.server_close()

    assert answer == "the answer"
    rows = _spans(project=tok)
    assert len(rows) == 1, f"expected exactly one span, got {[ (r.kind, r.model) for r in rows ]}"
    r = rows[0]
    assert (r.provider, r.model, r.kind) == ("custom", "gpt-4.1", "chat")
    assert r.base_url == url
    assert r.tokens_source == "reported"
    assert (r.input_tokens, r.output_tokens) == (11, 7)
    assert r.ok and r.error_class == "" and r.http_status is None
    assert r.latency_ms and r.latency_ms >= 0
    # gpt-4.1: 2.5/10 per MTok
    assert r.cost_usd == pytest.approx((11 * 2.5 + 7 * 10) / 1_000_000, abs=1e-6)


def test_a_plain_stream_meters_once_and_still_streams(client):
    """The `iter_reply` path consumes stream() directly — the generator driver must
    neither swallow chunks nor emit two rows."""
    httpd, _ = _serve([(200, "sse", _sse_chunks(text="abc"))])
    tok = _tok()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        chat = providers.build_chat("custom", base_url=url, api_key="k", model="gpt-4o",
                                    project_id=tok)
        pieces = list(chat.stream(system="s", context="c", question="q"))
    finally:
        httpd.server_close()

    assert "".join(pieces) == "abc"
    rows = _spans(project=tok)
    assert len(rows) == 1 and rows[0].kind == "chat"
    assert rows[0].tokens_source == "reported"


def test_a_stream_consumed_across_threads_meters_once(client):
    """The CI-shaped version of the stream test: an actual `StreamingResponse` route
    consumes `stream()` through `iterate_in_threadpool`, so the driver's set/reset
    pairs land in a DIFFERENT copied context per chunk. The first cut raised
    `ValueError: Token created in a different Context` here (and in every grill/
    assistant/agent-chat stream test), and the usage tail — which arrives on a later
    chunk than the one that opened the sink — would have decayed to `estimated`
    even if it hadn't crashed. This test is the regression lock for both."""
    from fastapi import APIRouter
    from starlette.responses import StreamingResponse

    httpd, _ = _serve([(200, "sse", _sse_chunks(text="hello "))])
    tok = _tok()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"

    def _stream_route():
        chat = providers.build_chat("custom", base_url=url, api_key="k",
                                    model="gpt-4o", project_id=tok)
        return StreamingResponse(chat.stream(system="s", context="c", question="q"),
                                 media_type="text/event-stream")

    router = APIRouter()
    router.add_api_route("/__meter_probe", _stream_route)
    client.app.include_router(router)
    try:
        with client.stream("GET", "/__meter_probe") as r:
            body = b"".join(r.iter_bytes()).decode()
    finally:
        httpd.server_close()

    assert "hello" in body
    rows = _spans(project=tok)
    assert len(rows) == 1, f"threaded stream emitted {len(rows)} spans"
    assert rows[0].tokens_source == "reported"  # the sink survived the chunk hop


def test_an_error_is_a_span_too_and_still_raises(client):
    """A failed call spent the request — hiding it would make an outage-shaped absence
    read as no traffic, which is the defect class again. The caller still gets the
    exception: the span records the failure, it does not absorb it."""
    httpd, _ = _serve([(429, "json", {"error": "rate limited"})])
    tok = _tok()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        chat = providers.build_chat("custom", base_url=url, api_key="k", model="gpt-4.1",
                                    project_id=tok)
        with pytest.raises(httpx.HTTPStatusError):
            chat.chat(system="s", context="c", question="q")
    finally:
        httpd.server_close()

    rows = _spans(project=tok)
    assert len(rows) == 1
    r = rows[0]
    assert r.ok is False
    assert r.error_class == "HTTPStatusError"
    assert r.http_status == 429  # the drift signal: 429s are a rate, not a mystery
    assert r.retryable is None   # the raw httpx error carries no verdict — absence, not False


def test_an_extractor_makes_one_span_not_two(client):
    """Extractors construct their inner chat *inside* the adapter (never through
    build_chat), and extract() calls chat() which joins stream(). Nested wrapped calls
    must be silent: instrumentation at the construction chokepoint cannot double-count
    a call the extractor owns."""
    httpd, _ = _serve([(200, "sse", _sse_chunks(text="a decision\na convention"))])
    tok = _tok()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        ex = providers.build_extractor("custom", base_url=url, api_key="k", model="gpt-4.1",
                                       project_id=tok)
        shards = ex.extract(title="shipped X", description="we chose Y")
    finally:
        httpd.server_close()

    assert shards == ["a decision", "a convention"]
    rows = _spans(project=tok)
    assert len(rows) == 1, f"extractor double-counting: {[(r.kind, r.feature) for r in rows]}"
    assert rows[0].kind == "extract"
    assert rows[0].tokens_source == "reported"
    assert rows[0].output_preview and "a decision" in rows[0].output_preview


# ---------------------------------------------------------------- absence: NULL is a fact

def test_stub_spend_is_a_real_zero_not_an_unknown(client):
    """0.0 here is a claim that CAN be true (compute we already pay for); NULL would be
    a cop-out and an estimate would be noise on a zero row."""
    tok = _tok()
    with llm_meter.llm_context(feature="unit.stub"):
        chat = providers.build_chat("stub", project_id=tok)
        chat.chat(system="s", context="c", question="q")
    rows = _spans(project=tok)
    assert len(rows) == 1
    r = rows[0]
    assert r.provider == "stub" and r.ok
    assert r.cost_usd == 0.0
    assert r.tokens_source == "none"
    assert r.input_tokens is None and r.output_tokens is None
    assert r.output_preview and "Local stub agent" in r.output_preview


def test_an_unpriced_model_costs_NULL_never_zero(client):
    """The single most tempting fabrication in the whole feature. A model that matches
    no price prefix has an UNKNOWN cost; a 0 would read as free on the exact panel this
    feature exists to populate."""
    httpd, _ = _serve([(200, "sse", _sse_chunks())])
    tok = _tok()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        chat = providers.build_chat("custom", base_url=url, api_key="k",
                                    model="unobtainium-9000", project_id=tok)
        chat.chat(system="s", context="c", question="q")
    finally:
        httpd.server_close()

    r = _spans(project=tok)[0]
    assert r.cost_usd is None
    # tokens still arrive — NULL cost is "we cannot price this", not "we saw nothing":
    assert r.tokens_source == "reported" and r.input_tokens == 11


def test_a_provider_that_reports_nothing_is_flagged_as_estimated(client):
    """chars/4 is for charting an order of magnitude, and only honest if stamped.
    An estimate with no flag is a fabricated number wearing reported's clothes."""
    httpd, _ = _serve([(200, "sse", _sse_chunks(usage=None))])
    tok = _tok()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        chat = providers.build_chat("custom", base_url=url, api_key="k", model="gpt-4.1",
                                    project_id=tok)
        chat.chat(system="s", context="c", question="q")
    finally:
        httpd.server_close()

    r = _spans(project=tok)[0]
    assert r.tokens_source == "estimated"
    assert r.input_tokens > 0 and r.output_tokens > 0  # from text length, stamped as such
    assert r.cost_usd is not None  # priced off an estimate: roughly right, visibly sourced


# ---------------------------------------------------------------- attribution

def test_project_bound_at_construction_wins_over_the_contextvar(client):
    """A request that resolves project A's provider and then touches project B must
    bill A — the resolution is the authority. Instance beats context."""
    tok = _tok()
    with llm_meter.llm_context(feature="anything", project_id=f"other-{tok}"):
        providers.build_chat("stub", project_id=tok).chat(
            system="s", context="c", question="q")
    r = _spans(project=tok)[0]
    assert r.project_id == tok  # instance beats the contextvar, per the resolver-authority rule
    assert r.feature == "anything"  # feature is still call-site context, by design


def test_a_context_project_still_binds_when_the_instance_carries_none(client):
    """Deployment-scoped embedders (the env path) know no project; the contextvar is
    what gives their rows an owner."""
    emb = providers.build_embedder("stub")
    tok = _tok()
    with llm_meter.llm_context(feature="embed.write", project_id=tok):
        emb.embed("some text")
    r = _spans(project=tok)[0]
    assert (r.kind, r.feature) == ("embed", "embed.write")  # ctx-bound project read at emit


def test_an_untagged_call_site_lands_in_untagged_not_a_default_bucket(client):
    tok = _tok()
    providers.build_chat("stub", project_id=tok).chat(system="s", context="c", question="q")
    assert _spans(project=tok)[0].feature == ""


def test_a_tool_session_keeps_its_creation_feature_into_the_stream(client):
    """The assistant's session is built in the request scope but its turns run from the
    response generator's thread — a context manager that has already exited is not
    there to be read. The factory must stamp, not hope."""
    httpd, _ = _serve([(200, "json", {
        "choices": [{"message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2}})])
    tok = _tok()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        chat = providers.build_chat("custom", base_url=url, api_key="k", model="gpt-4.1")
        with llm_meter.llm_context(feature="assistant", project_id=tok):
            session = chat.tool_session(system="s", context="c", question="q")
        # Context has EXITED. The turn below runs where feature_var reads "".
        turn = session.run_turn([])
    finally:
        httpd.server_close()

    assert turn.text == "hi"
    rows = _spans(kind="tool_turn", project=tok)
    assert len(rows) == 1
    r = rows[0]
    assert r.feature == "assistant" and r.project_id == tok
    # usage rides on the ToolTurn result; the wrapper merges it — reported, not estimated
    assert r.tokens_source == "reported"
    assert (r.input_tokens, r.output_tokens) == (5, 2)


def test_stream_turn_preserves_the_generator_return_value(client):
    """`stream_turn` returns its ToolTurn via generator `return` — the assistant loop
    reads `turn = yield from ...`. A driver that emits a span but eats the return value
    breaks the feature to observe it."""
    httpd, _ = _serve([(200, "sse", _sse_chunks(text="partial"))])
    tok = _tok()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        chat = providers.build_chat("custom", base_url=url, api_key="k", model="gpt-4.1",
                                    project_id=tok)
        session = chat.tool_session(system="s", context="c", question="q")
        gen = session.stream_turn([])
        collected = []
        returned = None
        while True:
            try:
                collected.append(next(gen))
            except StopIteration as stop:
                returned = stop.value
                break
    finally:
        httpd.server_close()

    assert "".join(collected) == "partial"
    assert returned is not None and returned.text == "partial"
    assert returned.usage == {"input": 11, "output": 7}
    rows = _spans(kind="tool_turn", project=tok)
    assert len(rows) == 1
    assert rows[0].tokens_source == "reported"  # merged from the ToolTurn's usage


# ---------------------------------------------------------- telemetry never breaks it

def test_a_failing_span_write_never_breaks_the_call(client, monkeypatch):
    """THE invariant, and the only one whose test is worth more than the feature row:
    the provider call's outcome belongs to the user, the span belongs to us."""
    attempts = {"n": 0}

    def _boom():
        attempts["n"] += 1
        raise RuntimeError("db is on fire")

    monkeypatch.setattr("app.db.SessionLocal", _boom)
    chat = providers.build_chat("stub")
    answer = chat.chat(system="s", context="c", question="q")

    assert answer  # the user still got their reply
    assert attempts["n"] >= 1  # the write was ATTEMPTED — the swallow is real, not a skip


def test_record_usage_outside_a_span_is_silent(client):
    """Adapters cannot know whether the object was wrapped; a bare build must not turn
    a legal report into an error."""
    tok = _tok()
    llm_meter.record_usage(input=5, output=None)  # no sink, no raise
    assert _spans(project=tok) == []


def test_every_span_is_mirrored_to_a_structured_log_line(client):
    """The log platform half of the store decision: `extra={"llm": ...}` is what makes
    LOG_JSON deployments whole without a collector.

    This test earned its keep before it passed: the first cut of the format string had
    six placeholders and five arguments, and the TypeError inside logging was swallowed
    by `_emit`'s own try/except — every mirror line dead on arrival, invisibly. So the
    assertions run `getMessage()` (what formatting does) rather than trusting that
    `logger.info` returned.

    Captured with its own handler rather than `caplog`, for the documented reason in
    tests/test_fallback_says_so.py: `configure_logging()` replaces the root handlers.
    """
    import logging

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    lg = logging.getLogger("graphban.llm")
    handler = Capture(level=logging.INFO)
    lg.addHandler(handler)
    try:
        providers.build_chat("stub").chat(system="s", context="c", question="q")
    finally:
        lg.removeHandler(handler)

    mirror = [r for r in records if getattr(r, "llm", None)]
    assert len(mirror) == 1
    assert mirror[0].getMessage()  # raises on a placeholder/arg mismatch — the bug class
    payload = mirror[0].llm
    assert payload["provider"] == "stub" and payload["ok"] is True
    assert payload["cost_usd"] == 0.0  # stub spend is a real zero, present, not omitted
    assert "http_status" not in payload  # None is filtered out, not serialized as null-noise


# ---------------------------------------------------------------------- retention

def _insert_span(ts, provider="stub", project=None):
    db = SessionLocal()
    try:
        db.add(LlmCallSpan(ts=ts, provider=provider, kind="chat",
                           **({"project_id": project} if project else {})))
        db.commit()
    finally:
        db.close()


def test_purge_expired_deletes_only_beyond_the_window(client, monkeypatch):
    now = datetime.now(timezone.utc)
    tok = _tok()
    _insert_span(now - timedelta(days=200), project=tok)
    _insert_span(now - timedelta(days=1), project=tok)
    monkeypatch.setattr(llm_meter.settings, "llm_span_retention_days", 90)

    assert llm_meter.purge_expired() >= 1  # >= : other workers' stale rows are fair game
    remaining = _spans(project=tok)
    assert len(remaining) == 1
    # SQLite reads DateTime(timezone=True) back NAIVE; Postgres reads it aware. The
    # comparison must normalize the read-back rather than assume either engine — the
    # suite runs both, and a tz assumption is the classic one-engine-passes bug.
    ts = remaining[0].ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    assert (now - ts).days < 10


def test_retention_zero_keeps_everything(client, monkeypatch):
    """Self-host boxes with no dashboard pressure must be able to opt out by value,
    not by patching code."""
    now = datetime.now(timezone.utc)
    tok = _tok()
    _insert_span(now - timedelta(days=500), project=tok)
    monkeypatch.setattr(llm_meter.settings, "llm_span_retention_days", 0)
    assert llm_meter.purge_expired() == 0
    assert len(_spans(project=tok)) >= 1


# ------------------------------------------------- the MCP dispatch context (threadpool)

def test_an_mcp_tool_call_tags_its_spans_mcp_colon_tool(client, auth):
    """End-to-end for the set-never-reset block in mcp_server: the tool runs in
    run_in_threadpool — a COPIED context — and the embed written there must still know
    which tool asked for it. An untagged row here would be the absence reading as clean."""
    key = client.post("/api/api-keys", json={"name": "meter"}, headers=auth).json()["plaintext"]
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "add_memory",
                                                 "arguments": {"text": "a durable lesson",
                                                               "scope": "global"}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200 and not r.json()["result"].get("isError")

    # This call embedded at least one vector (its write completed) — so an
    # mcp:add_memory-tagged embed span must exist. Under xdist on shared Postgres
    # another worker's add_memory could also be here; that still tests the
    # mechanism. What cannot be here is the absence: zero rows means this process's
    # own embed wrote an untagged span, i.e. the copy did not carry the tag.
    tagged = [r for r in _spans(kind="embed") if r.feature == "mcp:add_memory"]
    assert tagged, f"no tagged embed span; saw {[(r.kind, r.feature) for r in _spans()][-8:]}"
