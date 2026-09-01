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


def build_chat(provider: str = "stub", *, base_url: str = "", api_key: str = "", model: str = "",
             project_id: str = "") -> ChatModel:
    """Construct a chat adapter from an explicit provider config — no global state, so
    each project can resolve its own provider at call time (platform.resolve_chat).

    Every return is run through `llm_meter.metered` (GRPH-225): the construction point
    is the only place that knows BOTH the provider id and the resolved model, and every
    public path to an adapter — global, per-project, env — passes through here. Wrapping
    here instead of in each adapter is what keeps a tenth provider entry from being a
    tenth instrumentation site."""
    provider = provider or "stub"
    if provider == "ollama":
        from app.providers import llm_meter, ollama

        return llm_meter.metered(ollama.chat(base_url=base_url, model=model, auth_key=api_key),
                                provider="ollama", project_id=project_id)
    if provider == "anthropic":
        from app.providers import anthropic_provider, llm_meter

        return llm_meter.metered(anthropic_provider.chat(api_key=api_key, model=model),
                                 provider="anthropic", project_id=project_id)
    if registry.is_openai_compat(provider):
        from app.providers import llm_meter, openai_compat

        meta = registry.get(provider) or {}
        resolved = model or meta.get("chat_model", "")
        return llm_meter.metered(
            openai_compat.chat(base_url or meta.get("base_url", ""), api_key, resolved),
            provider=provider, model=resolved, project_id=project_id)
    from app.providers import llm_meter

    return llm_meter.metered(StubChat(), provider="stub", project_id=project_id)


def build_extractor(provider: str = "stub", *, base_url: str = "", api_key: str = "", model: str = "",
                    project_id: str = "") -> Extractor:
    """Extractor counterpart of build_chat — same per-project resolution, no global state."""
    provider = provider or "stub"
    if provider == "ollama":
        from app.providers import llm_meter, ollama

        return llm_meter.metered(ollama.extractor(base_url=base_url, model=model, auth_key=api_key),
                                 provider="ollama", project_id=project_id)
    if provider == "anthropic":
        from app.providers import anthropic_provider, llm_meter

        return llm_meter.metered(anthropic_provider.extractor(api_key=api_key, model=model),
                                 provider="anthropic", project_id=project_id)
    if registry.is_openai_compat(provider):
        from app.providers import llm_meter, openai_compat

        meta = registry.get(provider) or {}
        resolved = model or meta.get("chat_model", "")
        return llm_meter.metered(
            openai_compat.extractor(base_url or meta.get("base_url", ""), api_key, resolved),
            provider=provider, model=resolved, project_id=project_id)
    from app.providers import llm_meter

    return llm_meter.metered(StubExtractor(), provider="stub", project_id=project_id)


def build_embedder(provider: str = "stub", *, base_url: str = "", api_key: str = "",
                   model: str = "", project_id: str = "") -> Embedder:
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
        from app.providers import llm_meter, ollama

        return llm_meter.metered(ollama.OllamaEmbedder(
            base_url or settings.ollama_base_url,
            model or settings.ollama_embed_model,
            settings.embed_dim,
            api_key or settings.ollama_auth_key,
        ), provider="ollama", project_id=project_id)
    if provider in ("openai", "openai_compat") or registry.is_openai_compat(provider):
        from app.providers import llm_meter
        from app.providers import openai as openai_provider

        meta = registry.get(provider) or {}
        return llm_meter.metered(openai_provider.OpenAIEmbedder(
            base_url or meta.get("base_url", "") or settings.openai_base_url,
            api_key or settings.openai_api_key,
            model or settings.openai_embed_model,
            settings.embed_dim,
        ), provider=provider, project_id=project_id)
    from app.providers import llm_meter

    return llm_meter.metered(StubEmbedder(), provider="stub")


def safe_embed(text: str) -> list[float] | None:
    """Embed for an INGEST path: never raise, return None if the provider is down.

    A row is worth more than its vector. When the embedding endpoint is unreachable
    (cold, rate-limited, gateway down) we'd rather store the shard or code node with a
    NULL embedding than lose the write — the existing backfill (`/api/memory/backfill`,
    and the code-graph equivalent) is the retry mechanism that fills those in later.
    Query paths deliberately do NOT use this: a search that silently matches nothing is
    worse than one that reports an error."""
    import logging

    from app.providers import llm_meter

    try:
        with llm_meter.llm_context(feature="embed.write" if not llm_meter.feature_var.get() else ""):
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
    # The env path bypasses build_embedder, so it wraps here — every embedder instance
    # in the process carries a span wrapper, which is what lets the tests below assert
    # "a call produced a row" without knowing which config path the box uses.
    from app.providers import llm_meter

    p = settings.embed_provider
    if p == "ollama":
        from app.providers import ollama

        return llm_meter.metered(ollama.embedder(), provider="ollama")
    if p == "openai":
        from app.providers import openai

        return llm_meter.metered(openai.embedder(), provider="openai")
    return llm_meter.metered(StubEmbedder(), provider="stub")


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
