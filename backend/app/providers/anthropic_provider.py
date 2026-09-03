"""Anthropic (Claude) adapters for chat + extraction (opt-in cloud provider).

Uses the official `anthropic` SDK (optional dependency, imported lazily). Auth via
the standard ANTHROPIC_API_KEY env var. Model defaults to claude-opus-4-8.
"""
from __future__ import annotations

from app.config import settings
from app.providers.base import require_answer
from app.providers.toolcall import ToolCall, ToolResult, ToolSpec, ToolTurn

_MAX_TOKENS = 1024


def _client(api_key: str = ""):
    import anthropic  # lazy: only needed when the active provider is Anthropic

    # A UI-entered key wins; otherwise the SDK reads ANTHROPIC_API_KEY from the env.
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def _text(message) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text").strip()


class AnthropicChat:
    def __init__(self, model: str, api_key: str = ""):
        self.model = model
        self.api_key = api_key

    def chat(self, *, system: str, context: str, question: str,
             temperature: float | None = None) -> str:
        msg = _client(self.api_key).messages.create(
            model=self.model,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": f"Project context:\n{context}\n\nQuestion: {question}",
                }
            ],
        )
        from app.providers import llm_meter

        record = _usage(getattr(msg, "usage", None))  # exact counts ride the response (GRPH-225)
        if record:
            llm_meter.record_usage(**record)
        # Blank is a failure, not an answer (see `EmptyAnswer`).
        return require_answer(_text(msg), "anthropic", model=self.model)

    def stream(self, *, system: str, context: str, question: str,
               temperature: float | None = None):
        with _client(self.api_key).messages.stream(
            model=self.model,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[
                {"role": "user", "content": f"Project context:\n{context}\n\nQuestion: {question}"}
            ],
            **({"temperature": temperature} if temperature is not None else {}),
        ) as s:
            yield from s.text_stream
            # text_stream alone discards the final message; get_final_message() after it
            # is complete is documented and is the only place the usage lands here —
            # same call the tool session already makes in stream_turn.
            from app.providers import llm_meter

            u = _usage(getattr(s.get_final_message(), "usage", None))
            if u:
                llm_meter.record_usage(**u)

    def tool_session(self, *, system: str, context: str, question: str) -> "AnthropicToolSession":
        return AnthropicToolSession(self, system, context, question)


#: Prompt-cache accounting, carried on every turn (GRPH-226).
#:
#: **Recorded before caching is enabled, deliberately.** The documented behaviour is that a
#: prompt below the model's minimum cacheable length is "processed without caching, and no
#: error is returned" — so a `cache_control` breakpoint that does nothing is indistinguishable
#: from one that works, unless somebody is reading these two numbers. The docs name them as
#: the way to tell: if both are 0, the prompt was not cached.
#:
#: Extra keys rather than a new shape, because every consumer reads this dict with `.get`.
def _usage(usage) -> dict | None:
    if usage is None:
        return None
    return {
        "input": getattr(usage, "input_tokens", 0),
        "output": getattr(usage, "output_tokens", 0),
        # Both absent on providers that do not cache, and 0 on Anthropic when the prefix fell
        # short. Those two cases are different and neither is "it worked".
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


class AnthropicToolSession:
    """Owns the Anthropic-format message history for one tool-calling conversation."""

    def __init__(self, chat: AnthropicChat, system: str, context: str, question: str):
        self._chat = chat
        self.system = system
        self.messages: list[dict] = [
            {"role": "user", "content": f"Project context:\n{context}\n\nQuestion: {question}"}
        ]

    def run_turn(self, tools: list[ToolSpec]) -> ToolTurn:
        message = _client(self._chat.api_key).messages.create(
            model=self._chat.model,
            max_tokens=_MAX_TOKENS,
            system=self.system,
            tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            messages=self.messages,
        )
        # Echo the assistant turn's content blocks verbatim (the documented replay pattern).
        self.messages.append({"role": "assistant", "content": message.content})
        text, calls = [], []
        for block in message.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text.append(block.text)
            elif btype == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        usage = getattr(message, "usage", None)
        return ToolTurn(text="".join(text), tool_calls=calls,
                        wants_tools=message.stop_reason == "tool_use",
                        usage=_usage(usage))

    def stream_turn(self, tools: list[ToolSpec]):
        """Streaming run_turn (AL-183): yield text deltas, then return the ToolTurn.
        text_stream carries only text; tool_use blocks + usage + stop_reason come from
        the buffered final message, so tool-call parsing stays identical to run_turn."""
        text_parts: list[str] = []
        with _client(self._chat.api_key).messages.stream(
            model=self._chat.model,
            max_tokens=_MAX_TOKENS,
            system=self.system,
            tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            messages=self.messages,
        ) as s:
            for delta in s.text_stream:
                text_parts.append(delta)
                yield delta
            message = s.get_final_message()
        self.messages.append({"role": "assistant", "content": message.content})
        calls = [ToolCall(id=b.id, name=b.name, input=dict(b.input))
                 for b in message.content if getattr(b, "type", None) == "tool_use"]
        usage = getattr(message, "usage", None)
        return ToolTurn(text="".join(text_parts), tool_calls=calls,
                        wants_tools=message.stop_reason == "tool_use",
                        usage=_usage(usage))

    def add_results(self, results: list[ToolResult]) -> None:
        # Anthropic: a SINGLE user message carrying all tool_result blocks.
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.id, "content": r.content, "is_error": r.is_error}
            for r in results
        ]})


class AnthropicExtractor:
    def __init__(self, model: str, api_key: str = ""):
        self.model = model
        self.api_key = api_key

    def extract(self, *, title: str, description: str) -> list[str]:
        system = (
            "You distill a completed dev task into 1-3 durable, reusable memory shards "
            "(decisions, learnings, conventions). Reply with one shard per line, no numbering."
        )
        msg = _client(self.api_key).messages.create(
            model=self.model,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": f"Task: {title}\n\nDetails: {description}"}],
        )
        from app.providers import llm_meter

        record = _usage(getattr(msg, "usage", None))
        if record:
            llm_meter.record_usage(**record)
        return [ln.strip("-• ").strip() for ln in _text(msg).splitlines() if ln.strip()][:3]


def chat(api_key: str = "", model: str = "") -> AnthropicChat:
    return AnthropicChat(model or settings.anthropic_model, api_key)


def extractor(api_key: str = "", model: str = "") -> AnthropicExtractor:
    return AnthropicExtractor(model or settings.anthropic_model, api_key)
