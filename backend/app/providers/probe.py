"""Ask a provider what models it actually has (GRPH-485).

**A model name nobody checked is a config that fails at every call site instead of once.**
On 2026-08-25 a project's `chat_model` was set to `qwen3.6:35b-a3b-coding-mtp`, a tag the
Ollama host did not have. The value saved cleanly, every chat call returned
`model ... not found`, and the PRD grill it broke reported "your answers are still
outstanding" for an hour — because a grader that cannot run and an author who under-answered
produce the same response. The name was one edit from correct and nothing said so.

**`None` and the empty set are different answers and must stay different.** `None` means
*this provider cannot be asked* — Anthropic ships no listing endpoint, the stub has no
models. An empty set means *asked, and it has none*. Collapsing them would refuse every
model on a provider that simply cannot enumerate, which is worse than not checking at all.
This is the same contract `gbfleet.adapters.Adapter.known_models` uses for vendor CLIs, and
deliberately so: two places asking "what can this thing run" should answer in one shape.

Never raises. An unreachable provider is `None` — *cannot be asked* — not a refusal, because
a network blip must not block a correct edit.
"""
from __future__ import annotations

import logging

import httpx

from app.providers import registry

logger = logging.getLogger("graphban.providers.probe")

#: Short. This runs inside a config save, where a human is waiting on a form.
TIMEOUT = 6.0


def _ollama(base_url: str, api_key: str) -> set[str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.get(f"{base_url.rstrip('/')}/api/tags", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    # Ollama names carry the tag (`qwen3.6:35b-a3b-coding-mtp-q4_K_M`), and the tag is
    # exactly what was wrong in the incident above, so the match must be on the full name.
    return {m["name"] for m in (r.json().get("models") or []) if m.get("name")}


def _openai_compat(base_url: str, api_key: str) -> set[str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return {m["id"] for m in (r.json().get("data") or []) if m.get("id")}


def known_models(provider_id: str, base_url: str, api_key: str = "") -> frozenset[str] | None:
    """What this provider says it can run, or `None` when it cannot be asked.

    `None` is returned for a provider with no listing endpoint AND for one that could not
    be reached. Both are honestly "unchecked": refusing a save because a host was briefly
    down would break a correct edit for a reason that has nothing to do with the edit.
    """
    if provider_id not in registry.LISTS_MODELS:
        return None
    if not base_url:
        return None
    try:
        if provider_id == "ollama":
            names = _ollama(base_url, api_key)
        else:
            names = _openai_compat(base_url, api_key)
    except Exception:  # noqa: BLE001 — unreachable is "unchecked", never "invalid"
        logger.info("model probe: %s at %s could not be asked", provider_id, base_url,
                    exc_info=True)
        return None
    return frozenset(names)
