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

#: The WRITES a fleet member needs to do its job (S7). Kept in a separate tuple from the
#: reads above, because the argument that makes orientation safe to hand to a weak model —
#: nothing here changes server state — stops being true the moment these are mixed in.
#:
#: `claim_cluster` takes the work (P30 D3). `claim_next` stays on the server for humans
#: and solo sessions; a fleet child is not taught it, because it reserves no files.
#: `update_item` moves the item to `review` with evidence, and the server clamps a worker
#: at `review`: `done` is the reviewer's word (WORKER_STATUS_CEILING). `heartbeat` keeps
#: the lease alive across a `run_tests` that takes minutes.
#:
#: Absent, and the server would refuse them anyway: `sign_off`, `bounce`, `claim_review`,
#: `mint_enrolment`. D5 — done is not the agent's word.
COORDINATION_TOOLS: tuple[str, ...] = (
    "claim_cluster", "update_item", "heartbeat",
    # P30 D11. A worker that cannot create cannot file a typed human wait, and a
    # wait is an item on this tracker, not a free-text blocker. `link_items` is
    # how the original depends on that wait.
    "create_item", "link_items",
)

#: Arguments the AGENT owns and the model does not get to invent. **Overwritten, not
#: defaulted**, wherever the tool's schema has the field.
#:
#: Filling only a BLANK was the first version and it was wrong, which the GRPH-506 trace showed
#: on turn 1 of a spawned run: the claim call came back "needs to know which agent is calling"
#: because the model had supplied an `agent_id` of its own — plausible, truthy, and not this
#: agent. Truthy meant the real one was never substituted. It recovered by reading the refusal
#: and spent two of twelve turns doing it.
#:
#: An agent's identity is not a field a model gets an opinion about: naming a different one is
#: claiming work as somebody else, and naming a made-up one is a refusal it cannot act on.
INJECTED = ("agent_id",)

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
    "Open PRs against gitops.base_branch.value from `get_context` when source is not "
    "unmeasured; unset, unmeasured, or linked_unreachable means do not guess main.\n"
    "When gitops.release_defined_in.source is not unmeasured, follow that path or URL; "
    "unmeasured means do not invent docs/release.md or GitHub's source zip.\n"
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

    #: Every name this layer answers for. Set by `build`.
    names: tuple[str, ...] = ORIENTATION_TOOLS
    #: This agent's server-side identity, injected into calls that take one.
    agent_id: str = ""
    #: The item a successful claim handed back, or None.
    #:
    #: **FOUND BY THE S7 WALK.** When the model claims its own work, the harness was never
    #: told what it claimed — so the give-up path had no item id, `write_handoff` called
    #: `update_item(id="")`, the server refused, and the run exited 70 with the item stuck
    #: claimed until its lease expired. The loop's refusal to release without a handoff was
    #: correct and protected the record; it was protecting it from a hole in this wiring.
    claimed_item: str | None = None

    def handles(self, name: str) -> bool:
        return name in self.names

    def execute(self, call: ToolCall) -> ToolResult:
        """Forward to the server and hand back what it said.

        Every failure is a result the model can act on, exactly as in the execution layer: a
        wrong argument should cost a turn, not the run.
        """
        self.calls += 1
        arguments = dict(call.input)
        if self.agent_id:
            for field in INJECTED:
                if field in (self._schema(call.name) or {}):
                    arguments[field] = self.agent_id
        try:
            payload = self.client.call(call.name, **arguments)
        except Exception as exc:  # noqa: BLE001 — refusals, tool errors and outages all read back
            return ToolResult(id=call.id, content=f"{call.name}: {exc}", is_error=True)
        if call.name in ("claim_next", "claim_cluster"):
            self._remember_claim(payload)
        return ToolResult(id=call.id, content=_render(payload))

    def _remember_claim(self, payload: dict) -> None:
        """A successful claim, or nothing when the queue is empty.

        `claim_next` answers `{item: {id}}`; `claim_cluster` answers `{items: [{id}, ...]}`.
        Remembering only `claim_next` would leave a fleet worker's heartbeat without an
        id after the tool the PRD says to call (P30 D3/D4).
        """
        item = payload.get("item") if isinstance(payload.get("item"), dict) else None
        if item is None:
            items = payload.get("items")
            if isinstance(items, list) and items and isinstance(items[0], dict):
                item = items[0]
        if item is None and isinstance(payload, dict) and payload.get("id"):
            item = payload
        key = item.get("id") if isinstance(item, dict) else None
        if key:
            self.claimed_item = str(key)


    def _schema(self, name: str) -> dict:
        spec = next((s for s in self.specs if s.name == name), None)
        return (spec.input_schema.get("properties") or {}) if spec else {}


def _render(payload: dict) -> str:
    import json

    text = json.dumps(payload, indent=None, default=str)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + f"\n... truncated at {MAX_RESULT_CHARS} chars"
    return text


def build(client: Graphban, *, extra: tuple[str, ...] = (), agent_id: str = "") -> Orientation:
    """Fetch the manifest, keep what was asked for, refuse if any of it is gone.

    `extra` is how S7 adds `COORDINATION_TOOLS` without them becoming orientation: the reads
    stay a separately-named tuple, so "nothing in the orientation layer changes server state"
    remains a checkable claim rather than a sentence that used to be true.

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
    wanted = (*ORIENTATION_TOOLS, *extra)
    missing = [name for name in wanted if name not in manifest]
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
        for name in wanted
    ]
    return Orientation(client=client, specs=specs, names=wanted, agent_id=agent_id)
