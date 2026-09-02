"""OpenAI-compatible embeddings adapter (opt-in).

Anthropic has no embeddings endpoint, so cloud embeddings go through any
OpenAI-compatible `/v1/embeddings` API (OpenAI, or a self-hosted gateway such as an
Ollama instance exposing the compat surface, where the model is chosen by the
`model` field rather than a separate endpoint).
"""
from __future__ import annotations

import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger("graphban.providers.openai")


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.llm_timeout_seconds, connect=5.0)


class OpenAIEmbedder:
    def __init__(self, base_url: str, api_key: str, model: str, dim: int):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        """Embed one string, retrying transient failures.

        A cold model behind a gateway can take a while on the first call, and a blip
        shouldn't cost an ingest — so retry a bounded number of times with a short
        backoff before giving up. Callers that must not fail use `safe_embed`."""
        attempts = max(1, settings.embed_max_retries + 1)
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                r = httpx.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": text or ""},
                    timeout=_timeout(),
                )
                r.raise_for_status()
                payload = r.json()
                from app.providers import llm_meter

                u = payload.get("usage") or {}
                llm_meter.record_usage(input=u.get("prompt_tokens") or u.get("total_tokens"))
                return payload["data"][0]["embedding"]
            except Exception as e:  # noqa: BLE001 — retried, then re-raised below
                last = e
                if attempt + 1 < attempts:
                    logger.warning("embed attempt %d/%d failed: %s", attempt + 1, attempts, e)
                    time.sleep(0.5 * (attempt + 1))
        raise last  # type: ignore[misc]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One request for the whole batch — the OpenAI embeddings endpoint takes an array.

        Order is not assumed: the response carries an `index` per item and the vectors are
        placed by it. An endpoint that returned them out of order would otherwise attach every
        vector to the wrong row, which is the kind of corruption that looks like a bad model
        rather than a bad loop.
        """
        if not texts:
            return []
        r = httpx.post(
            f"{self.base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": list(texts)},
            timeout=_timeout() if callable(globals().get("_timeout")) else 60.0,
        )
        r.raise_for_status()
        body = r.json()
        rows = body["data"]
        from app.providers import llm_meter

        u = body.get("usage") or {}
        llm_meter.record_usage(input=u.get("prompt_tokens") or u.get("total_tokens"))
        out: list[list[float]] = [[] for _ in texts]
        for row in rows:
            out[row.get("index", rows.index(row))] = row["embedding"]
        return out


def embedder() -> OpenAIEmbedder:
    return OpenAIEmbedder(
        settings.openai_base_url, settings.openai_api_key, settings.openai_embed_model, settings.embed_dim
    )
