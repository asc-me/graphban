"""The one place the supervisor talks to Graphban, and the only calls it may make.

PRD-22 §4. The authority table says the supervisor may not mint a seat, decide or
assign a role, claim work, sign off, bounce, decide independence, or judge whether a
diff is worth resuming. Prose is exactly what stops binding when somebody adds a
convenient call, so the permitted tool names are a frozenset and every outbound call
goes through one function that checks it.

**This is not a security boundary and must not be described as one.** The server
enforces authority: a credential's `eligible_roles` still binds, a planner still cannot
build, and a supervisor that bypassed this module entirely would gain nothing it was
not already given. What the allowlist buys is that widening the supervisor's reach
becomes a deliberate edit to a named set with a test in front of it, rather than a call
site somebody adds while doing something else.

The chokepoint pays for itself anyway — auth, timeouts, offline detection and the D-a
error shape all want one place to live.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

#: Every Graphban tool the supervisor is permitted to call. Pinned exactly by
#: `test_client.py`, so an addition fails a test rather than passing review.
#:
#: Both entries are reads. The supervisor decides HOW MANY children of an
#: already-authorised kind to run; `propose_allocation` decides the MIX (D-j) and the
#: planner mints against it. `fleet_status` is the backstop poll that notices a seat
#: revoked while a child is still building (D-d).
#:
#: `list_enrolments` belongs here once GRPH-451 ships it — planner-scoped seat state is
#: what makes the backstop prompt rather than inferential.
ALLOWED_TOOLS: frozenset[str] = frozenset({"fleet_status", "propose_allocation"})


class NotPermitted(RuntimeError):
    """The supervisor tried to make a call PRD-22 §4 says it may not make."""


class ServerUnreachable(RuntimeError):
    """No answer from Graphban.

    Distinct from every other failure on purpose: D-i turns on it. Unreachable means
    stop spawning and let each child run to its own lease deadline. A refused call or a
    tool error means the server is fine and answered.
    """


class ProtocolError(RuntimeError):
    """A reply that was not the JSON-RPC exchange we asked for."""


class ToolFailed(RuntimeError):
    """The server ran the tool and reported a failure (`isError`)."""

    def __init__(self, tool: str, code: str, message: str, hint: str = "") -> None:
        self.tool = tool
        self.code = code
        self.hint = hint
        super().__init__(f"{tool}: {code}: {message}" + (f" ({hint})" if hint else ""))


@dataclass
class Graphban:
    """A Graphban server, reachable and holding a credential somebody handed us.

    Holds whatever credential the human gave it, and nothing more (PRD-22 §4). It
    cannot widen that: the ceiling is server-side.
    """

    base_url: str
    api_key: str
    timeout: float = 30.0
    #: Injected by tests via `httpx.MockTransport`. Production leaves it None and gets
    #: httpx's default — there is no second code path, which is the point.
    transport: httpx.BaseTransport | None = None
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1), repr=False)
    _http: httpx.Client | None = field(default=None, repr=False)

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/api/mcp"

    def _client(self) -> httpx.Client:
        # One connection pool for the life of the supervisor. It polls; opening a new
        # pool per poll is the kind of waste that only shows up as latency.
        if self._http is None:
            self._http = httpx.Client(
                timeout=self.timeout,
                transport=self.transport,
                headers={"X-API-Key": self.api_key},
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def call(self, tool: str, /, **arguments: Any) -> dict:
        """The single outbound call site. Everything else in this package goes through here.

        `test_client.py` asserts that structurally — one function in the whole package
        makes an HTTP request — because an allowlist with a second door is decoration.
        """
        if tool not in ALLOWED_TOOLS:
            raise NotPermitted(
                f"gbfleet may not call {tool!r}. Permitted: {sorted(ALLOWED_TOOLS)}. "
                "PRD-22 §4: the supervisor decides how many children of an "
                "already-authorised kind to run, and nothing else. If this call is "
                "genuinely the supervisor's to make, add it to ALLOWED_TOOLS and say "
                "why in the same commit."
            )

        request_id = next(self._ids)
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        try:
            response = self._client().post(self.endpoint, json=body)
        except httpx.HTTPError as exc:
            raise ServerUnreachable(f"{self.endpoint}: {exc}") from exc

        if response.status_code >= 500:
            # A 5xx is the server failing to answer, which for D-i's purposes is the
            # same situation as not reaching it: retrying is the right move and
            # spawning is not. A 4xx is an answer — a bad credential does not become
            # true by waiting — so it stays a ProtocolError below.
            raise ServerUnreachable(f"{self.endpoint}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise ProtocolError(
                f"{self.endpoint}: HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProtocolError(f"{self.endpoint}: reply was not JSON") from exc

        if payload.get("id") != request_id:
            raise ProtocolError(
                f"reply id {payload.get('id')!r} does not match request {request_id!r}"
            )
        if "error" in payload:
            err = payload["error"]
            raise ProtocolError(f"{tool}: {err.get('code')}: {err.get('message')}")

        result = payload.get("result")
        if not isinstance(result, dict):
            raise ProtocolError(f"{tool}: reply had no result object")

        if result.get("isError"):
            err = (result.get("structuredContent") or {}).get("error") or {}
            raise ToolFailed(
                tool,
                code=str(err.get("code") or "error"),
                message=str(err.get("message") or _text_of(result)),
                hint=str(err.get("hint") or ""),
            )

        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        raise ProtocolError(f"{tool}: reply carried no structuredContent")

    # Named conveniences. Each is one line through `call` — they exist so call sites
    # read well, and must never grow a request of their own.
    def fleet_status(self, **arguments: Any) -> dict:
        return self.call("fleet_status", **arguments)

    def propose_allocation(self, **arguments: Any) -> dict:
        return self.call("propose_allocation", **arguments)


def _text_of(result: dict) -> str:
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return "the server reported a tool error with no message"
