"""Local/self-hosted Ollama adapters (opt-in). Reachable at base_url — local,
over Tailscale, or a public endpoint guarded by a reverse proxy (Caddy) that wants a
bearer token (`auth_key`). Chat + embedding models are configured separately.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from app.config import settings
from app.providers.base import provider_errors

logger = logging.getLogger("graphban.providers.ollama")


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.llm_timeout_seconds, connect=5.0)


def _headers(auth_key: str) -> dict:
    return {"Authorization": f"Bearer {auth_key}"} if auth_key else {}


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, dim: int, auth_key: str = ""):
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_embed_model
        self.dim = dim
        self.auth_key = auth_key or ""

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch in ONE request, falling back to per-text calls if unsupported.

        Ollama's newer `/api/embed` takes an array in `input`; the older `/api/embeddings`
        takes a single `prompt`. The re-index needs the batch path — a round trip per row is an
        order of magnitude worse, and 1,103 shards at one request each is the difference
        between half a minute and several (GRPH-536).

        **The fallback is not defensive habit.** This code cannot verify which endpoint a given
        Ollama build serves, and a deployment on an older one must keep working rather than
        fail a re-index with a 404. When it triggers, the request-count property this batching
        exists for is genuinely lost — so it logs, rather than degrading quietly.
        """
        if not texts:
            return []
        try:
            r = httpx.post(
                f"{self.base_url}/api/embed",
                headers=_headers(self.auth_key),
                json={"model": self.model, "input": list(texts)},
                timeout=_timeout(),
            )
            r.raise_for_status()
            body = r.json()
            vectors = body.get("embeddings")
            from app.providers import llm_meter

            llm_meter.record_usage(input=body.get("prompt_eval_count"))
            if isinstance(vectors, list) and len(vectors) == len(texts):
                return vectors
            logger.warning("ollama /api/embed returned %s vectors for %d inputs; falling back "
                           "to one request per row",
                           len(vectors) if isinstance(vectors, list) else "no", len(texts))
        except Exception as e:  # noqa: BLE001 — any failure means "use the older endpoint"
            logger.warning("ollama /api/embed unavailable (%s); falling back to one request "
                           "per row, which is much slower", e)
        return [self.embed(t) for t in texts]

    def embed(self, text: str) -> list[float]:
        """Embed one string, retrying transient failures — a cold model behind a
        gateway can be slow on the first call, and a blip shouldn't cost an ingest.
        Callers that must not fail use `safe_embed`."""
        attempts = max(1, settings.embed_max_retries + 1)
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                r = httpx.post(
                    f"{self.base_url}/api/embeddings",
                    headers=_headers(self.auth_key),
                    json={"model": self.model, "prompt": text or ""},
                    timeout=_timeout(),
                )
                r.raise_for_status()
                payload = r.json()
                from app.providers import llm_meter

                llm_meter.record_usage(input=payload.get("prompt_eval_count"))
                return payload["embedding"]
            except Exception as e:  # noqa: BLE001 — retried, then re-raised below
                last = e
                if attempt + 1 < attempts:
                    logger.warning("embed attempt %d/%d failed: %s", attempt + 1, attempts, e)
                    time.sleep(0.5 * (attempt + 1))
        raise last  # type: ignore[misc]


class OllamaChat:
    def __init__(self, base_url: str, model: str, auth_key: str = ""):
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_chat_model
        self.auth_key = auth_key or ""

    def _msgs(self, system: str, context: str, question: str) -> list[dict]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Project context:\n{context}\n\nQuestion: {question}"},
        ]

    def chat(self, *, system: str, context: str, question: str,
             temperature: float | None = None) -> str:
        """Full completion as a string — assembled from the STREAM, not a blocking POST.

        See the note in providers/openai_compat.py: a blocking completion's
        time-to-first-byte equals total generation time, which an edge proxy capping
        TTFB (~100s on Cloudflare) will sever on a long answer. Streaming makes the
        first byte immediate while keeping the identical `-> str` contract."""
        return "".join(self.stream(system=system, context=context, question=question,
                                   temperature=temperature)).strip()

    def stream(self, *, system: str, context: str, question: str,
               temperature: float | None = None):
        # The connection and status check happen on the first `next()`, so wrapping the
        # generator body is what turns a misconfigured endpoint into an actionable error
        # instead of the dispatcher's generic "internal … safe to retry once".
        with provider_errors("ollama", model=self.model, endpoint=self.base_url), httpx.stream(
            "POST",
            f"{self.base_url}/api/chat",
            headers=_headers(self.auth_key),
            json={"model": self.model, "messages": self._msgs(system, context, question),
                  "stream": True,
                  **({"options": {"temperature": temperature}} if temperature is not None else {})},
            timeout=_timeout(),
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                obj = json.loads(line)
                piece = obj.get("message", {}).get("content", "")
                if piece:
                    yield piece
                if obj.get("done"):
                    # The done line carries the counts (GRPH-225): prompt_eval_count /
                    # eval_count are ollama's, exact when present.
                    from app.providers import llm_meter

                    llm_meter.record_usage(input=obj.get("prompt_eval_count"),
                                           output=obj.get("eval_count"))
                    break

    def tool_session(self, *, system: str, context: str, question: str):
        """Tool-calling for the in-app assistant (AL-184). Ollama exposes an
        OpenAI-compatible endpoint at `{base_url}/v1`, so we reuse the shared
        OpenAI-compat session — tool-call parsing and AL-183 token streaming come for
        free. Needs a model that supports tools (e.g. qwen2.5-coder, llama3.1); a model
        without tool support just won't emit tool_calls and answers in plain text."""
        from app.providers.openai_compat import OpenAICompatChat

        oai = OpenAICompatChat(f"{self.base_url}/v1", self.auth_key, self.model)
        return oai.tool_session(system=system, context=context, question=question)


class OllamaExtractor:
    def __init__(self, base_url: str, model: str, auth_key: str = ""):
        self._chat = OllamaChat(base_url, model, auth_key)

    def extract(self, *, title: str, description: str) -> list[str]:
        system = (
            "You distill a completed dev task into 1-3 durable, reusable memory shards "
            "(decisions, learnings, conventions). Reply with one shard per line, no numbering."
        )
        out = self._chat.chat(
            system=system, context="", question=f"Task: {title}\n\nDetails: {description}"
        )
        return [ln.strip("-• ").strip() for ln in out.splitlines() if ln.strip()][:3]


def embedder() -> OllamaEmbedder:
    return OllamaEmbedder(
        settings.ollama_base_url, settings.ollama_embed_model, settings.embed_dim, settings.ollama_auth_key
    )


def chat(base_url: str = "", model: str = "", auth_key: str = "") -> OllamaChat:
    return OllamaChat(base_url, model, auth_key)


def extractor(base_url: str = "", model: str = "", auth_key: str = "") -> OllamaExtractor:
    return OllamaExtractor(base_url, model, auth_key)
