"""AI provider registry — the catalog the Settings UI lists and the provider layer
resolves against. One entry per provider; `kind` picks the adapter:

- `stub`     — deterministic offline (no config)
- `anthropic`— native Claude SDK
- `openai`   — any OpenAI-compatible /chat/completions API (OpenAI, Groq, DeepSeek,
               Mistral, xAI, Gemini's compat endpoint, the CN labs, the hosted
               open-weights providers) — base_url + api_key + model
- `ollama`   — local/self-hosted Ollama (base_url, optional bearer for a Caddy-guarded
               endpoint, chat + embedding models)

`embeds` marks providers that can also serve embeddings. NOTE: switching the embedding
provider/model changes the vector dimension, so it stays a deploy-time choice (EMBED_PROVIDER
+ EMBED_DIM) — only the base_url/model are read from this config.

Optional `models` lists the selectable chat models the picker offers for a provider (e.g.
xAI's general `grok-4.5` and coding-tuned `grok-build-0.1`); when absent the picker offers
just `chat_model`. `chat_model` remains the out-of-box default.
"""
from __future__ import annotations

PROVIDERS: list[dict] = [
    {"id": "stub", "label": "Offline stub", "kind": "stub", "embeds": True,
     "base_url": "", "chat_model": "", "embed_model": "", "auth": False},
    {"id": "anthropic", "label": "Anthropic", "kind": "anthropic", "embeds": False,
     "base_url": "", "chat_model": "claude-opus-4-8", "embed_model": "", "auth": True},
    {"id": "openai", "label": "OpenAI", "kind": "openai", "embeds": True,
     "base_url": "https://api.openai.com/v1", "chat_model": "gpt-4o-mini",
     "embed_model": "text-embedding-3-small", "auth": True},
    {"id": "gemini", "label": "Google Gemini", "kind": "openai", "embeds": True,
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "chat_model": "gemini-2.0-flash", "embed_model": "text-embedding-004", "auth": True},
    {"id": "xai", "label": "xAI Grok", "kind": "openai", "embeds": False,
     "base_url": "https://api.x.ai/v1", "chat_model": "grok-4.5",
     "models": ["grok-4.5", "grok-build-0.1"], "embed_model": "", "auth": True},
    {"id": "groq", "label": "Groq", "kind": "openai", "embeds": False,
     "base_url": "https://api.groq.com/openai/v1", "chat_model": "llama-3.3-70b-versatile",
     "embed_model": "", "auth": True},
    {"id": "deepseek", "label": "DeepSeek", "kind": "openai", "embeds": False,
     "base_url": "https://api.deepseek.com/v1", "chat_model": "deepseek-chat", "embed_model": "", "auth": True},
    {"id": "mistral", "label": "Mistral", "kind": "openai", "embeds": False,
     "base_url": "https://api.mistral.ai/v1", "chat_model": "mistral-large-latest", "embed_model": "", "auth": True},
    {"id": "ollama", "label": "Ollama", "kind": "ollama", "embeds": True,
     "base_url": "http://localhost:11434", "chat_model": "qwen2.5-coder",
     "embed_model": "nomic-embed-text", "auth": True},
    # GRPH-625. The CN labs and the hosted open-weights providers, all OpenAI-compat wire —
    # registry entries, not new adapters. `chat_model`/`models` are best-known-at-filing
    # defaults, NOT gospel: every one of them is checked against the provider's live
    # /v1/models on save (probe.known_models), and a wrong guess surfaces there as a 422
    # that lists what the provider actually offers. That is why the list errs toward
    # long-lived aliases (qwen-plus, kimi-latest) over dated snapshot names.
    {"id": "qwen", "label": "Qwen (DashScope)", "kind": "openai", "embeds": False,
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "chat_model": "qwen-plus", "models": ["qwen-plus", "qwen-max", "qwen-turbo"],
     "embed_model": "", "auth": True},
    {"id": "kimi", "label": "Kimi (Moonshot)", "kind": "openai", "embeds": False,
     "base_url": "https://api.moonshot.ai/v1", "chat_model": "kimi-latest",
     "embed_model": "", "auth": True},
    {"id": "glm", "label": "GLM (Z.ai)", "kind": "openai", "embeds": False,
     "base_url": "https://api.z.ai/api/paas/v4", "chat_model": "glm-4.5",
     "models": ["glm-4.5", "glm-4.5-air"], "embed_model": "", "auth": True},
    {"id": "minimax", "label": "MiniMax", "kind": "openai", "embeds": False,
     "base_url": "https://api.minimax.io/v1", "chat_model": "MiniMax-M2",
     "embed_model": "", "auth": True},
    {"id": "openrouter", "label": "OpenRouter", "kind": "openai", "embeds": False,
     "base_url": "https://openrouter.ai/api/v1", "chat_model": "anthropic/claude-sonnet-5",
     # Verified live against openrouter.ai/api/v1/models on 2026-09-01 (its listing is
     # public, so these are the only defaults here checked by evidence and not by memory).
     "models": ["anthropic/claude-sonnet-5", "openai/gpt-5.6-luna", "moonshotai/kimi-k3",
                "z-ai/glm-5.3", "deepseek/deepseek-v4-pro-0813"],
     "embed_model": "", "auth": True},
    {"id": "together", "label": "Together AI", "kind": "openai", "embeds": False,
     "base_url": "https://api.together.xyz/v1", "chat_model": "deepseek-ai/DeepSeek-V3",
     "embed_model": "", "auth": True},
    {"id": "fireworks", "label": "Fireworks AI", "kind": "openai", "embeds": False,
     # No default model: Fireworks serves per-account endpoints, so guessing a public id
     # here would be a wrong guess on half the deployments. The form requires the operator
     # to name one and the probe checks it.
     "base_url": "https://api.fireworks.ai/inference/v1", "chat_model": "",
     "embed_model": "", "auth": True},
    {"id": "perplexity", "label": "Perplexity", "kind": "openai", "embeds": False,
     # /v1 was 401 (routed) where bare /models was 404 (unknown), and the quickstart
     # documents /router/v1/chat/completions — probed 2026-09-01.
     "base_url": "https://api.perplexity.ai/router/v1", "chat_model": "sonar-pro",
     "models": ["sonar-pro", "sonar"], "embed_model": "", "auth": True},
    {"id": "cohere", "label": "Cohere", "kind": "openai", "embeds": False,
     "base_url": "https://api.cohere.com/v2", "chat_model": "command-a-03-2025",
     "models": ["command-a-03-2025", "command-r-plus"], "embed_model": "", "auth": True},
    # The generic shape of the `openai` kind finally gets an entry: vLLM, LM Studio,
    # llama.cpp's server, an internal LiteLLM gateway — anything that speaks the wire but
    # has no business being in a shipped catalogue. Empty base_url + the visible endpoint
    # field IS the feature; adding a provider per local server is what this replaces.
    {"id": "custom", "label": "Custom (OpenAI-compat)", "kind": "openai", "embeds": False,
     "base_url": "", "chat_model": "", "embed_model": "", "auth": True},
]

_BY_ID = {p["id"]: p for p in PROVIDERS}
IDS = set(_BY_ID)
OPENAI_COMPAT = {p["id"] for p in PROVIDERS if p["kind"] == "openai"}


def get(pid: str) -> dict | None:
    return _BY_ID.get(pid)


def is_openai_compat(pid: str) -> bool:
    return pid in OPENAI_COMPAT


def kind(pid: str) -> str:
    p = _BY_ID.get(pid)
    return p["kind"] if p else "stub"


# ---- Can this provider be ASKED what models it has? (GRPH-485) -------------------------
#
# `kind` decides. Ollama lists at `/api/tags`; every OpenAI-compatible endpoint lists at
# `/v1/models`. Anthropic has no listing endpoint and the stub has no models, so both
# answer "cannot be asked" rather than "has none" — a distinction the caller must keep,
# because refusing every model on a provider that simply cannot enumerate would make the
# check worse than its absence.
LISTS_MODELS = {p["id"] for p in PROVIDERS if p["kind"] in ("ollama", "openai")}
