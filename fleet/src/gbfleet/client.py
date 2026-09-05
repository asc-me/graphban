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

**The allowlist is per-holder, not per-package.** `allowed` defaults to the supervisor's set;
`gbagent` (PRD-24 S3) is a worker in the same distribution with a different job, and passes
its own. Widening `ALLOWED_TOOLS` to cover both would have given the supervisor a worker's
reach for no reason but sharing a transport.
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

#: The REST paths this client may POST to (PRD-38 D3). `ALLOWED_TOOLS` governs `/api/mcp` and
#: says nothing about anything else, so a route that is not a tool would otherwise be a second
#: surface on a credential with authority, reached by whoever thought of it first. Pinned by
#: exact equality in `test_client.py` for the same reason the tool set is.
ATTEMPTS_PATH = "/api/fleet/attempts"
ALLOWED_PATHS: frozenset[str] = frozenset({ATTEMPTS_PATH})

#: Short on purpose. This is a measurement posted beside real work; a supervisor waiting on it
#: is a supervisor not starting a child.
ATTEMPT_TIMEOUT = 2.0


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
    #: Which tools THIS holder may call. Defaults to the supervisor's set; `gbagent` passes a
    #: worker's instead (PRD-24 S3). One checked call, two different keys — the alternative was
    #: widening `ALLOWED_TOOLS` to cover a worker, which would hand the supervisor authority
    #: PRD-22 §4 says it must not have, for no reason but sharing a transport.
    allowed: frozenset[str] = ALLOWED_TOOLS
    #: The project every call names unless the caller names one (GRPH-718/719). A credential
    #: spanning several projects resolves to its DEFAULT project when a call names none, and
    #: that is not the project the seat was minted on — the supervisor then polls a roster
    #: its child never appears on, and the child reads a backlog that is not its own.
    #: Empty means today's behaviour: the server picks.
    project_id: str = ""
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
        """The single outbound call site. Every Graphban call in this package goes through here.

        `test_client.py` asserts that structurally, because an allowlist with a second door
        is decoration. The package has exactly one other module that opens a socket —
        `gbagent/llm.py`, which talks to a model endpoint and holds no authority over
        anything — and the same test pins that list so a third door fails rather than passes
        review.
        """
        if tool not in self.allowed:
            raise NotPermitted(
                f"this credential may not call {tool!r}. Permitted: {sorted(self.allowed)}. "
                "Widening is a deliberate edit to whichever set this holder was given: "
                "`ALLOWED_TOOLS` for the supervisor — PRD-22 §4, it decides how many "
                "children of an already-authorised kind to run and nothing else — or "
                "`gbagent.coord.WORKER_TOOLS` for the agent. Say why in the same commit."
            )

        if self.project_id and "project_id" not in arguments:
            arguments = {**arguments, "project_id": self.project_id}
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments}, label=tool)

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

    def post_attempt(self, **payload: Any) -> dict | None:
        """Telemetry, on the ONE REST path this client may reach (PRD-38 D3).

        `ALLOWED_TOOLS` governs `/api/mcp`; a REST route is a second surface on the same
        credential, so it gets a pinned allowlist of its own rather than being ungoverned by
        virtue of not being a tool.

        **Never raises, and never blocks anything.** The supervisor calls this twice per
        child — once before it starts one, once after it exits — and a measurement that could
        fail a spawn would be a worse bargain than having no measurement. A post that does not
        land returns None, and the server then reads `sampled` as `unknown`, which is a value
        the page shows rather than a gap it hides.
        """
        return self._post(ATTEMPTS_PATH, payload, timeout=ATTEMPT_TIMEOUT)

    def _post(self, path: str, payload: dict, *, timeout: float) -> dict | None:
        if path not in ALLOWED_PATHS:
            raise NotPermitted(
                f"this client may not POST {path!r}. Permitted: {sorted(ALLOWED_PATHS)}. "
                "A REST surface on a credential with authority is widened deliberately or "
                "not at all."
            )
        url = self.base_url.rstrip("/") + path
        body = {k: v for k, v in payload.items() if v is not None}
        try:
            reply = self._client().post(url, json=body, timeout=timeout)
        except httpx.HTTPError:
            return None
        if reply.status_code >= 400:
            return None
        try:
            return reply.json()
        except ValueError:
            return None

    def list_tools(self) -> list[dict]:
        """The server's own tool manifest — name, description and input schema.

        **Listing is not calling**, which is why this does not consult `allowed`. The
        allowlist governs what this credential may DO; the manifest is the same document
        every MCP client fetches at connect, before it has a role at all (PRD-17 D-b: the
        manifest is not trimmed by role, the call gate refuses instead).

        `gbagent` fetches it so the orientation tools it advertises to a model carry the
        SERVER's schemas rather than a second copy that goes stale. A declared duplicate of
        eight tool schemas is a thing that drifts silently and is discovered as a model
        calling with arguments nobody accepts.
        """
        tools = self._rpc("tools/list", {}, label="tools/list").get("tools")
        return list(tools) if isinstance(tools, list) else []

    def _rpc(self, method: str, params: dict, *, label: str) -> dict:
        """The single outbound request. `call` checks the allowlist before reaching here."""
        request_id = next(self._ids)
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
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
            raise ProtocolError(f"{label}: {err.get('code')}: {err.get('message')}")

        result = payload.get("result")
        if not isinstance(result, dict):
            raise ProtocolError(f"{label}: reply had no result object")
        return result

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
