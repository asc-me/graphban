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
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx

from . import __version__
from . import observe

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
#: Names of skills and MCP servers, so the Harness page can show where harnesses differ.
#: The same rule as attempts: a REST route on this credential is a deliberate widening,
#: and the body is names only — the scanner drops URLs, headers and paths before posting.
SURFACE_PATH = "/api/harness/surface"
GRID_PATH = "/api/harness/grid"
#: The repo suite (GRPH-P98 §3): read with `get_suite`, written with `adopt_suite`.
SUITE_PATH = "/api/bench/suite"
BENCH_RESULTS_PATH = "/api/bench/results"
BENCH_SEAT_PATH = "/api/bench/seat"
SUITE_ADOPT_PATH = "/api/bench/suite/adopt"
#: A baseline run (GRPH-P98 §4): open it for a scratch project and seats, post its results.
BENCH_RUNS_PATH = "/api/bench/runs"
BENCH_RESULTS_PATH = "/api/bench/results"
#: Facts only the supervisor knows about a tree it cut (GRPH-975): the base, the draft PR,
#: a salvage, a withheld proposal. One route, so a later kind extends it rather than adding
#: a path. The body is those facts; the server composes the note. Not a tool — widening
#: `ALLOWED_TOOLS` for this is the thing PRD-22 §4 forbids.
OBSERVATIONS_PATH = "/api/fleet/observations"
#: A `--prd` drain's outcome (GRPH-P99): the server holds the last notice and answers
#: `changed` false for a post identical to it.
DRAIN_NOTICES_PATH = "/api/fleet/drain-notices"
# GRPH-1346: the start-up probe of that route. It must be a body the route VALIDATES, or
# FastAPI answers 422 before `record_observation` reaches its credential check and the probe
# reads every key as fine. A real kind and an item id no key can resolve: a key that cannot
# supervise is 403 at the credential gate, a key that can is 404 "no such item" at the item
# gate, and nothing is written either way. `gban.doctor.PROBE_BODY` is the same body.
OBSERVATION_PROBE_ITEM = "__gbfleet_probe_no_such_item__"
OBSERVATION_PROBE = {"item_id": OBSERVATION_PROBE_ITEM, "kind": "cut_from"}
ALLOWED_PATHS: frozenset[str] = frozenset(
    {ATTEMPTS_PATH, SURFACE_PATH, OBSERVATIONS_PATH, DRAIN_NOTICES_PATH, SUITE_ADOPT_PATH,
     BENCH_RUNS_PATH, BENCH_RESULTS_PATH, BENCH_SEAT_PATH})

#: Short on purpose. This is a measurement posted beside real work; a supervisor waiting on it
#: is a supervisor not starting a child.
ATTEMPT_TIMEOUT = 2.0


class NotPermitted(RuntimeError):
    """The supervisor tried to make a call PRD-22 §4 says it may not make."""


class ObservationRefused(RuntimeError):
    """The observations route refused, or never answered (GRPH-975).

    Telemetry swallows a failure and returns None, because a measurement that could fail a
    spawn is worse than no measurement. A base or a PR receipt is the opposite: None would
    read as landed, `sign_off` would report the commit uncompared, and the wave log — the
    only place the refusal used to be visible — would say nothing. The caller records the
    reason on the wave and carries on. It does not raise out of the wave.

    `status` is the HTTP status when the server answered, None when it never did — a 403 is
    "this credential cannot supervise" and a dropped connection is not.
    """

    def __init__(self, message: str = "", status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        #: The reply body when the server answered (a 409 carries `current_version`).
        self.body = body


class ServerUnreachable(RuntimeError):
    """No answer from Graphban.

    Distinct from every other failure on purpose: D-i turns on it. Unreachable means
    stop spawning and let each child run to its own lease deadline. A refused call or a
    tool error means the server is fine and answered.
    """

    def __init__(self, message: str = "", tool: str = "") -> None:
        super().__init__(message)
        self.tool = tool


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
    #: TOTAL seconds one request may take, start to last byte (GRPH-1179). `timeout` is per
    #: read, so a server that trickles bytes can hold a call open indefinitely.
    deadline: float = 90.0
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
        # GRPH-1235: `_post_within`'s watchdog closes the client at the deadline, and it can
        # fire after the reply was read and returned. A closed client raises RuntimeError, not
        # an httpx error, so every caller would leak it — replace it here, once, for all.
        if self._http is None or self._http.is_closed:
            # GRPH-1244: keepalive so a vanished peer is noticed even between calls. Only for
            # the real transport; a caller-supplied one (tests) is used as given.
            transport = self.transport or httpx.HTTPTransport(socket_options=_keepalive_options())
            self._http = httpx.Client(
                timeout=self.timeout,
                transport=transport,
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

    def post_surface(self, **payload: Any) -> dict | None:
        """The skill and MCP names a child on this machine would load.

        Same bargain as `post_attempt`: a report that cannot land returns None and never
        raises. `gbfleet doctor` prints UNKNOWN when that happens; it does not refuse to
        start a wave because the Harness page is a report behind.
        """
        return self._post(SURFACE_PATH, payload, timeout=10.0)

    def get_grid(self, project_id: str, *, timeout: float = 20.0) -> dict:
        """The Harness grid's cells, read with the agent key (`gbfleet harness grid`).

        A read of one fixed path; raises ObservationRefused on any failure so the CLI can
        name it. Not a door for other reads: the path is not a parameter.
        """
        url = self.base_url.rstrip("/") + GRID_PATH
        try:
            reply = self._client().get(url, params={"project_id": project_id}, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ObservationRefused(f"{GRID_PATH}: {exc}") from exc
        if reply.status_code >= 400:
            raise ObservationRefused(f"{GRID_PATH}: HTTP {reply.status_code}: {reply.text[:300]}",
                                     status=reply.status_code)
        try:
            parsed = reply.json()
        except ValueError:
            raise ObservationRefused(f"{GRID_PATH}: reply was not JSON")
        if not isinstance(parsed, dict):
            raise ObservationRefused(f"{GRID_PATH}: reply was not an object")
        return parsed

    def get_suite(self, project_id: str, *, timeout: float = 20.0) -> dict:
        """The project's repo suite: `{version, tasks}`. A read of one fixed path."""
        url = self.base_url.rstrip("/") + SUITE_PATH
        try:
            reply = self._client().get(url, params={"project_id": project_id}, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ObservationRefused(f"{SUITE_PATH}: {exc}") from exc
        if reply.status_code >= 400:
            raise ObservationRefused(f"{SUITE_PATH}: HTTP {reply.status_code}: {reply.text[:300]}",
                                     status=reply.status_code, body=reply.text)
        try:
            parsed = reply.json()
        except ValueError:
            raise ObservationRefused(f"{SUITE_PATH}: reply was not JSON")
        if not isinstance(parsed, dict):
            raise ObservationRefused(f"{SUITE_PATH}: reply was not an object")
        return parsed

    def list_bench_results(self, project_id: str, *, timeout: float = 20.0) -> list[dict]:
        """The project's baseline records, newest first."""
        url = self.base_url.rstrip("/") + BENCH_RESULTS_PATH
        try:
            reply = self._client().get(url, params={"project_id": project_id}, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ObservationRefused(f"{BENCH_RESULTS_PATH}: {exc}") from exc
        if reply.status_code >= 400:
            raise ObservationRefused(
                f"{BENCH_RESULTS_PATH}: HTTP {reply.status_code}: {reply.text[:300]}",
                status=reply.status_code, body=reply.text)
        try:
            return list(reply.json().get("results") or [])
        except (ValueError, AttributeError) as exc:
            raise ObservationRefused(f"{BENCH_RESULTS_PATH}: not JSON") from exc

    def adopt_suite(self, *, expected_version: int, tasks: list[dict],
                    timeout: float = 30.0) -> dict:
        """Adopt tasks into the repo suite. Raises ObservationRefused; a 409 has status 409
        and the server's body (with `current_version`)."""
        got = self._post(SUITE_ADOPT_PATH, {"expected_version": expected_version, "tasks": tasks},
                         timeout=timeout, strict=True)
        if not isinstance(got, dict):
            raise ObservationRefused(f"{SUITE_ADOPT_PATH}: empty reply")
        return got

    def open_bench_run(self, body: dict, *, timeout: float = 60.0) -> dict:
        """Open a baseline run: `{id, project_id, seats[]}`. Raises ObservationRefused."""
        got = self._post(BENCH_RUNS_PATH, body, timeout=timeout, strict=True)
        if not isinstance(got, dict):
            raise ObservationRefused(f"{BENCH_RUNS_PATH}: empty reply")
        return got

    def bench_task_seat(self, run_id: str, task_id: str, *, timeout: float = 30.0) -> dict:
        """A fresh seat for one task of an open run (GRPH-1888). Raises ObservationRefused."""
        got = self._post(BENCH_SEAT_PATH, {"run_id": run_id, "task_id": task_id},
                         timeout=timeout, strict=True)
        if not isinstance(got, dict) or not got.get("enrolment_code"):
            raise ObservationRefused(f"{BENCH_SEAT_PATH}: no seat in the reply")
        return got

    def post_bench_result(self, body: dict, *, timeout: float = 30.0) -> dict:
        """Store one baseline record. Raises ObservationRefused."""
        got = self._post(BENCH_RESULTS_PATH, body, timeout=timeout, strict=True)
        if not isinstance(got, dict):
            raise ObservationRefused(f"{BENCH_RESULTS_PATH}: empty reply")
        return got

    def post_observation(self, *, timeout: float = 10.0, **payload: Any) -> dict:
        """One fact about a tree this supervisor cut (GRPH-975).

        Raises on a refusal. `post_attempt` returns None because a lost measurement must
        not stop a child; a lost base is the defect that made `sign_off` waive every item
        by hand. The wave catches this and logs the reason. It does not fall back to a
        tool call — that tool is not in `ALLOWED_TOOLS`, and a fallback would hide the
        refusal the route exists to make visible.

        Goes through `_post` (strict). A second method that called `.post` itself would
        be a third door, which `test_client.py` pins shut.
        """
        got = self._post(OBSERVATIONS_PATH, payload, timeout=timeout, strict=True)
        if not isinstance(got, dict):
            raise ObservationRefused(f"{OBSERVATIONS_PATH}: empty reply")
        return got

    def post_drain_notice(self, *, timeout: float = 10.0, **payload: Any) -> dict:
        """One drain outcome (GRPH-1912). Raises ObservationRefused when it did not land."""
        got = self._post(DRAIN_NOTICES_PATH, payload, timeout=timeout, strict=True)
        if not isinstance(got, dict):
            raise ObservationRefused(f"{DRAIN_NOTICES_PATH}: empty reply")
        return got

    def _post(self, path: str, payload: dict, *, timeout: float,
              strict: bool = False) -> dict | None:
        if path not in ALLOWED_PATHS:
            raise NotPermitted(
                f"this client may not POST {path!r}. Permitted: {sorted(ALLOWED_PATHS)}. "
                "A REST surface on a credential with authority is widened deliberately or "
                "not at all."
            )
        url = self.base_url.rstrip("/") + path
        body = {k: v for k, v in payload.items() if v is not None}
        try:
            # The same bound as MCP. `.post` on this thread is a recv the caller
            # cannot leave; a server that accepts and never answers held the drain
            # there with no log line (GRPH-1786).
            reply = _post_within(self._client(), url, body, timeout)
        except _Abandoned as exc:
            self._http = None
            if strict:
                raise ObservationRefused(
                    f"{path}: no complete reply within {timeout:g}s"
                ) from exc
            return None
        except httpx.HTTPError as exc:
            if strict:
                raise ObservationRefused(f"{path}: {exc}") from exc
            return None
        if reply.status_code >= 400:
            if strict:
                raise ObservationRefused(
                    f"{path}: HTTP {reply.status_code}: {reply.text[:300]}",
                    status=reply.status_code, body=reply.text,
                )
            return None
        try:
            parsed = reply.json()
        except ValueError:
            if strict:
                raise ObservationRefused(f"{path}: reply was not JSON")
            return None
        if not isinstance(parsed, dict):
            if strict:
                raise ObservationRefused(f"{path}: reply was not an object")
            return None
        return parsed

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
            response = _post_within(self._client(), self.endpoint, body, self.deadline)
        except httpx.HTTPError as exc:
            if isinstance(exc, _Abandoned):
                # The abandoned client is being closed on a background thread. Drop it here, in
                # the caller's thread, so the next call cannot pick it up before that close
                # lands and get httpx's RuntimeError mid-request (the GRPH-1235 hazard).
                self._http = None
            raise ServerUnreachable(f"{self.endpoint}: {exc!r}", tool=label) from exc

        if response.status_code >= 500:
            # A 5xx is the server failing to answer, which for D-i's purposes is the
            # same situation as not reaching it: retrying is the right move and
            # spawning is not. A 4xx is an answer — a bad credential does not become
            # true by waiting — so it stays a ProtocolError below.
            raise ServerUnreachable(f"{self.endpoint}: HTTP {response.status_code}", tool=label)
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

    # GRPH-1977, the scripting surface. Two reads the CLI wraps, held by the CLI's own
    # `SCRIPTING_READS` set and not by `ALLOWED_TOOLS`.
    def prd_coverage(self, prd_id: str) -> dict:
        return self.call("prd_coverage", prd_id=prd_id)

    def decompose_preview(self, prd_id: str) -> dict:
        # `create` is spelled here and nowhere a caller can reach: there is no parameter
        # for it, so no scripting form of `decompose` files anything.
        return self.call("decompose_prd", prd_id=prd_id, create=False)


def _keepalive_options() -> list[tuple[int, int, int]]:
    """SO_KEEPALIVE plus a short idle/interval/count where the platform names them."""
    import socket

    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    for name, value in ((idle, 30), (getattr(socket, "TCP_KEEPINTVL", None), 10),
                        (getattr(socket, "TCP_KEEPCNT", None), 3)):
        if name is not None:
            options.append((socket.IPPROTO_TCP, name, value))
    return options


class _Abandoned(httpx.ReadTimeout):
    """The deadline passed with the exchange still running: `client` is being closed behind it."""


def _wait_bounded(done: threading.Event, deadline: float) -> bool:
    """Whether `done` was set before `deadline` seconds.

    `Event.wait` takes the condition lock. On 2026-11-20 the drain's main thread
    sat in that acquire for 50 minutes while its worker stayed in `poll` (GRPH-1786).
    `is_set` reads the flag without that lock, and `time.sleep` returns whether or
    not the condition does, so this wait ends when the clock says it ends.
    """
    if deadline < 0:
        deadline = 0.0
    end = time.monotonic() + deadline
    while True:
        if done.is_set():
            return True
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _shutdown_pooled_sockets(client: httpx.Client) -> None:
    """`SHUT_RDWR` on every pooled socket so a blocked `poll` returns.

    `close()` from another thread does not interrupt `recv` on macOS (GRPH-1244).
    Shutdown does, once the connection exists. Best effort: a connect that has not
    reached a socket yet is left to the caller's deadline, which does not wait on it.
    """
    import socket

    transport = getattr(client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    if pool is None:
        return
    found: list[Any] = []
    for attr in ("_connections", "_requests"):
        bag = getattr(pool, attr, None)
        if not bag:
            continue
        try:
            items = list(bag)
        except TypeError:
            continue
        for item in items:
            found.append(item)
            inner = getattr(item, "_connection", None)
            if inner is not None:
                found.append(inner)
    for obj in found:
        stream = getattr(obj, "_network_stream", None)
        sock = None
        if stream is not None and hasattr(stream, "get_extra_info"):
            try:
                sock = stream.get_extra_info("socket")
            except Exception:  # noqa: BLE001 - a socket we cannot see is skipped
                sock = None
        if sock is None and stream is not None:
            sock = getattr(stream, "_sock", None)
        if sock is None:
            continue
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _post_within(client: httpx.Client, url: str, body: dict, deadline: float) -> httpx.Response:
    """POST `body`, giving up after `deadline` seconds in TOTAL (GRPH-1179).

    httpx's timeouts bound each connect/read/write, not the exchange: a response that trickles
    a byte every few seconds never trips them. Streaming lets the clock be checked between
    chunks. Raises `httpx.ReadTimeout`, so callers treat it as any other transport failure.
    """
    start = time.monotonic()
    # A call that is still inside its own deadline is the loop working, not a silence.
    observe.touch_loop()
    # GRPH-1235: waiting for the HEADERS happens inside `stream().__enter__`, where the chunk
    # check below cannot reach. Cap every httpx phase at the deadline so that wait is bounded
    # too (the client's own 30s per-read timeout is the looser bound it replaces). No phase is
    # left at None: httpcore treats a missing read timeout as a blocking recv, which is the
    # `poll` the 2026-11-20 sample was stuck in.
    limit = min(deadline, getattr(client.timeout, "read", None) or deadline)
    # GRPH-1244: a peer that vanished without FIN/RST (half-open, e.g. over Tailscale) leaves the
    # request blocked in recv(), and `client.close()` from another thread does not interrupt that
    # on macOS. The exchange runs on a worker. The caller sleeps on the monotonic clock and
    # returns at the deadline even if that worker never sets `done` — `done.set` lives in
    # `finally`, which does not run while `poll` is still inside the `with`. Shutdown is attempted
    # so the worker can leave; the caller does not join it.
    timeout = httpx.Timeout(connect=limit, read=limit, write=limit, pool=limit)
    outcome: dict[str, Any] = {}
    done = threading.Event()

    def _exchange() -> None:
        try:
            with client.stream("POST", url, json=body, timeout=timeout) as streamed:
                chunks: list[bytes] = []
                for chunk in streamed.iter_bytes():
                    chunks.append(chunk)
                    if time.monotonic() - start > deadline:
                        raise httpx.ReadTimeout(f"no complete reply within {deadline:g}s")
                headers = [(k, v) for k, v in streamed.headers.multi_items()
                           if k.lower() not in ("content-encoding", "content-length",
                                                "transfer-encoding")]
                outcome["response"] = httpx.Response(
                    streamed.status_code, headers=headers, content=b"".join(chunks),
                    request=streamed.request)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_exchange, name="gbfleet-post", daemon=True).start()
    if not _wait_bounded(done, deadline):
        _shutdown_pooled_sockets(client)
        # Closing can itself block on the stuck connection; never let it hold the caller.
        threading.Thread(target=_close_quietly, args=(client,), daemon=True).start()
        observe.emit("call_timeout", url=url, deadline=deadline,
                     waited=round(time.monotonic() - start, 1))
        raise _Abandoned(f"no complete reply within {deadline:g}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["response"]


def _close_quietly(client: httpx.Client) -> None:
    try:
        client.close()
    except Exception:  # noqa: BLE001 - best effort on a connection already given up on
        logging.getLogger(__name__).debug("closing an abandoned client failed", exc_info=True)


def _text_of(result: dict) -> str:
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return "the server reported a tool error with no message"


# --- a build server the seat grants --------------------------------------------------


#: The protocol version named in the handshake. The same string `gbfleet.mcp` answers with,
#: pinned equal by a test rather than imported from there: `mcp.py` imports THIS module, so
#: sharing the constant the other way round would be a cycle.
SHARED_PROTOCOL_VERSION = "2025-06-18"

#: One request to a granted server. `Graphban`'s own default, and short enough that a
#: browser tool that hangs costs a turn rather than the run.
SHARED_TIMEOUT = 30.0


@dataclass
class McpServer:
    """One MCP server the seat grants, spoken to as streamable HTTP (GRPH-997).

    **Transport only.** What gets advertised to a model, and turning every failure into a
    result it can read, is `gbagent.buildtools`; this class raises and knows nothing about
    models.

    **No allowlist here, and that is not an oversight.** `Graphban.allowed` exists because
    the supervisor holds a credential with authority over the ledger and PRD-22 §4 limits
    what it may do with it. A granted build server carries no ledger authority at all: it is
    a docs server or a browser, reached with the OPERATOR's own credential for it, and the
    grant is the exact name somebody typed at `--mcp-server` (`mcpshare.select` refuses a
    pattern and refuses the ledger's own names). The boundary is the seat file, and it is
    enforced where the file is written rather than again where it is read.

    **Not `Graphban` with a different URL, either.** That class appends `/api/mcp`, injects
    `project_id` into every call, and answers `structuredContent` only. A third-party server
    has its own endpoint, has never heard of a project, and answers `content` blocks — so
    reusing it would have meant un-pinning the tests that hold its shape.
    """

    name: str
    url: str
    #: The stanza's own headers, sent as given. These are the operator's credential for that
    #: server; nothing here logs them, and the reason a server failed is reported without them.
    headers: dict = field(default_factory=dict)
    timeout: float = SHARED_TIMEOUT
    #: Injected by tests via `httpx.MockTransport`, exactly as on `Graphban`: one code path.
    transport: httpx.BaseTransport | None = None
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1), repr=False)
    _http: httpx.Client | None = field(default=None, repr=False)
    #: What the server issued at `initialize`, echoed on every later request.
    _session: str = field(default="", repr=False)

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=self.timeout,
                transport=self.transport,
                # Both shapes are named because a streamable-HTTP server refuses (406) a
                # request that does not say it can take either — and which one it ANSWERS in
                # is its choice, not ours.
                headers={"Accept": "application/json, text/event-stream", **(self.headers or {})},
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def initialize(self) -> None:
        """The handshake, best-effort.

        A server that keeps session state issues `Mcp-Session-Id` here and expects it back on
        every later request; Graphban's own does (`backend/app/mcp_server.py` sets it on the
        `initialize` reply and reads it on `tools/call`).

        Best-effort rather than fatal on purpose. A server that wants no handshake answers
        this with a JSON-RPC error, and the request that actually matters is the next one —
        so refusing to continue here would turn "this server is stateless" into "this grant
        is broken", which is the absence reading as a failure instead of as what it is.
        """
        try:
            self._rpc("initialize", {
                "protocolVersion": SHARED_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "gbagent", "version": __version__},
            }, label="initialize")
            self._notify("notifications/initialized")
        except (ServerUnreachable, ProtocolError):
            pass

    def list_tools(self) -> list[dict]:
        """The server's own manifest — name, description, inputSchema.

        Fetched rather than declared, for the reason `orient.build` gives: a copy of somebody
        else's tool contract goes stale quietly and is discovered as a model calling with
        arguments nobody accepts, at thirty seconds a turn.
        """
        tools = self._rpc("tools/list", {}, label="tools/list").get("tools")
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    def call_tool(self, tool: str, arguments: dict) -> dict:
        """The raw MCP result: `content` blocks, and `isError` when the server refused.

        **`isError` is returned, not raised.** A refusal here is an answer from a server with
        no authority over anything, and the caller hands it to a model that can read it and do
        something else. `Graphban.call` raises `ToolFailed` on the same flag because there a
        refusal means the ledger declined a change of state, which the caller must not treat
        as data.
        """
        return self._rpc("tools/call", {"name": tool, "arguments": arguments}, label=tool)

    def _send(self, body: dict, *, label: str) -> httpx.Response:
        """The single outbound request. There is no second one, for the reason `Graphban`
        gives: a door with two hinges is not a door."""
        headers = {"Mcp-Session-Id": self._session} if self._session else {}
        try:
            response = self._client().post(self.url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ServerUnreachable(f"{self.name}: {self.url}: {exc}") from exc
        if response.status_code >= 500:
            # Same split as `Graphban._rpc`: a 5xx is a server failing to answer, a 4xx is an
            # answer.
            raise ServerUnreachable(f"{self.name}: {self.url}: HTTP {response.status_code}")
        session = response.headers.get("mcp-session-id")
        if session:
            self._session = session
        return response

    def _rpc(self, method: str, params: dict, *, label: str) -> dict:
        request_id = next(self._ids)
        response = self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            label=label,
        )
        if response.status_code not in (200, 202):
            raise ProtocolError(
                f"{self.name}: {label}: HTTP {response.status_code}: {response.text[:200]}"
            )
        payload = _json_reply(response)
        if payload is None:
            raise ProtocolError(f"{self.name}: {label}: reply was not JSON")
        if payload.get("id") != request_id:
            raise ProtocolError(
                f"{self.name}: reply id {payload.get('id')!r} does not match request "
                f"{request_id!r}"
            )
        if "error" in payload:
            err = payload["error"] or {}
            raise ProtocolError(f"{self.name}: {label}: {err.get('code')}: {err.get('message')}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ProtocolError(f"{self.name}: {label}: reply had no result object")
        return result

    def _notify(self, method: str) -> None:
        """A JSON-RPC notification: no id, no reply body, and a 2xx is the whole contract."""
        response = self._send({"jsonrpc": "2.0", "method": method}, label=method)
        if response.status_code >= 400:
            raise ProtocolError(f"{self.name}: {method}: HTTP {response.status_code}")


def _json_reply(response: httpx.Response) -> dict | None:
    """The JSON-RPC reply out of a response that may be in either of the two shapes.

    Graphban's server answers `application/json` and says so in its own docstring ("Single
    JSON responses (no SSE)"), but a granted server is somebody else's, and streamable HTTP
    lets it answer `text/event-stream` — where the reply is the last `data:` frame. Reading
    only `response.json()` would report a working server as unreadable, and the model would
    be told the grant was broken.
    """
    text = response.text or ""
    if "text/event-stream" in (response.headers.get("content-type") or ""):
        frames = [line[len("data:"):].strip() for line in text.splitlines()
                  if line.startswith("data:")]
        text = frames[-1] if frames else ""
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None



# ---- the person's `gban` session (GRPH-1974) ---------------------------------------------------
#
# `gbfleet drain create` mints a drain's key AS THE PERSON, the way `gban setup` does, because a
# PRD-limited key (GRPH-1973) is minted by a human with write on the PRD's project. The
# supervisor's own credential (`Graphban` above) is an agent key and must not be able to mint
# one, so this is a second, separate holder with two fixed paths of its own and nothing else.
#
# `gban` is a separate distribution (cli/) and fleet cannot import it (PRD-22 D-e), so the two
# facts it keeps are read here directly: `$GRAPHBAN_HOME/session.json` (`refresh_token`) and
# `gban.json` (`url`). Read, never written — `gban login` owns both files.

#: Exchanges the stored refresh token for an access token. The presented token stays valid
#: (the server keys on `token_version`), so nothing is written back.
SESSION_REFRESH_PATH = "/api/auth/refresh"
#: Mints a key. Called once per `drain create`.
SESSION_API_KEYS_PATH = "/api/api-keys"
SESSION_PATHS: frozenset[str] = frozenset({SESSION_REFRESH_PATH, SESSION_API_KEYS_PATH})
#: The one READ the session makes: a PRD's coverage (status and `shaped`), which
#: `drain create` checks before it mints (GRPH-1975). A GET, so it cannot write.
SESSION_COVERAGE_PATH = "/api/prds/{prd}/coverage"
#: `drain remove` / `drain rekey` (GRPH-1976): the key list (ids, prefixes, `last_used`, the
#: first-call signal) is a GET on `SESSION_API_KEYS_PATH`; a revoke is a DELETE on one key.
#: No other DELETE is possible through the session.
SESSION_REVOKE_PATH = "/api/api-keys/{id}"

GBAN_HOME_ENV = "GRAPHBAN_HOME"
#: The tiers `gban setup` mints with. The server adds `fleet` to a PRD-limited key anyway.
DRAIN_KEY_TIERS = ["fleet", "prd"]


class NoSession(RuntimeError):
    """No stored `gban` session, or one the server will not renew. Nothing was minted."""


def gban_home() -> Path:
    return Path(os.environ.get(GBAN_HOME_ENV) or (Path.home() / ".graphban"))


def _gban_file(name: str) -> dict:
    try:
        loaded = json.loads((gban_home() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def gban_url() -> str:
    """The server `gban login` recorded, or "". A default, never an override of `--server`."""
    return str(_gban_file("gban.json").get("url") or "").strip()


@dataclass
class LedgerSession:
    """The person's session on one server: an access token held in memory, nothing on disk."""

    base_url: str
    #: Empty until `open` has exchanged the refresh token. `default_factory`, not a literal:
    #: bandit reads `token=""` as a hard-coded password (B105/B106).
    token: str = field(default_factory=str, repr=False)
    transport: httpx.BaseTransport | None = None
    timeout: float = 30.0

    @classmethod
    def open(cls, base_url: str, *,
             transport: httpx.BaseTransport | None = None) -> "LedgerSession":
        """One refresh exchange. Raises `NoSession` before any network when nothing is stored."""
        stored = str(_gban_file("session.json").get("refresh_token") or "")
        if not stored:
            raise NoSession("no stored gban session")
        session = cls(base_url=base_url.rstrip("/"), transport=transport)
        try:
            pair = session._post(SESSION_REFRESH_PATH, {"refresh_token": stored}, auth=False)
        except ToolFailed as exc:
            if exc.code in ("401", "403"):
                raise NoSession(f"the server would not renew the gban session ({exc})") \
                    from exc
            raise
        token = str(pair.get("access_token") or "")
        if not token:
            raise NoSession("the server returned no access token")
        session.token = token
        return session

    def mint_prd_key(self, prd: str, label: str, *, project: str = "") -> dict:
        """A key limited to `prd` (GRPH-1973). Returns the server's reply, `plaintext` included.

        The caller must write `plaintext` to an owner-only file and print only `id`.
        """
        body: dict[str, Any] = {"name": label, "prd_id": prd, "expires_in_days": None,
                                "tool_tiers": list(DRAIN_KEY_TIERS), "origin": "fleet"}
        if project:
            body["project_id"] = project
        return self._post(SESSION_API_KEYS_PATH, body)

    def prd_coverage(self, prd: str) -> dict:
        """The PRD's `prd_coverage` reply: `status`, `shaped` and the rest. Writes nothing."""
        payload = self._request("GET", SESSION_COVERAGE_PATH.format(prd=quote(prd, safe="")))
        return payload if isinstance(payload, dict) else {}

    def list_keys(self) -> list[dict]:
        """The person's keys as the registry shows them: id, prefix, `last_used`, `revoked`.
        Never a plaintext — the server stores only a hash."""
        payload = self._request("GET", SESSION_API_KEYS_PATH)
        return [row for row in payload if isinstance(row, dict)] \
            if isinstance(payload, list) else []

    def revoke_key(self, key_id: str) -> None:
        """`DELETE /api/api-keys/{id}`: soft and idempotent on the server (GRPH-788)."""
        self._request("DELETE", SESSION_REVOKE_PATH.format(id=quote(key_id, safe="")))

    def _request(self, method: str, path: str) -> Any:
        """A GET or a DELETE with the access token. The JSON reply, or None for an empty one."""
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as http:
                response = http.request(method, self.base_url + path,
                                        headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as exc:
            raise ServerUnreachable(f"could not reach {self.base_url}: {exc}") from exc
        if not response.content:
            payload: Any = None
        else:
            try:
                payload = response.json()
            except ValueError:
                payload = {"detail": response.text[:400]}
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise ToolFailed(path, code=str(response.status_code),
                             message=str(detail or f"HTTP {response.status_code}"))
        return payload

    def _post(self, path: str, body: dict, *, auth: bool = True) -> dict:
        if path not in SESSION_PATHS:
            raise NotPermitted(f"the gban session may not POST {path!r}")
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as http:
                response = http.post(self.base_url + path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ServerUnreachable(f"could not reach {self.base_url}: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text[:400]}
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail)
            raise ToolFailed(path, code=str(response.status_code),
                             message=str(detail or f"HTTP {response.status_code}"))
        return payload if isinstance(payload, dict) else {}
