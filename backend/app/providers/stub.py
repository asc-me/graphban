"""Offline, deterministic providers — the zero-dependency default.

These run with no external services and give stable, testable behavior.
"""
from __future__ import annotations

import hashlib
import math
import re

from app.config import settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_LESSON_MARKERS = ("decided", "learning", "convention", "must", "avoid", "fix", "fallback")


class StubEmbedder:
    """Hashed bag-of-tokens → L2-normalized vector. Same text → same vector."""

    def __init__(self, dim: int = settings.embed_dim):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN_RE.findall((text or "").lower()):
            h = hashlib.sha256(tok.encode()).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            vec[idx] += 1.0 if h[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]


class StubChat:
    """Retrieval-grounded, no-LLM chat. Composes an answer from the given context."""

    def chat(self, *, system: str, context: str, question: str,
             temperature: float | None = None) -> str:
        lines = [context.strip()] if context.strip() else []
        lines.append(
            "(Local stub agent — no external model configured. "
            "Set CHAT_PROVIDER=ollama or anthropic for generative replies.)"
        )
        return "\n\n".join(lines)

    def stream(self, *, system: str, context: str, question: str,
               temperature: float | None = None):
        reply = self.chat(system=system, context=context, question=question)
        for i in range(0, len(reply), 24):  # emit in chunks to simulate token stream
            yield reply[i : i + 24]

    def tool_session(self, *, system: str, context: str, question: str):
        """No-op tool session (AL-172): the offline stub can't call tools, so it answers
        in one text-only turn and the driver terminates immediately — graceful
        degradation when no real provider is configured."""
        from app.providers.toolcall import ToolTurn

        reply = self.chat(system=system, context=context, question=question)

        class _StubToolSession:
            def run_turn(self, tools):
                return ToolTurn(text=reply, tool_calls=[], wants_tools=False)

            def stream_turn(self, tools):  # emit in chunks to simulate a token stream (AL-183)
                for i in range(0, len(reply), 24):
                    yield reply[i : i + 24]
                return ToolTurn(text=reply, tool_calls=[], wants_tools=False)

            def add_results(self, results):  # never reached (wants_tools is always False)
                pass

        return _StubToolSession()


# `insights._extraction_source` labels the original proposal as possibly stale
# (GRPH-358). Extracting from it is how the stub reproduced the defect the label
# exists to prevent. Matched as a prefix so a lesson that merely mentions the
# phrase is not cut.
_PROPOSAL_MARK = "ORIGINAL PROPOSAL"


class StubExtractor:
    """Heuristic lesson extraction: pull decision/learning-flavored sentences."""

    def extract(self, *, title: str, description: str) -> list[str]:
        body = description or ""
        cut = body.find(_PROPOSAL_MARK)
        source = body[:cut] if cut >= 0 else body
        # Headings are not terminated with `. `, so they glue to the next
        # bullet if we sentence-split first — and dropping that "sentence"
        # would throw away the outcome the wrap exists to privilege.
        kept = []
        for line in source.splitlines():
            s = line.strip()
            if not s or s.startswith("WHAT ACTUALLY HAPPENED") or s.startswith(_PROPOSAL_MARK):
                continue
            kept.append(s)
        source = " ".join(kept)
        sentences = [s.strip() for s in _SENT_RE.split(source) if s.strip()]
        hits = [s for s in sentences if any(m in s.lower() for m in _LESSON_MARKERS)]
        if hits:
            return hits[:3]
        # Fall back to a single completion note so `done` items always leave a
        # trace. Do not append the first leftover sentence: after a cut that is
        # often an evidence bullet, which is not a lesson.
        return [f"Completed: {title}."]
