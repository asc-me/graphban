"""The local stdio surface. PRD-22 S3 and D-a.

**Two servers, and only one of them has authority.** Arbitration is remote and
authoritative; process control is local and unprivileged. The planner attaches both:

    planner
     ├─ graphban   (remote HTTP)  → mint_enrolment, propose_allocation, assign_role, fleet_status
     └─ gbfleet    (local stdio)  → spawn, stop, ps, orphans

`gbfleet` runs on the developer's machine and the Graphban server never learns its calls
happened. **There are no HTTP routes on this surface and no HTTP status codes anywhere
in it** — the grill misread that three times, asking for the endpoint URLs by which the
supervisor would invoke `spawn` on the Graphban server. There are none.

**Authentication is process ownership.** The planner speaks over a pipe to a child it
launched. There is no credential on this surface because there is nothing for one to
protect: `spawn` takes a seat it cannot mint and a cluster it cannot assign. It is a
launcher, and a launcher that lies gets a process the server refuses.

**Errors are tool results carrying `isError`**, with the message in `content` — the same
shape the Graphban server uses. A JSON-RPC error is reserved for the protocol itself
(unparseable input, an unknown method), because the planner must be able to tell *your
adapter is broken* from *the supervisor is gone*, and collapsing those two into one
transport failure is exactly what takes that away.

**`spawn` starts ONE child and takes no count.** The planner decides how many to run:
it holds both servers, so it can read `collision_clusters` and `get_backlog` itself,
mint that many seats, and call this once per child. The supervisor executes. That is
D-j's shape — the server computes, the planner commits, the supervisor executes — and it
is why nothing here needs to know how much work is outstanding.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from . import worktree as wt_mod
from .adapters import AdapterError
from .client import Graphban
from .lock import Acquired
from .seat import Seat
from .spawn import Child, LaunchFailed, Reason, stop
from .supervisor import Limits, LaunchFactory, Partition, _tree_for, start_one

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "gbfleet"

#: JSON-RPC codes used for PROTOCOL failures only. A tool that fails is a successful
#: exchange carrying `isError`, not one of these.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601

TOOLS: list[dict[str, Any]] = [
    {
        "name": "spawn",
        "description": (
            "Start ONE fleet member on a seat you already minted, in its own worktree. "
            "Takes no count: mint a seat and call this per child. Returns its agent id "
            "once it registers, or an error naming the adapter if it never does."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "adapter": {"type": "string", "description": "Vendor to run. Named, never inferred."},
                "enrolment_code": {"type": "string", "description": "The seat. Single-use."},
                "wave": {"type": "string", "description": "Names the branch: gb/<wave>-<n>."},
            },
            "required": ["adapter", "enrolment_code"],
        },
    },
    {
        "name": "stop",
        "description": (
            "Stop a child by agent id or pid. Idempotent — stopping something already "
            "gone is not an error. Cleans up nothing: the worktree survives for salvage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "pid": {"type": "integer"},
                "reason": {"type": "string", "description": "asked | scaled_down | shutdown"},
            },
        },
    },
    {
        "name": "ps",
        "description": "Children this supervisor started, running or not, with why each stopped.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "orphans",
        "description": (
            "gb/ branches with no worktree, and whether each carries a salvage commit. "
            "Mechanical: whether a half-finished diff is worth resuming is yours to decide."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


@dataclass
class Fleet:
    """Everything one supervisor process is holding.

    The lock is held for the server's lifetime (D-h), so a second `gbfleet mcp` on the
    same repository refuses to start rather than exceeding `--max-workers` between them.
    """

    repo: Path
    workspace: Path
    client: Graphban
    launch_for: Callable[[str], LaunchFactory]
    lock: Acquired | None = None
    limits: Limits = field(default_factory=Limits)
    partition: Partition = field(default_factory=Partition)
    children: list[Child] = field(default_factory=list)
    started: int = 0

    def find(self, agent_id: str | None, pid: int | None) -> Child | None:
        for child in self.children:
            if agent_id and child.agent_id == agent_id:
                return child
            if pid and child.pid == pid:
                return child
        return None


def _result(id_: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": payload}


def _ok(id_: Any, value: dict) -> dict:
    return _result(id_, {
        "content": [{"type": "text", "text": json.dumps(value)}],
        "structuredContent": value,
    })


def _tool_error(id_: Any, message: str) -> dict:
    """A tool that failed. NOT a JSON-RPC error.

    The planner has to be able to tell "your adapter is broken" from "the supervisor is
    gone", and a transport-level failure says the second when it means the first.
    """
    return _result(id_, {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {"error": {"message": message}},
        "isError": True,
    })


def _rpc_error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _describe(child: Child) -> dict:
    return {
        "agent_id": child.agent_id,
        "pid": child.pid,
        "adapter": child.adapter,
        "branch": child.branch,
        "worktree": str(child.worktree),
        "running": child.running,
        "registration_latency": child.registration_latency,
        "stopped_because": child.stopped_because.value if child.stopped_because else None,
    }


def call_tool(fleet: Fleet, name: str, args: dict) -> dict:
    """Dispatch one tool. Raises nothing the caller has to translate — failures return
    a message, and `handle` wraps them in `isError`."""
    if name == "spawn":
        adapter = args.get("adapter") or ""
        code = args.get("enrolment_code") or ""
        if not adapter or not code:
            raise ValueError("spawn needs both `adapter` and `enrolment_code`")

        wave = args.get("wave") or "wave"
        fleet.started += 1
        tree = _tree_for(fleet.repo, fleet.workspace, wave, str(fleet.started))
        seat = Seat(code=code, server_url=fleet.client.base_url, api_key=fleet.client.api_key)
        child = start_one(
            tree, seat, fleet.launch_for(adapter), fleet.client, fleet.limits,
            fleet.partition, workspace=fleet.workspace, wave_name=wave,
            slot=str(fleet.started), on_spawned=fleet.children.append,
        )
        return _describe(child)

    if name == "stop":
        child = fleet.find(args.get("agent_id"), args.get("pid"))
        if child is None:
            # Not an error. `stop` is idempotent because D-d gives revocation two paths
            # to it — the planner noticing, and the supervisor's backstop poll — and two
            # paths to the same transition is fine only if the second is harmless.
            return {"stopped": False, "reason": "no such child, or already forgotten"}
        reason = Reason(args.get("reason") or Reason.ASKED.value)
        result = stop(child, reason)
        return {"stopped": True, "escalated": result.escalated, "exit_code": result.exit_code,
                "child": _describe(child)}

    if name == "ps":
        return {"children": [_describe(c) for c in fleet.children],
                "running": sum(1 for c in fleet.children if c.running)}

    if name == "orphans":
        found = wt_mod.orphans(fleet.repo)
        return {"orphans": [
            {"branch": o.branch, "commit": o.commit, "subject": o.subject, "salvaged": o.salvaged}
            for o in found
        ]}

    raise LookupError(name)


def handle(fleet: Fleet, message: dict) -> dict | None:
    """One JSON-RPC message in, one response out. None means "no reply" (a notification)."""
    id_ = message.get("id")
    method = message.get("method")

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": _version()},
        })
    if method == "tools/list":
        return _result(id_, {"tools": TOOLS})
    if method != "tools/call":
        return _rpc_error(id_, METHOD_NOT_FOUND, f"unknown method {method!r}")

    params = message.get("params") or {}
    name = params.get("name")
    try:
        return _ok(id_, call_tool(fleet, name, params.get("arguments") or {}))
    except LookupError:
        return _rpc_error(id_, METHOD_NOT_FOUND, f"no such tool {name!r}")
    except (LaunchFailed, AdapterError, wt_mod.BranchExists, wt_mod.GitError, ValueError) as exc:
        # Everything a tool can legitimately fail with, reported as a tool result. The
        # adapter name and the child's stderr are already in these messages (D-a).
        return _tool_error(id_, str(exc))


def _version() -> str:
    from . import __version__

    return __version__


def serve(fleet: Fleet, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Read JSON-RPC lines, write JSON-RPC lines. That is the whole transport."""
    source = stdin or sys.stdin
    sink = stdout or sys.stdout

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _write(sink, _rpc_error(None, PARSE_ERROR, "request is not valid JSON"))
            continue
        if not isinstance(message, dict):
            _write(sink, _rpc_error(None, INVALID_REQUEST, "request must be a JSON object"))
            continue

        reply = handle(fleet, message)
        if reply is not None:
            _write(sink, reply)


def _write(sink: TextIO, message: dict) -> None:
    sink.write(json.dumps(message) + "\n")
    sink.flush()
