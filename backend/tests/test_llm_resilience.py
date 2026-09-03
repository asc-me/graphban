"""AL-136 + AL-141: surviving a real LLM gateway.

The hosted instance embeds and generates against a self-hosted gateway behind an edge
proxy that cuts a request off at roughly 100s to FIRST byte. Two consequences drive
this suite:

- A blocking completion's time-to-first-byte IS its total generation time, so long
  answers get severed. `chat()` therefore assembles from the stream.
- The gateway can be cold, rate-limited, or down. An ingest must not lose the row just
  because it couldn't get a vector.
"""
import httpx
import pytest

from app.providers import ollama, openai, openai_compat


# ---- AL-141: chat() must not depend on a blocking response --------------------
class _FakeStreamChat:
    """Records how the completion was obtained."""

    def __init__(self, cls):
        self.cls = cls
        self.streamed = False

    def stream(self, *, system, context, question, temperature=None):
        self.streamed = True
        yield "Hello"
        yield ", "
        yield "world.  "


def test_openai_compat_chat_assembles_from_stream(monkeypatch):
    chat = openai_compat.OpenAICompatChat("https://gw.example/v1", "k", "m")
    monkeypatch.setattr(chat, "stream", lambda **kw: iter(["Hel", "lo", " world.  "]))
    # A blocking POST would be a bug — fail loudly if anything reaches httpx.post.
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("chat() used a blocking POST"))
    assert chat.chat(system="s", context="c", question="q") == "Hello world."


def test_ollama_chat_assembles_from_stream(monkeypatch):
    chat = ollama.OllamaChat("https://gw.example", "m")
    monkeypatch.setattr(chat, "stream", lambda **kw: iter(["par", "tial", " answer "]))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("chat() used a blocking POST"))
    assert chat.chat(system="s", context="c", question="q") == "partial answer"


def test_chat_streams_so_first_byte_is_immediate(monkeypatch):
    """The property that matters for the edge limit: output is produced incrementally,
    not after the whole generation completes."""
    chat = openai_compat.OpenAICompatChat("https://gw.example/v1", "k", "m")
    fake = _FakeStreamChat(openai_compat.OpenAICompatChat)
    monkeypatch.setattr(chat, "stream", fake.stream)
    chat.chat(system="s", context="c", question="q")
    assert fake.streamed is True


# ---- AL-136: embed retry, then graceful degradation ---------------------------
def test_embed_retries_transient_failure_then_succeeds(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "embed_max_retries", 2)
    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    def flaky_post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("gateway cold")
        return _Resp()

    monkeypatch.setattr(httpx, "post", flaky_post)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    emb = openai.OpenAIEmbedder("https://gw.example/v1", "k", "bge-m3", 1024)
    assert emb.embed("hi") == [0.1, 0.2, 0.3]
    assert calls["n"] == 3  # two failures, then success


def test_embed_raises_after_exhausting_retries(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "embed_max_retries", 1)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    emb = openai.OpenAIEmbedder("https://gw.example/v1", "k", "bge-m3", 1024)
    with pytest.raises(httpx.ConnectError):
        emb.embed("hi")


def test_safe_embed_returns_none_instead_of_raising(monkeypatch):
    """The ingest contract: never raise, even when the provider is dead."""
    import app.providers as providers

    class _Dead:
        dim = 1024

        def embed(self, text):
            raise httpx.ConnectError("gateway down")

    monkeypatch.setattr(providers, "get_embedder", lambda: _Dead())
    assert providers.safe_embed("anything") is None


# ---- ingest survives a dead embedder -----------------------------------------
def test_memory_write_survives_a_dead_embedder(client, auth, monkeypatch):
    """A shard is worth more than its vector — the row lands with NULL embedding and
    backfill fills it in later."""
    import app.providers as providers

    class _Dead:
        dim = 384

        def embed(self, text):
            raise httpx.ConnectError("gateway down")

    monkeypatch.setattr(providers, "get_embedder", lambda: _Dead())
    r = client.post("/api/memory/shards",
                    json={"text": "survives the outage", "project_id": "core"}, headers=auth)
    assert r.status_code == 201, r.text

    shards = client.get("/api/memory/shards?project_id=core", headers=auth).json()
    assert any(s["text"] == "survives the outage" for s in shards)

    # ...and it really did degrade rather than quietly embedding via some other path.
    from app.db import SessionLocal
    from app.models import MemoryShard

    db = SessionLocal()
    try:
        stored = db.get(MemoryShard, r.json()["id"])
        assert stored is not None and stored.embedding is None
    finally:
        db.close()


def test_code_graph_describe_survives_a_dead_embedder(client, auth, monkeypatch):
    import app.providers as providers

    class _Dead:
        dim = 384

        def embed(self, text):
            raise httpx.ConnectError("gateway down")

    monkeypatch.setattr(providers, "get_embedder", lambda: _Dead())
    from app.db import SessionLocal
    from app.services import code_graph as code_svc

    db = SessionLocal()
    try:
        out = code_svc.describe_code(
            db, project_id="core",
            nodes=[{"path": "app/x.py", "name": "x", "summary": "does x", "kind": "file"}],
        )
        assert out["nodes_upserted"] == 1
        node = code_svc.list_nodes(db, "core")
        assert any(n.path == "app/x.py" and n.embedding is None for n in node)
    finally:
        db.close()


# ---- startup guard ------------------------------------------------------------
def test_hosted_stub_embeddings_warns_not_fatal(monkeypatch, caplog):
    """Warn loudly, but never strand a running deployment mid-migration."""
    from app.config import settings
    from app.security.startup import check_security

    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://x/y")  # skip sqlite short-circuit
    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "embed_provider", "stub")
    monkeypatch.setattr(settings, "secret_encryption_key", "k")
    monkeypatch.setattr(settings, "require_real_embeddings", False)
    check_security()
    assert "EMBED_PROVIDER is 'stub'" in caplog.text


def test_hosted_stub_embeddings_refuses_when_required(monkeypatch):
    from app.config import settings
    from app.security.startup import check_security

    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://x/y")
    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "embed_provider", "stub")
    monkeypatch.setattr(settings, "secret_encryption_key", "k")
    monkeypatch.setattr(settings, "require_real_embeddings", True)
    with pytest.raises(RuntimeError, match="EMBED_PROVIDER"):
        check_security()


def test_real_embed_provider_passes_the_guard(monkeypatch):
    from app.config import settings
    from app.security.startup import check_security

    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://x/y")
    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "embed_provider", "openai")
    monkeypatch.setattr(settings, "chat_provider", "anthropic")
    monkeypatch.setattr(settings, "secret_encryption_key", "k")
    monkeypatch.setattr(settings, "require_real_embeddings", True)
    monkeypatch.setattr(settings, "jwt_secret", "x" * 40)  # avoid the weak-secret path
    check_security()  # no raise


def _hosted(monkeypatch, **over):
    """Hosted-mode settings with every guard except the one under test satisfied."""
    from app.config import settings

    base = {
        "database_url": "postgresql+psycopg://x/y",  # skip the sqlite short-circuit
        "hosted_mode": True,
        "secret_encryption_key": "k",
        "embed_provider": "openai",
        "require_real_embeddings": False,
        "jwt_secret": "x" * 40,
    }
    for k, v in {**base, **over}.items():
        monkeypatch.setattr(settings, k, v)
    return settings


def test_no_startup_guard_on_the_legacy_chat_provider_mirror(monkeypatch, caplog):
    """`settings.chat_provider` is a legacy mirror that `platform.apply_llm` writes at
    runtime — the resolver reads `PlatformConfig.active_chat_provider` from the DB, per
    project. At boot the mirror is therefore always the env default, whatever projects
    have actually configured, so guarding on it warns forever on a healthy instance.

    This pins the decision: a stub mirror alone must produce NO chat complaint (AL-248).
    """
    from app.security.startup import check_security

    _hosted(monkeypatch, chat_provider="stub")
    check_security()
    assert "CHAT_PROVIDER" not in caplog.text


def test_health_reports_embedding_readiness(client, monkeypatch):
    """The startup warning only ever reached stdout, which nobody tails — the live
    instance ran on stub embeddings for days with it scrolling past. /health is the
    surface an operator actually curls (AL-248)."""
    from app.config import settings

    monkeypatch.setattr(settings, "embed_provider", "stub")
    assert client.get("/health").json()["providers"] == {"embed_ok": False}

    monkeypatch.setattr(settings, "embed_provider", "openai")
    assert client.get("/health").json()["providers"] == {"embed_ok": True}


def test_health_does_not_claim_anything_about_chat(client, monkeypatch):
    """Chat is per-project BYOK from the DB, so an instance-wide boolean would be
    actively misleading — it reads a process global that `apply_llm` mutates, and with
    more than one project it reflects whichever was applied last."""
    from app.config import settings

    monkeypatch.setattr(settings, "chat_provider", "anthropic")
    assert "chat_ok" not in client.get("/health").json()["providers"]


# ---- thinking is off on the wire ---------------------------------------------
class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self._lines


def test_ollama_chat_sends_think_false(monkeypatch):
    """A reasoning model (qwen3.x) defaults thinking ON, and this adapter throws the
    thinking stream away — so leaving the field unset pays for tokens nobody reads. On
    the live grill that was a 9x latency difference (64.7s vs 7.4s for the same PRD)."""
    sent = {}

    def fake_stream(method, url, **kw):
        sent.update(kw["json"])
        return _FakeStreamResponse([
            '{"message": {"thinking": "let me ponder"}}',
            '{"message": {"content": "answer"}}',
            '{"message": {"content": ""}, "done": true, "prompt_eval_count": 3, "eval_count": 1}',
        ])

    monkeypatch.setattr(httpx, "stream", fake_stream)
    chat = ollama.OllamaChat("https://gw.example", "qwen3.6:35b")
    assert chat.chat(system="s", context="c", question="q") == "answer"
    assert sent["think"] is False
    assert sent["stream"] is True
    assert sent["model"] == "qwen3.6:35b"


# ---- an empty answer is a failure, not a result -------------------------------
# Live on 2026-09-03: the xAI fallback streamed ~120s, ended with no content and no
# `[DONE]`, and "" became a grill result recorded ok=true. An absence must not read as clean.
from app.providers.base import EmptyAnswer


def test_ollama_chat_raises_on_thinking_only_stream(monkeypatch):
    """Everything the model produced was thinking (which the adapter drops) — the caller
    must get an error it can fail over on, not an empty string it will file as an answer."""
    def fake_stream(method, url, **kw):
        return _FakeStreamResponse([
            '{"message": {"thinking": "hmm"}}',
            '{"message": {"content": ""}, "done": true, "prompt_eval_count": 3, "eval_count": 9}',
        ])

    monkeypatch.setattr(httpx, "stream", fake_stream)
    chat = ollama.OllamaChat("https://gw.example", "qwen3.6:35b")
    with pytest.raises(EmptyAnswer) as ei:
        chat.chat(system="s", context="c", question="q")
    assert ei.value.retryable is True
    assert "empty answer" in str(ei.value) and "qwen3.6:35b" in str(ei.value)


class _FakeSSEResponse(_FakeStreamResponse):
    pass


def test_compat_chat_names_a_severed_stream(monkeypatch):
    """The live shape: bytes arrive, then the stream just ends — no content, no [DONE]."""
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeSSEResponse([
        'data: {"choices": [{"delta": {"reasoning_content": "thinking..."}}]}',
    ]))
    chat = openai_compat.OpenAICompatChat("https://api.x.ai/v1", "k", "grok-4.5")
    with pytest.raises(EmptyAnswer) as ei:
        chat.chat(system="s", context="c", question="q")
    assert ei.value.retryable is True
    assert "without [DONE]" in str(ei.value)


def test_compat_chat_distinguishes_a_completed_empty_answer(monkeypatch):
    """[DONE] arrived and there was still nothing: the model answered blank. Still an
    error — but it must NOT claim the stream was cut, because it was not."""
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeSSEResponse([
        'data: {"choices": [{"delta": {"content": "   "}}]}',
        'data: [DONE]',
    ]))
    chat = openai_compat.OpenAICompatChat("https://gw.example/v1", "k", "m")
    with pytest.raises(EmptyAnswer) as ei:
        chat.chat(system="s", context="c", question="q")
    assert "without [DONE]" not in str(ei.value)


def test_a_real_answer_still_passes_through_stripped(monkeypatch):
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeSSEResponse([
        'data: {"choices": [{"delta": {"content": "  fine  "}}]}',
        'data: [DONE]',
    ]))
    chat = openai_compat.OpenAICompatChat("https://gw.example/v1", "k", "m")
    assert chat.chat(system="s", context="c", question="q") == "fine"
