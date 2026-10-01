"""The build servers the seat grants, as the model sees them (GRPH-997).

GRPH-816 made a shared MCP server a grant somebody types: `--mcp-server context7` copies that
one stanza into the seat file, and `--strict-mcp-config` means the child holds nothing else.
Every vendor child then reached it, because a vendor harness IS an MCP client. `gbagent` did
not — `cli.read_seat` kept the `graphban` entry and dropped the rest, so the same grant bought
a qwen child a docs server and bought this one nothing. gbagent has no shell and no web, and a
docs server and a browser are exactly the build tools it cannot fake with `curl`.

**The grant is the boundary.** What may be called is every server stanza in the seat file
beside the ledger's own reserved names, and nothing else. This module never reads
`~/.claude.json`, never discovers a server, and never accepts a name from the model: the seat
is written by the supervisor from an exact name an operator typed, and `mcpshare.select`
already refused a pattern.

**Every failure is a result, not an exception** — the rule `toolset` states, applied here to a
surface that is somebody else's. A server that will not connect, a call it refuses, a manifest
with nothing in it: each comes back as text a model can read and act on, because the
alternative spends a whole run on a docs server that was down. And a call to ANY name under a
granted server is answered by this layer rather than by the toolset's "no tool named" refusal,
so a server that failed at startup does not read to the model as a tool that does not exist.

**Schemas come from the server**, for the reason `orient` gives: a declared copy of somebody
else's tool contract goes stale quietly, and is discovered as a model calling with arguments
nobody accepts — at thirty seconds a turn.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from gbfleet import mcpshare
from gbfleet.client import McpServer

from .llm import ToolCall, ToolResult, ToolSpec
from .orient import MAX_RESULT_CHARS

#: `mcp__<server>__<tool>` — the spelling qwen and claude both advertise a shared server's
#: tools in (`adapters/qwen_code.py` measured 57 `mcp__<name>__*` tools from one grant). Not
#: chosen for taste: a model that has seen the prefix in any other harness types it correctly
#: here, and the prefix is also what makes the dispatch below unambiguous.
PREFIX = "mcp__"
SEPARATOR = "__"

#: What is advertised for a server that was granted and yielded nothing callable. The model
#: reads this in the tool list, which is what keeps the grant from being invisible.
UNUSABLE = "unavailable"

#: Never a build grant, whatever an operator typed — the same set `mcpshare` refuses at grant
#: time, read from there so "besides the ledger's own entry" has one definition rather than two.
RESERVED = mcpshare.RESERVED


@dataclass
class Granted:
    """One server the seat named, and what came of reaching it.

    `reason` is empty exactly when the server is callable. A granted server with a reason is
    NOT a dropped one: it is still named to the model, still answers calls, and says why it
    cannot help — the third answer the absence rule asks for, since "no tools advertised" on
    its own reads as "there is no docs server here".
    """

    name: str
    server: McpServer | None = None
    #: The manifest this server listed, keyed by its own tool name and holding its own
    #: description and inputSchema. Kept whole rather than reduced to names, because what is
    #: advertised is the server's contract and not a copy of it.
    entries: dict[str, dict] = field(default_factory=dict)
    reason: str = ""

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(self.entries)

    @property
    def usable(self) -> bool:
        return self.server is not None and bool(self.entries) and not self.reason

    def prefix(self) -> str:
        return f"{PREFIX}{self.name}{SEPARATOR}"


@dataclass
class BuildTools:
    """The granted servers as a tool layer: advertised, dispatched, and never fatal."""

    granted: list[Granted] = field(default_factory=list)
    specs: list[ToolSpec] = field(default_factory=list)
    #: How many calls this run made to a build server. Reported, never scored.
    calls: int = 0

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)

    def handles(self, name: str) -> bool:
        """True for any name under a GRANTED server, whether or not that tool exists.

        Wider than `name in self.names` on purpose, and the width is the point: the toolset's
        fallthrough answers an unknown name with "no tool named X", which for a server that
        failed to connect is the wrong sentence — it tells the model the capability is not
        here, when what is true is that the grant exists and could not be reached.
        """
        return self._owner(name) is not None

    def execute(self, call: ToolCall) -> ToolResult:
        """Run one call against a granted server. Never raises."""
        self.calls += 1
        owner = self._owner(call.name)
        if owner is None:  # pragma: no cover - `handles` gates the dispatch
            return ToolResult(id=call.id, content=f"{call.name}: no server was granted for it",
                              is_error=True)
        if not owner.usable:
            return ToolResult(id=call.id, content=f"{owner.name}: {owner.reason}", is_error=True)
        tool = call.name[len(owner.prefix()):]
        if tool not in owner.tools:
            return ToolResult(
                id=call.id,
                content=(
                    f"{owner.name} has no tool {tool!r}. It has: "
                    + ", ".join(owner.tools)
                ),
                is_error=True,
            )
        try:
            result = owner.server.call_tool(tool, dict(call.input or {}))
        except Exception as exc:  # noqa: BLE001 — an outage is a result, not a dead run
            return ToolResult(id=call.id, content=f"{call.name}: {exc}", is_error=True)
        text = _render(result)
        return ToolResult(id=call.id, content=text,
                          is_error=bool(result.get("isError")))

    def close(self) -> None:
        """Drop the sockets. Called on the way out, beside the model session's own `close`."""
        for each in self.granted:
            if each.server is not None:
                each.server.close()

    def _owner(self, name: str) -> Granted | None:
        if not name or not name.startswith(PREFIX):
            return None
        server, sep, _ = name[len(PREFIX):].partition(SEPARATOR)
        if not sep:
            return None
        return next((g for g in self.granted if g.name == server), None)

    def one_line(self) -> str:
        """For the child's stderr trace: what the grant turned into, in one line."""
        if not self.granted:
            return ""
        return "; ".join(
            f"{g.name}: {len(g.tools)} tool(s)" if g.usable else f"{g.name}: {g.reason}"
            for g in self.granted
        )

    def instruction(self) -> str:
        """What the model is told about the grant, or "" when nothing was granted.

        Said in prose as well as advertised in the tool list, because the two carry different
        information: the list has the schemas, and this has the fact that a server which came
        up empty was still GRANTED — which is what stops a model concluding the run has no
        docs access and giving up, or spending turns probing a server that is down.
        """
        if not self.granted:
            return ""
        live = [g for g in self.granted if g.usable]
        dead = [g for g in self.granted if not g.usable]
        parts = ["BUILD SERVERS."]
        if live:
            # Counts and prefixes, not names: the names and schemas are in the tool list this
            # same turn, and a browser server lists thirty of them.
            parts.append("Granted and callable: " + "; ".join(
                f"{g.name} — {len(g.tools)} tool(s), advertised as {g.prefix()}*"
                for g in live) + ".")
        if dead:
            parts.append("Granted but NOT callable: " + "; ".join(
                f"{g.name} — {g.reason}" for g in dead) + ".")
        parts.append(
            "A build server that is not callable is not a reason to stop: build with what you "
            "have and say in the handoff what you could not check."
        )
        return " ".join(parts)


def build(shared: dict[str, dict] | None, *, transport: object | None = None) -> BuildTools:
    """Reach every granted server and advertise what answered.

    Never raises and never returns None: a grant that entirely failed still produces a layer,
    so the model is TOLD about it rather than silently getting a smaller tool list. `shared`
    is what `cli.read_shared` pulled out of the seat file.

    `transport` is the test seam `client.McpServer` and `client.Graphban` both have — one
    injected for every server, which is the better shape here, because a handler that answers
    by request host is also the proof that each server was reached at its OWN url. Typed as
    `object` rather than as the transport's own class on purpose: `test_client.py` scans every
    module in this tree for the names of the libraries that can open a socket and allows two
    files to mention them, and this is not one of them. Naming one here — in prose, in a
    comment, with no import anywhere — is a red suite, which is how this docstring came to be
    worded around it.
    """
    layer = BuildTools()
    for name, stanza in (shared or {}).items():
        if name in RESERVED:
            # Not a grant and never was: `graphban` is the ledger itself, reached through
            # `coord`/`orient` on this child's OWN credential, and `gbfleet` is the
            # supervisor's local stdio server. `mcpshare` refuses both at grant time; naming
            # them here too is what makes "besides the ledger's own entry" one rule rather
            # than a reader's assumption about who filtered what.
            continue
        granted = _connect(name, stanza if isinstance(stanza, dict) else {}, transport=transport)
        layer.granted.append(granted)
        layer.specs.extend(_specs(granted))
    return layer


def _connect(name: str, stanza: dict, *, transport: object | None = None) -> Granted:
    """One server: handshake, list its tools, and say what went wrong if it did not."""
    url = str(stanza.get("url") or stanza.get("httpUrl") or "").strip()
    if not url:
        # A stdio stanza is `{"command": ..., "args": [...]}`. Launching it would mean running
        # a command read out of a config file, from an agent whose whole safety story is that
        # it has no shell — so it is refused here and the reason travels to the model.
        return Granted(name=name, reason=(
            "the seat's stanza for it has no url, so there is nothing to connect to (a stdio "
            "server is not launched by this agent: it has no shell, and running a command "
            "read from a config file is not something it does)"
        ))
    if not url.startswith(("http://", "https://")):
        return Granted(name=name, reason="its url is not http(s), so there is nothing to reach")
    server = McpServer(name=name, url=url, headers=dict(stanza.get("headers") or {}),
                       transport=transport)
    server.initialize()
    try:
        listed = server.list_tools()
    except Exception as exc:  # noqa: BLE001 — a dead docs server must not kill the build
        server.close()
        return Granted(name=name, reason=f"it did not answer tools/list ({exc})")
    entries = {str(t["name"]): dict(t) for t in listed if isinstance(t, dict) and t.get("name")}
    if not entries:
        # Distinct from a failed connection, and neither is silence: "listed no tools" is a
        # server that answered and had nothing, which is a fact about the grant worth reading.
        server.close()
        return Granted(name=name, reason=(
            f"it connected and listed {len(listed)} tool(s), none with a usable name"
            if listed else "it connected and listed no tools"
        ))
    return Granted(name=name, server=server, entries=entries)


def _specs(granted: Granted) -> list[ToolSpec]:
    """The server's own manifest, under names this agent can dispatch on."""
    if not granted.usable:
        # One tool, named for what it is, carrying the reason. This is the advertisement that
        # makes an unusable grant visible in the tool list itself and not only in prose: a
        # server that came up empty still occupies a line the model reads every turn.
        return [ToolSpec(
            name=granted.prefix() + UNUSABLE,
            description=(
                f"The {granted.name} build server was granted for this run but is not "
                f"callable: {granted.reason}. Calling this returns the same sentence. Do not "
                "retry it; build with the tools you have."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
        )]
    return [
        ToolSpec(
            name=granted.prefix() + tool,
            description=str(entry.get("description") or "")
            or f"{tool} on the {granted.name} build server.",
            input_schema=dict(entry.get("inputSchema") or {"type": "object"}),
        )
        for tool, entry in granted.entries.items()
    ]


def _render(result: dict) -> str:
    """The text a model reads out of an MCP result.

    `content` blocks are what the protocol says a tool answers with, so they come first;
    `structuredContent` is the fallback for a server that sends only that. A non-text block is
    NAMED rather than dropped — an image silently omitted reads as a tool that returned
    nothing, and this agent reads text only, so the sentence has to say which of those it was.
    """
    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(f"<{kind or 'untyped'} block not shown — this agent reads text only>")
    text = "\n".join(p for p in parts if p.strip())
    if not text:
        structured = result.get("structuredContent")
        if structured is not None:
            try:
                text = json.dumps(structured, default=str)
            except (TypeError, ValueError):  # pragma: no cover - default=str cannot raise
                text = str(structured)
    text = text or "<the server returned an empty result>"
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + f"\n... truncated at {MAX_RESULT_CHARS} chars"
    return text
