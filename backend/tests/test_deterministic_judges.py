"""Judging is deterministic; conversing is not (follow-up to PRD-15).

Measured on the live instance while grilling PRD-12: three runs of the SAME transcript
through the SAME model produced two different completion states. Nothing sent a
temperature, so classification inherited the provider's default sampling — around 0.8 for
Ollama. Whether a PRD approved depended on when the classifier happened to run.

That is a worse problem than which model is used, and it was invisible: every run
returned a well-formed, plausible verdict. It only showed up by running the same input
repeatedly and comparing, which no test was doing.

The rule these pin: anything that JUDGES asks for temperature 0. Anything that WRITES —
the grill conversation, the assistant, body synthesis — is left alone, because varied
phrasing there is a feature.
"""
import pytest

from app.services import memory as mem_svc
from app.services import prds as prd_svc
from app.services.platform import Resolved


class _Recorder:
    """Captures what the service asked for, and answers plausibly."""

    model, base_url = "test-model", "http://x"

    def __init__(self, reply: str = "{}"):
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, **kw):
        self.calls.append(kw)
        return self.reply


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ---- the judges ask for determinism ---------------------------------------------------
def test_the_grill_classifier_asks_for_temperature_zero(db, monkeypatch):
    prd = prd_svc.create_prd(db, title="T", project_id="core", body="# T\n")
    prd_svc.record_grill_turns(db, prd.id, [{"role": "user", "text": "An answer with substance."}])
    rec = _Recorder()
    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat", lambda db, pid: Resolved("ollama", rec))

    prd_svc._classify_dimensions(db, prd, prd_svc.grill_history(db, prd.id))
    assert rec.calls and rec.calls[0]["temperature"] == 0


def test_the_memory_judge_asks_for_temperature_zero(db, monkeypatch):
    """Same class of thing: 'is this memory worth keeping' must not be a coin flip."""
    from app.models import MemoryShard

    rec = _Recorder('{"keep": true, "quality": 0.9, "reason": "ok"}')
    monkeypatch.setattr(mem_svc, "_llm_judge", mem_svc._llm_judge)  # keep the real one
    import app.services.platform as platform_svc

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("ollama", rec))
    shard = MemoryShard(id="m_x", text="A durable fact.", scope="global", project_id="core",
                        status="candidate", origin="agent:t")
    db.add(shard)
    db.commit()

    mem_svc._llm_judge(db, shard)
    assert rec.calls and rec.calls[0]["temperature"] == 0


# ---- conversation is left warm ----------------------------------------------------------
def test_the_grill_conversation_does_not_force_determinism(db, monkeypatch):
    """Asking sharp questions benefits from variation — a grill that produces the same
    four questions forever stops finding anything. Only the verdict must be reproducible."""
    prd = prd_svc.create_prd(db, title="T", project_id="core", body="# T\n")
    rec = _Recorder("- a question?")
    monkeypatch.setattr(prd_svc.platform_svc, "resolve_chat", lambda db, pid: Resolved("ollama", rec))

    prd_svc.ai_command(db, prd.id, "grill")
    assert rec.calls and rec.calls[0].get("temperature") is None


# ---- every provider accepts it -----------------------------------------------------------
@pytest.mark.parametrize("factory", [
    lambda: __import__("app.providers.ollama", fromlist=["x"]).chat(base_url="http://x", model="m"),
    lambda: __import__("app.providers.openai_compat", fromlist=["x"]).chat("http://x", "k", "m"),
    lambda: __import__("app.providers.stub", fromlist=["x"]).StubChat(),
])
def test_every_chat_provider_accepts_a_temperature(factory):
    """The protocol gained a keyword, so an implementation that ignores it must still
    ACCEPT it — otherwise switching provider turns a judge call into a TypeError."""
    import inspect

    sig = inspect.signature(factory().chat)
    assert "temperature" in sig.parameters
    assert sig.parameters["temperature"].default is None


def test_ollama_sends_the_temperature_it_was_given(monkeypatch):
    """Accepting the argument and dropping it would pass the test above while leaving the
    judge exactly as non-deterministic as before."""
    import app.providers.ollama as ol

    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def iter_lines(self): return iter([])

    class _Ctx:
        def __enter__(self): return _Resp()
        def __exit__(self, *a): return False

    def fake_stream(method, url, **kw):
        sent.update(kw.get("json") or {})
        return _Ctx()

    monkeypatch.setattr(ol.httpx, "stream", fake_stream)
    list(ol.OllamaChat("http://x", "m").stream(system="s", context="c", question="q", temperature=0))
    assert sent.get("options") == {"temperature": 0}


def test_ollama_omits_temperature_when_not_asked(monkeypatch):
    """No temperature means the provider's own default, unchanged — this must not
    silently pin every call in the app to something."""
    import app.providers.ollama as ol

    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def iter_lines(self): return iter([])

    class _Ctx:
        def __enter__(self): return _Resp()
        def __exit__(self, *a): return False

    monkeypatch.setattr(ol.httpx, "stream",
                        lambda m, u, **kw: (sent.update(kw.get("json") or {}), _Ctx())[1])
    list(ol.OllamaChat("http://x", "m").stream(system="s", context="c", question="q"))
    assert "options" not in sent
