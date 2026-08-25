"""Orientation: ask the graph before reading files (PRD-24 S6, D1's third layer).

**This is the cheapest performance work in the PRD, and the reason is arithmetic.** One
tool-calling turn against a local model is 22.2s (`qwen3-coder:30b`), 29.7s (`gpt-oss:20b`),
44.8s (`qwen3:30b-a3b`). A `code_neighbors` call that replaces a filesystem crawl is not a
nicety, it is the difference between a run that finishes and one that spends its budget
looking around. A vendor harness ignores these tools and greps, because it does not know
they exist. This agent is told.

**The schemas come from the server, not from a copy here.** Eight declared duplicates of
someone else's tool contract is a thing that goes stale quietly and is discovered as a model
calling with arguments nobody accepts — at 30 seconds a turn. `list_tools` fetches the same
manifest every MCP client fetches at connect, and this filters it.

**A missing tool refuses before a turn is spent.** If the server's manifest does not carry one
of these names, something has been renamed and the instruction below is telling the model to
call something that does not exist. Refusing at startup costs seconds; discovering it at turn
nine costs the run.

**Reads only, and that took an edit to make true.** Nothing in this layer changes server
state — no claim, no status move, no sign-off — which is what makes it safe to hand to a weak
model without any further argument. D1 lists `describe_code` among the eight and it does not
belong: see `NOT_ORIENTATION` below.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gbfleet.client import Graphban

from .llm import ToolCall, ToolResult, ToolSpec

#: The ones that answer "what is this code, and what touches it" (D1).
#: Every one is a READ. Widening this is a deliberate edit, in the same spirit as
#: `coord.WORKER_TOOLS` — and anything that writes belongs in that set, not this one.
ORIENTATION_TOOLS: tuple[str, ...] = (
    "get_code_map",
    "search_code",
    "code_neighbors",
    "related_work",
    "get_context",
    "search_memory",
    "get_item_details",
)

#: **A DELIBERATE DEVIATION FROM D1, which lists eight.**
#:
#: `describe_code` is not an orientation tool. It does not answer "what is this code" — it
#: ASSERTS it: "Upsert the codebase's structure as a queryable graph of `nodes` and `edges`",
#: with a `prune=true` that marks unseen nodes stale. It is the indexing half of the graph,
#: and handing it to a weak model mid-build means a bad description can damage the map every
#: other agent orients against.
#:
#: Measured against the live server, it is also the expensive one: 1628 characters of schema
#: against 242-535 for the rest — roughly 40% of the whole orientation budget, advertised on
#: every single turn, to buy a capability a builder has no use for.
#:
#: Named here rather than silently omitted, so the deviation is arguable rather than a
#: seven-item list nobody notices used to be eight.
NOT_ORIENTATION: tuple[str, ...] = ("describe_code",)

#: What the model is told, in the system prompt. Deliberately concrete: "orient first" is
#: advice nobody can follow, and a model that does not know `code_neighbors` exists will grep.
INSTRUCTION = (
    "ORIENT BEFORE YOU READ. This repository is indexed as a graph, and asking it costs one "
    "turn where crawling the filesystem costs many.\n"
    "- `search_code` finds where something lives. Use it before `grep`.\n"
    "- `code_neighbors` tells you what calls a thing and what it calls. Use it before "
    "reading a file to find out.\n"
    "- `get_item_details` and `related_work` say what the work is and what has already "
    "touched it.\n"
    "- `search_memory` is what earlier agents learned here. It is worth one turn.\n"
    "Read files when you have a specific file and a specific reason. Then edit, then "
    "run_tests."
)


class OrientationUnavailable(RuntimeError):
    """The graph tools are not what this agent was written against."""


#: Results are large — a `get_code_map` can run to thousands of lines. Bounded here rather
#: than by compaction, because compaction only helps AFTER the window has been filled.
MAX_RESULT_CHARS = 12_000


@dataclass
class Orientation:
    """The graph tools, as the model sees them."""

    client: Graphban
    specs: list[ToolSpec] = field(default_factory=list)
    #: How many orientation calls this run made. Reported, never used as a score — see
    #: `docs/orientation-metric-prd24.md` for why counting calls is the wrong metric.
    calls: int = 0

    def handles(self, name: str) -> bool:
        return name in ORIENTATION_TOOLS

    def execute(self, call: ToolCall) -> ToolResult:
        """Forward to the server and hand back what it said.

        Every failure is a result the model can act on, exactly as in the execution layer: a
        wrong argument should cost a turn, not the run.
        """
        self.calls += 1
        try:
            payload = self.client.call(call.name, **call.input)
        except Exception as exc:  # noqa: BLE001 — refusals, tool errors and outages all read back
            return ToolResult(id=call.id, content=f"{call.name}: {exc}", is_error=True)
        text = _render(payload)
        return ToolResult(id=call.id, content=text)


def _render(payload: dict) -> str:
    import json

    text = json.dumps(payload, indent=None, default=str)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + f"\n... truncated at {MAX_RESULT_CHARS} chars"
    return text


def build(client: Graphban) -> Orientation:
    """Fetch the manifest, keep the eight, refuse if any of them is gone.

    Refusing on a missing name rather than quietly advertising seven: the instruction names
    these tools, so a rename that goes unnoticed here becomes a model being told to call
    something the server has never heard of, discovered one 30-second turn at a time.
    """
    manifest = {t.get("name"): t for t in client.list_tools() if isinstance(t, dict)}
    if not manifest:
        raise OrientationUnavailable(
            "the server returned an empty tool manifest. Orientation is the reason this "
            "agent is cheaper than a vendor child (PRD-24 D1); starting without it means "
            "spending the whole budget crawling the filesystem."
        )
    missing = [name for name in ORIENTATION_TOOLS if name not in manifest]
    if missing:
        raise OrientationUnavailable(
            f"the server's manifest has no {', '.join(missing)}. These are named in the "
            "instruction this agent is given, so advertising the rest would leave it being "
            "told to call tools that do not exist. Refusing before a turn is spent."
        )
    specs = [
        ToolSpec(
            name=name,
            description=str(manifest[name].get("description") or ""),
            input_schema=dict(manifest[name].get("inputSchema") or {"type": "object"}),
        )
        for name in ORIENTATION_TOOLS
    ]
    return Orientation(client=client, specs=specs)
