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

import threading
import time

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from . import worktree as wt_mod
from .adapters import AdapterError, Tuning
from .tiers import TierTable
from .client import Graphban
from .lock import Acquired
from .seat import Seat
from .spawn import Child, LaunchFailed, Reason, stop
from .progress import NEVER_WROTE
from . import adopt as adopt_mod
from .supervisor import (
    Limits, LaunchFactory, Partition, Wave, _tree_for, item_status, start_one,
    watch_tick,
)

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
                "adapter": {"type": "string", "description": "Vendor to run. Named, never inferred. Optional when `tier` is given."},
                "tier": {
                    "type": "string",
                    "description": (
                        "A tier the operator mapped at launch (`gbfleet mcp --tier "
                        "cheap=gbagent:<model>`), resolved to adapter+model here (PRD-36 D6). "
                        "`adapter`/`model` override it when both are given. Unmapped: refused "
                        "naming the flag."
                    ),
                },
                "item": {
                    "type": "string",
                    "description": (
                        "The item this seat is BOUND to (from delegate(seat=true)), so the "
                        "instruction names it and gbagent gets --item. The registration "
                        "reply's `assigned` is the authority; this only tells the child what "
                        "to expect."
                    ),
                },
                "enrolment_code": {"type": "string", "description": "The seat. Single-use."},
                "wave": {"type": "string", "description": "Names the branch: gb/<wave>-<n>."},
                "fallback_model": {
                    "type": "string",
                    "description": (
                        "claude only. Comma-separated models to try when the primary is "
                        "overloaded. An unattended child that cannot get a model never "
                        "registers, so this turns a dead spawn into a slower one."
                    ),
                },
                "effort": {
                    "type": "string",
                    "description": "grok only. Reasoning effort, passed through unvalidated.",
                },
                "turns": {
                    "type": "string",
                    "description": (
                        "gbagent only. Turn budget (PRD-24 D6). Required for gbagent: the "
                        "adapter refuses to guess it, so a gbagent spawn without it exits "
                        "before registering."
                    ),
                },
                "window": {
                    "type": "string",
                    "description": (
                        "gbagent only. The model's context window in tokens (PRD-24 D7). "
                        "Required for gbagent, for the same reason as `turns`."
                    ),
                },
                "debug": {
                    "type": "boolean",
                    "description": (
                        "Ask this vendor to write a debug log beside its stdout. grok and "
                        "claude can; cursor-agent and gbagent have no such flag and the "
                        "reply says so rather than leaving you to assume otherwise."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Vendor model for this child, e.g. 'sonnet' or 'grok-4.5'. Omit "
                        "for the vendor default. NAMED, never inferred — the supervisor "
                        "carries it and chooses nothing."
                    ),
                },
            },
            "required": ["enrolment_code"],
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
        "description": (
            "Children this supervisor started, running or not, with why each stopped — "
            "and how much each has WRITTEN. `running` alone cannot tell a child that is "
            "thinking from one that is wedged; `output.silent_for` and the `quiet` list "
            "can."
        ),
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
    launch_for: Callable[..., LaunchFactory]
    lock: Acquired | None = None
    limits: Limits = field(default_factory=Limits)
    partition: Partition = field(default_factory=Partition)
    children: list[Child] = field(default_factory=list)
    started: int = 0
    #: Same record `up` uses, so the MCP watch tick writes wall-clock and disown
    #: failures somewhere `ps` and a later `until` can read (P30 D6).
    wave: Wave = field(default_factory=Wave)
    #: PRD-36 D6/D16: what each tier means on this machine, named at launch, fixed for
    #: the life of the process.
    tiers: TierTable = field(default_factory=TierTable)

    def __post_init__(self) -> None:
        # One partition object. `start_one` is given `fleet.partition`; `watch_tick`
        # reads `wave.partition`. Two copies would mean MCP never learned the presence
        # TTL that `await_registration` just fetched.
        self.wave.partition = self.partition

    def tick(self, *, debug: bool = False) -> None:
        """One pass of the same watch loop `up` runs. Tests call this; `serve` ticks it."""
        watch_tick(self.wave, self.children, self.limits, self.client, debug=debug)
        adopt_mod.persist(adopt_mod.children_path(self.repo), self.children)

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
    """One child, including whether it is actually producing anything.

    `running` alone cannot answer the question a planner is really asking. A child
    thinking hard and a child wedged on a prompt nobody can answer both report
    `running: true`, and until GRPH-586 that was the whole of what this returned — while
    `spawn()` had already attached an output watcher to every child and nothing here
    ever read it.

    Sampling mutates the watcher: it records when output was last seen. If the watch
    tick and this both sample one child they split `new_bytes` between them, each
    seeing only the delta the other had not consumed. `silent_for` stays right either
    way, because it derives from the shared last-seen timestamp.
    """
    described = {
        "agent_id": child.agent_id,
        "pid": child.pid,
        "adapter": child.adapter,
        "branch": child.branch,
        "worktree": str(child.worktree),
        "running": child.running,
        "registration_latency": child.registration_latency,
        "stopped_because": child.stopped_because.value if child.stopped_because else None,
        "debug_log": str(child.debug_path) if child.debug_path else None,
    }
    if child.output is not None:
        reading = child.output.sample(time.monotonic())
        described["output"] = reading.as_dict()
    return described


def _is_quiet(described: dict, quiet_after: float) -> bool:
    """Whether this child has produced nothing for longer than the supervisor allows
    before saying so.

    `NEVER_WROTE` is not a duration and must not be compared as one. It is treated as
    quiet once the child is older than the threshold — a child that has made no sound
    since it started is the worse case, not an exempt one.
    """
    reading = described.get("output")
    if not reading:
        return False
    silent = reading.get("silent_for")
    if silent == NEVER_WROTE:
        return float(reading.get("age") or 0.0) >= quiet_after
    try:
        return float(silent) >= quiet_after
    except (TypeError, ValueError):
        return False


def call_tool(fleet: Fleet, name: str, args: dict) -> dict:
    """Dispatch one tool. Raises nothing the caller has to translate — failures return
    a message, and `handle` wraps them in `isError`."""
    if name == "spawn":
        adapter = args.get("adapter") or ""
        model = args.get("model") or ""
        tier = args.get("tier") or ""
        code = args.get("enrolment_code") or ""
        if not code:
            raise ValueError("spawn needs `enrolment_code`")
        via_tier = bool(tier) and not adapter
        if via_tier:
            # PRD-36 D6: the table resolves; the supervisor chooses nothing. An explicit
            # adapter wins over the tier, and the reply says which ran either way.
            lane = fleet.tiers.resolve(tier)
            adapter, model = lane.adapter, (model or lane.model)
        if not adapter:
            raise ValueError("spawn needs `adapter` or a mapped `tier`")

        live = sum(1 for child in fleet.children if child.running)
        if live >= fleet.limits.max_workers:
            raise ValueError(
                f"already {live} live worker(s); max_workers is {fleet.limits.max_workers}. "
                "stop one, or wait for one to exit"
            )

        wave = args.get("wave") or "wave"
        tree = None
        slot = str(fleet.started + 1)
        resumes = list(wt_mod.choose_resume(wt_mod.orphans(fleet.repo), item_status(fleet.client)))
        if resumes:
            orphan = resumes[0]
            slot = orphan.branch.rsplit("-", 1)[-1] or slot
            try:
                tree = wt_mod.resume(
                    fleet.repo, fleet.workspace / f"{wave}-{slot}-resume", orphan,
                )
                fleet.wave.resumed.append(orphan.branch)
            except wt_mod.ResumeFailed:
                tree = None
        while tree is None:
            fleet.started += 1
            if fleet.started > 1000:
                raise ValueError("no free gb/ slot under 1000")
            slot = str(fleet.started)
            try:
                tree = _tree_for(fleet.repo, fleet.workspace, wave, slot)
            except wt_mod.BranchExists:
                continue
        seat = Seat(code=code, server_url=fleet.client.base_url, api_key=fleet.client.api_key,
                    item=(args.get("item") or None))

        def remember(child: Child) -> None:
            fleet.children.append(child)
            adopt_mod.persist(adopt_mod.children_path(fleet.repo), fleet.children)

        child = start_one(
            tree, seat,
            fleet.launch_for(
                adapter,
                model,
                Tuning(
                    fallback_model=args.get("fallback_model") or "",
                    effort=args.get("effort") or "",
                    turns=str(args.get("turns") or ""),
                    window=str(args.get("window") or ""),
                ),
            ),
            fleet.client, fleet.limits,
            fleet.partition, workspace=fleet.workspace, wave_name=wave,
            slot=slot, on_spawned=remember,
            debug_file=(
                fleet.workspace / "logs" / f"{wave}-{slot}" / "debug.log"
                if args.get("debug")
                else None
            ),
        )
        adopt_mod.persist(adopt_mod.children_path(fleet.repo), fleet.children)
        described = _describe(child)
        # PRD-36 D6/D15: name what ran and what the seat handed the child. `assigned` is
        # None when the seat was unbound; `tier` is None when adapter/model were explicit.
        described["model"] = model
        described["tier"] = tier if via_tier else None
        described["assigned"] = child.assigned
        # Said once, at spawn, for the same reason the wave summary says it: an operator
        # who asked for debug and gets a quiet log from an adapter that has no debug flag
        # would reasonably conclude the child is fine.
        if args.get("debug") and child.debug_path is None:
            described["debug_unavailable"] = (
                f"{child.adapter} has no debug flag; output sampling only"
            )
        return described

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
        children = [_describe(c) for c in fleet.children]
        # Counted here rather than left to the caller. A planner that has to compare
        # `silent_for` against a threshold per child in order to notice a stuck worker
        # is a planner that will not — and the number it would need, `quiet_after`, is
        # the supervisor's, not something the planner should be guessing at.
        quiet = [
            c["agent_id"] or f"{c['adapter']}:{c['pid']}"
            for c in children
            if c["running"] and _is_quiet(c, fleet.limits.quiet_after)
        ]
        return {
            "children": children,
            "running": sum(1 for c in fleet.children if c.running),
            "quiet": quiet,
            "quiet_after": fleet.limits.quiet_after,
        }

    if name == "orphans":
        found = wt_mod.orphans(fleet.repo)
        return {"orphans": [
            {"branch": o.branch, "commit": o.commit, "subject": o.subject,
             "salvaged": o.salvaged, "item_keys": list(o.item_keys)}
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


def serve(fleet: Fleet, stdin: TextIO | None = None, stdout: TextIO | None = None,
          *, poll: float = 1.0) -> None:
    """Read JSON-RPC lines, write JSON-RPC lines. That is the whole transport.

    A daemon ticks `watch_tick` while stdin is open (P30 D6). `spawn` used to return
    and leave wall-clock, disowned-after and reap only in `up`'s `_wait_out`, so a
    hung child on this surface ran until a human called `stop`.
    """
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    halt = threading.Event()

    def _ticks() -> None:
        # First pass immediately, then on `poll`. `wait` rather than sleep so close
        # of stdin does not spend a full interval being tidy.
        while not halt.is_set():
            fleet.tick()
            halt.wait(poll)

    watcher = threading.Thread(target=_ticks, name="gbfleet-watch", daemon=True)
    watcher.start()
    try:
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
    finally:
        halt.set()
        watcher.join(timeout=5.0)


def _write(sink: TextIO, message: dict) -> None:
    sink.write(json.dumps(message) + "\n")
    sink.flush()
