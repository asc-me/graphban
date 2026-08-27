"""Provider registry (F1). Selects Embedder / ChatModel / Extractor from config.

Defaults are all-stub (offline). Selection is cached per-process; call reset() in
tests if you change settings at runtime.
"""
from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.providers.base import ChatModel, Embedder, Extractor, cosine_similarity
from app.providers.stub import StubChat, StubEmbedder, StubExtractor

from app.providers import registry

__all__ = [
    "Embedder",
    "ChatModel",
    "Extractor",
    "cosine_similarity",
    "get_embedder",
    "get_chat_model",
    "get_extractor",
    "iter_reply",
    "reset",
    "set_active_embedder",
    "set_active_chat",
    "build_chat",
    "build_embedder",
    "build_extractor",
]


# Active chat/extraction provider, set by platform.apply_llm from the DB (or by the legacy
# env path). Kept as plain module state so a switch takes effect immediately.
_active: dict = {"provider": "stub", "base_url": "", "api_key": "", "model": ""}


def set_active_chat(provider: str = "stub", *, base_url: str = "", api_key: str = "", model: str = "") -> None:
    _active.update(
        provider=provider or "stub", base_url=base_url or "", api_key=api_key or "", model=model or ""
    )
    get_chat_model.cache_clear()
    get_extractor.cache_clear()


def build_chat(provider: str = "stub", *, base_url: str = "", api_key: str = "", model: str = "") -> ChatModel:
    """Construct a chat adapter from an explicit provider config — no global state, so
    each project can resolve its own provider at call time (platform.resolve_chat)."""
    provider = provider or "stub"
    if provider == "ollama":
        from app.providers import ollama

        return ollama.chat(base_url=base_url, model=model, auth_key=api_key)
    if provider == "anthropic":
        from app.providers import anthropic_provider

        return anthropic_provider.chat(api_key=api_key, model=model)
    if registry.is_openai_compat(provider):
        from app.providers import openai_compat

        meta = registry.get(provider) or {}
        return openai_compat.chat(base_url or meta.get("base_url", ""), api_key, model or meta.get("chat_model", ""))
    return StubChat()


def build_extractor(provider: str = "stub", *, base_url: str = "", api_key: str = "", model: str = "") -> Extractor:
    """Extractor counterpart of build_chat — same per-project resolution, no global state."""
    provider = provider or "stub"
    if provider == "ollama":
        from app.providers import ollama

        return ollama.extractor(base_url=base_url, model=model, auth_key=api_key)
    if provider == "anthropic":
        from app.providers import anthropic_provider

        return anthropic_provider.extractor(api_key=api_key, model=model)
    if registry.is_openai_compat(provider):
        from app.providers import openai_compat

        meta = registry.get(provider) or {}
        return openai_compat.extractor(base_url or meta.get("base_url", ""), api_key, model or meta.get("chat_model", ""))
    return StubExtractor()


def build_embedder(provider: str = "stub", *, base_url: str = "", api_key: str = "",
                   model: str = "") -> Embedder:
    """Construct an embedder from an explicit config — the counterpart to `build_chat`.

    `get_embedder()` reads process-global settings and is cached, which is right for the
    env-configured default and wrong for two things PRD-25 S4 needs: probing a CANDIDATE
    credential before accepting it, and serving a deployment credential that a settings change
    can alter without a restart.

    **The dimension still comes from `settings.embed_dim`.** That is not a shortcut — the
    vector column's width is fixed when the models import, so an embedder built here that
    claimed a different dimension would be describing a column that does not exist. The gate in
    `services.embedder` is what refuses a mismatch, by measuring what the provider actually
    returns.
    """
    provider = provider or "stub"
    if provider == "ollama":
        from app.providers import ollama

        return ollama.OllamaEmbedder(
            base_url or settings.ollama_base_url,
            model or settings.ollama_embed_model,
            settings.embed_dim,
            api_key or settings.ollama_auth_key,
        )
    if provider in ("openai", "openai_compat") or registry.is_openai_compat(provider):
        from app.providers import openai as openai_provider

        meta = registry.get(provider) or {}
        return openai_provider.OpenAIEmbedder(
            base_url or meta.get("base_url", "") or settings.openai_base_url,
            api_key or settings.openai_api_key,
            model or settings.openai_embed_model,
            settings.embed_dim,
        )
    return StubEmbedder()


def safe_embed(text: str) -> list[float] | None:
    """Embed for an INGEST path: never raise, return None if the provider is down.

    A row is worth more than its vector. When the embedding endpoint is unreachable
    (cold, rate-limited, gateway down) we'd rather store the shard or code node with a
    NULL embedding than lose the write — the existing backfill (`/api/memory/backfill`,
    and the code-graph equivalent) is the retry mechanism that fills those in later.
    Query paths deliberately do NOT use this: a search that silently matches nothing is
    worse than one that reports an error."""
    import logging

    try:
        return get_embedder().embed(text)
    except Exception:  # noqa: BLE001 — ingest must survive a dead embedder
        logging.getLogger("graphban.providers").warning(
            "embedding failed; storing row without a vector (backfill will fill it in)",
            exc_info=True,
        )
        return None


def iter_reply(model: ChatModel, *, system: str, context: str, question: str):
    """Yield reply chunks. Uses the provider's native stream() when available."""
    streamer = getattr(model, "stream", None)
    if callable(streamer):
        yield from streamer(system=system, context=context, question=question)
    else:
        yield model.chat(system=system, context=context, question=question)


# The deployment's embedding credential, when one is configured (PRD-25 S4). Plain module
# state for the same reason `_active` is: a switch has to take effect immediately, and the
# alternative is threading a Session into every call site that embeds.
_active_embed: dict = {}


def set_active_embedder(provider: str = "", *, base_url: str = "", api_key: str = "",
                        model: str = "") -> None:
    _active_embed.update(provider=provider, base_url=base_url, api_key=api_key, model=model)
    get_embedder.cache_clear()


@lru_cache
def get_embedder() -> Embedder:
    if _active_embed.get("provider"):
        # A deployment credential was configured; it wins over the env default.
        return build_embedder(_active_embed["provider"], base_url=_active_embed["base_url"],
                              api_key=_active_embed["api_key"], model=_active_embed["model"])
    p = settings.embed_provider
    if p == "ollama":
        from app.providers import ollama

        return ollama.embedder()
    if p == "openai":
        from app.providers import openai

        return openai.embedder()
    return StubEmbedder()


@lru_cache
def get_chat_model() -> ChatModel:
    """The process-global chat model (env/legacy default via set_active_chat). Per-project
    call sites resolve their own via platform.resolve_chat; this stays for env-only setups."""
    return build_chat(_active["provider"], base_url=_active["base_url"], api_key=_active["api_key"], model=_active["model"])


@lru_cache
def get_extractor() -> Extractor:
    return build_extractor(_active["provider"], base_url=_active["base_url"], api_key=_active["api_key"], model=_active["model"])


def reset() -> None:
    get_embedder.cache_clear()
    get_chat_model.cache_clear()
    get_extractor.cache_clear()
