"""AL-183: token-level streaming for assistant text turns.

Each provider's `stream_turn` yields text deltas AND returns a normalized `ToolTurn`
(via StopIteration.value) with tool-calls + usage assembled from the buffered turn —
so text streams token-level while tool-call parsing keeps AL-180 parity. The router
forwards those deltas as SSE `delta` frames.
"""
import json
import types

from app.providers import anthropic_provider, openai_compat
from app.providers.toolcall import ToolCall, ToolSpec, ToolTurn

_SPEC = ToolSpec("update_item", "Advance an item's status.",
                 {"type": "object", "properties": {"status": {"type": "string"}}})


def _drain(gen):
    """Run a stream_turn generator to completion → (list of yielded deltas, returned ToolTurn)."""
    deltas = []
    try:
        while True:
            deltas.append(next(gen))
    except StopIteration as stop:
        return deltas, stop.value


# --------------------------------- stub ---------------------------------
def test_stub_stream_turn_chunks_text_and_returns_turn():
    from app.providers.stub import StubChat

    session = StubChat().tool_session(system="s", context="grounding facts", question="q")
    deltas, turn = _drain(session.stream_turn([_SPEC]))
    assert len(deltas) > 1  # emitted in chunks, not one blob
    assert "".join(deltas) == turn.text and "grounding facts" in turn.text
    assert not turn.wants_tools and turn.tool_calls == []


# ------------------------- OpenAI-compatible (Grok/ChatGPT) -------------------------
class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self._lines


def test_openai_compat_stream_assembles_fragmented_tool_call(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"content":"Mark"}}]}',
        'data: {"choices":[{"delta":{"content":"ing."}}]}',
        # tool_call arguments arrive as partial fragments across chunks
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"function":{"name":"update_item","arguments":"{\\"sta"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"tus\\": \\"done\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}',
        "data: [DONE]",
    ]
    sent = {}
    monkeypatch.setattr(openai_compat.httpx, "stream",
                        lambda *a, json=None, **k: (sent.update(json or {}), _FakeStream(lines))[1])

    session = openai_compat.chat("https://x/v1", "k", "grok-2").tool_session(system="s", context="c", question="q")
    deltas, turn = _drain(session.stream_turn([_SPEC]))

    assert deltas == ["Mark", "ing."]  # text streamed token-level
    assert sent["stream"] is True and sent["stream_options"]["include_usage"] is True
    # fragmented arguments concatenated then decoded ONCE into an object
    assert turn.tool_calls == [ToolCall(id="call_1", name="update_item", input={"status": "done"})]
    assert turn.wants_tools and turn.usage == {"input": 10, "output": 5}
    # the assistant turn (with its tool_calls) was echoed into history for the next round
    echoed = session.messages[-1]
    assert echoed["role"] == "assistant" and echoed["tool_calls"][0]["id"] == "call_1"


def test_openai_compat_stream_plain_text_turn(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"content":"All "}}]}',
        'data: {"choices":[{"delta":{"content":"good."}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(openai_compat.httpx, "stream", lambda *a, **k: _FakeStream(lines))
    session = openai_compat.chat("https://x/v1", "k", "m").tool_session(system="s", context="c", question="q")
    deltas, turn = _drain(session.stream_turn([_SPEC]))
    assert deltas == ["All ", "good."] and turn.text == "All good."
    assert not turn.wants_tools and turn.tool_calls == []


# --------------------------------- Anthropic (Claude) ---------------------------------
def _block(**kw):
    return types.SimpleNamespace(**kw)


class _FakeAnthropicStream:
    def __init__(self, deltas, message):
        self._deltas, self._message = deltas, message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self._deltas)

    def get_final_message(self):
        return self._message


def test_anthropic_stream_yields_text_then_returns_tool_use(monkeypatch):
    message = types.SimpleNamespace(
        stop_reason="tool_use",
        content=[_block(type="text", text="Marking done."),
                 _block(type="tool_use", id="toolu_1", name="update_item", input={"status": "done"})],
        usage=types.SimpleNamespace(input_tokens=12, output_tokens=4))
    cm = _FakeAnthropicStream(["Mark", "ing done."], message)
    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **kw: cm))
    monkeypatch.setattr(anthropic_provider, "_client", lambda api_key="": fake_client)

    session = anthropic_provider.chat("sk-ant", "claude-opus-4-8").tool_session(
        system="sys", context="ctx", question="mark done")
    deltas, turn = _drain(session.stream_turn([_SPEC]))

    assert deltas == ["Mark", "ing done."] and turn.text == "Marking done."
    assert turn.tool_calls == [ToolCall(id="toolu_1", name="update_item", input={"status": "done"})]
    # Cache accounting rides along on every Anthropic turn (GRPH-226). Both zero here is the
    # meaningful reading, not padding: prompt caching is deferred while the stable prefix is
    # below the model's minimum, and zeros are exactly what the documentation says to look for
    # to know a prompt was NOT cached. Asserted rather than omitted, so enabling caching later
    # has to change this line.
    assert turn.wants_tools and turn.usage == {
        "input": 12, "output": 4, "cache_read": 0, "cache_write": 0}
    # the assistant content blocks were echoed into history verbatim (replay pattern)
    assert session.messages[-1] == {"role": "assistant", "content": message.content}


# --------------------------------- router: SSE token stream ---------------------------------
def _events(text: str):
    out = []
    for block in text.strip().split("\n\n"):
        ev = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if ev:
            out.append((ev, data))
    return out


def _thread(client, auth, entity_type="item", entity_id="AL-08"):
    return client.post("/api/assistant/threads", json={
        "project_id": "core", "entity_type": entity_type, "entity_id": entity_id}, headers=auth).json()


def _patch_session(monkeypatch, session):
    from app.routers import assistant as router
    monkeypatch.setattr(router.platform_svc, "resolve_chat_for",
                        lambda db, pid, prov: ("openai", type("C", (), {"tool_session": lambda s, **k: session})()))


def test_router_forwards_each_token_as_its_own_delta(client, auth, monkeypatch):
    class _S:
        def stream_turn(self, tools):
            for piece in ["Hel", "lo ", "there"]:
                yield piece
            return ToolTurn(text="Hello there", wants_tools=False, usage={"input": 3, "output": 2})

        def add_results(self, results):
            pass

    _patch_session(monkeypatch, _S())
    t = _thread(client, auth)
    r = client.post(f"/api/assistant/threads/{t['id']}/message", json={"message": "hi"}, headers=auth)

    events = _events(r.text)
    deltas = [json.loads(d)["text"] for e, d in events if e == "delta"]
    assert deltas == ["Hel", "lo ", "there"]  # streamed token-level, not one buffered delta
    assert any(e == "usage" for e, _ in events) and events[-1][0] == "done"
    # the full text is what gets persisted
    detail = client.get(f"/api/assistant/threads/{t['id']}", headers=auth).json()
    assert detail["messages"][-1]["content"] == "Hello there"


def test_router_streams_text_and_still_stages_writes(client, auth, monkeypatch):
    class _S:
        def __init__(self):
            self.n = 0

        def stream_turn(self, tools):
            self.n += 1
            if self.n == 1:
                yield "Let me update it. "
                return ToolTurn(text="Let me update it. ", wants_tools=True,
                                tool_calls=[ToolCall(id="c1", name="update_item", input={"status": "review"})])
            yield "Proposed the change."
            return ToolTurn(text="Proposed the change.", wants_tools=False)

        def add_results(self, results):
            pass

    _patch_session(monkeypatch, _S())
    t = _thread(client, auth)
    r = client.post(f"/api/assistant/threads/{t['id']}/message", json={"message": "review it"}, headers=auth)

    events = _events(r.text)
    kinds = [e for e, _ in events]
    deltas = [json.loads(d)["text"] for e, d in events if e == "delta"]
    assert deltas == ["Let me update it. ", "Proposed the change."]  # both turns streamed
    assert "tool_call" in kinds and "proposed_action" in kinds and kinds[-1] == "done"
    # staged, not executed
    assert client.get("/api/items/AL-08", headers=auth).json()["status"] != "review"
