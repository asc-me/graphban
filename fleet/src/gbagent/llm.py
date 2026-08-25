"""The conversation with a local model (PRD-24 S3, D6).

**This is the package's second network door, and it is deliberately not the first one.**
`gbfleet/client.py` talks to Graphban and checks an allowlist on every call, because the
supervisor holds a credential with authority. This module talks to a model endpoint, which
holds no authority over anything: it cannot claim work, sign off, or move an item. Nothing here
references Graphban, and `test_client.py` asserts that rather than trusting it.

**Why the contract is re-stated rather than imported.** `app.providers.toolcall` already defines
exactly these four shapes and the driver that runs them, and importing it is impossible on
purpose: `graphban-fleet` is a separate distribution that must not drag in the backend stack
(PRD-22 D-e/G6), and `test_packaging.py` fails on any `from app.` in this tree. So the contract
is repeated here in ~40 lines rather than the whole backend being installed on a laptop. The
duplication is the price of the split, and it is the smaller bill.

**Arguments are always decoded at the boundary.** The wire returns `function.arguments` as a
JSON *string* — verified against `ms-s1-ubt` for `qwen3-coder:30b` — so callers never see a
provider's encoding. This is the AL-180 parity choice, and it is why `ToolCall.input` is a dict.
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field

import httpx


@dataclass(frozen=True)
class ToolSpec:
    """A tool advertised to the model. `input_schema` is JSON Schema."""

    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    """A tool the model asked us to run. `input` is ALWAYS decoded."""

    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class ToolResult:
    """The outcome of running a tool, fed back to the model.

    `is_error` is what makes a refusal correctable rather than fatal (D2): a write outside the
    worktree comes back as a result the model reads and can retry, not a crash.
    """

    id: str
    content: str
    is_error: bool = False


@dataclass
class ToolTurn:
    """One model turn, normalized."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    wants_tools: bool = False
    #: {input, output} token counts when the endpoint reports them. S4's compaction threshold
    #: is measured in tokens, so this is carried from the first turn rather than added later.
    usage: dict | None = None


class ModelUnreachable(RuntimeError):
    """No answer from the model endpoint. Distinct because an unattended run cannot ask."""


class ModelProtocolError(RuntimeError):
    """An answer that was not the exchange we asked for."""


class OllamaSession:
    """One tool-calling conversation with a local model, over the OpenAI-compatible endpoint.

    Ollama serves `/v1/chat/completions` alongside its native API, which is why this speaks the
    OpenAI shape: the same session works against any OpenAI-compatible endpoint, so a model
    served by vLLM or llama.cpp needs no second adapter.

    The session owns message history. The driver in `loop.py` never sees a message — it sees
    normalized turns — so the loop stays provider-neutral and testable without a network.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        system: str,
        task: str,
        api_key: str = "",
        timeout: float = 600.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        # A local 30B generating a long edit can take minutes on one turn. The default here is
        # generous on purpose: a timeout that fires mid-generation throws away a turn that cost
        # 22-45s and tells the model nothing about why.
        self.timeout = timeout
        self.messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        self._transport = transport
        self._http: httpx.Client | None = None
        self._synthetic_ids = itertools.count(1)

    # -- wire ---------------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        if self._http is None:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._http = httpx.Client(
                timeout=self.timeout, transport=self._transport, headers=headers
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    @staticmethod
    def _tool_defs(tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

    def run_turn(self, tools: list[ToolSpec]) -> ToolTurn:
        """Send the conversation advertising `tools`, append the reply, return it normalized.

        Buffered rather than streamed: the two provider families fragment streaming tool-call
        arguments differently, and there is no human watching an unattended run for whom a
        token-by-token answer would be worth that.
        """
        body: dict = {"model": self.model, "messages": self.messages}
        if tools:
            body["tools"] = self._tool_defs(tools)
        try:
            response = self._client().post(f"{self.base_url}/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise ModelUnreachable(f"{self.base_url}: {exc}") from exc
        if response.status_code != 200:
            raise ModelProtocolError(
                f"{self.base_url}: HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelProtocolError(f"{self.base_url}: reply was not JSON") from exc

        try:
            choice = payload["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelProtocolError(f"{self.base_url}: reply had no choices") from exc
        message = choice.get("message") or {}
        # Echoed verbatim so the assistant turn keeps its `tool_calls` exactly as the endpoint
        # wrote them — the tool results fed back next must correlate against those same ids.
        self.messages.append(message)
        return self._normalize(choice, message, payload.get("usage") or {})

    def _normalize(self, choice: dict, message: dict, usage: dict) -> ToolTurn:
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                # A model that emits malformed JSON has made a mistake it can correct. The call
                # is kept with empty input so `execute` refuses it and the model reads why;
                # dropping it would leave the turn looking like it asked for nothing.
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(
                ToolCall(
                    # Some endpoints omit the id entirely. A synthesised one still correlates,
                    # because the result we feed back carries whatever we put here.
                    id=raw.get("id") or f"call_{next(self._synthetic_ids)}",
                    name=fn.get("name") or "",
                    input=arguments,
                )
            )
        return ToolTurn(
            text=message.get("content") or "",
            tool_calls=calls,
            # NOT `finish_reason == "tool_calls"` alone. Models differ: some stop with
            # `finish_reason: stop` while still carrying tool_calls, and keying only on the
            # reason would silently drop the work they asked for. The calls themselves are the
            # more reliable signal, so either one counts.
            wants_tools=choice.get("finish_reason") == "tool_calls" or bool(calls),
            usage={
                "input": usage.get("prompt_tokens", 0),
                "output": usage.get("completion_tokens", 0),
            },
        )

    def add_results(self, results: list[ToolResult]) -> None:
        """Append tool results in the shape the endpoint expects for the next turn."""
        for result in results:
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.id,
                    # The OpenAI shape has no error flag on a tool message, so the marker is
                    # folded into the text. The model has to be able to tell a refusal from
                    # output, or it will treat "outside the worktree" as a file's contents.
                    "content": ("ERROR: " + result.content) if result.is_error else result.content,
                }
            )
