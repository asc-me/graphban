"""The execution tools as the model sees them: advertised, dispatched, and refused (S3, D1).

S1 and S2 built the tools. This is the layer that turns them into something a model can call —
a JSON Schema per tool, a dispatch that never trusts what came back, and the rule that makes the
whole surface safe to hand to a weak model:

**Every failure is a result, not an exception.** A path outside the worktree, a missing file, an
ambiguous edit anchor, an unknown tool name, an argument of the wrong type — each comes back as
`is_error=True` with a sentence saying what to do instead, and the model gets another turn. A
crash would spend the entire run on one wrong path (D2).

**The descriptions are part of the token budget.** The manifest a worker already holds is ~11.0k
tokens for 44 coordination tools; these seven are advertised on every single turn, so each
description says what the tool does and the one thing that will otherwise be got wrong, and
stops. This is the same ceiling discipline GRPH-474 applied to the MCP manifest.

The toolset also *watches* what it dispatched. When the loop gives up it has to write a note
saying where the tests stand and what changed (D6), and the honest source for that is what
actually ran — not what the model claimed in prose.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path

from . import tools, verify
from .config import VerifyConfig
from .llm import ToolCall, ToolResult, ToolSpec
from .workspace import ToolError

_PATH = {"type": "string", "description": "Path relative to the worktree root."}


def _spec(name: str, description: str, properties: dict, required: list[str]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": properties, "required": required},
    )


SPECS: list[ToolSpec] = [
    _spec(
        "read_file",
        "Read a file. Use start/count for a range in a large file rather than reading it whole.",
        {"path": _PATH,
         "start": {"type": "integer", "description": "First line, 1-indexed. Default 1."},
         "count": {"type": "integer", "description": "Lines to read. Default: to the end."}},
        ["path"],
    ),
    _spec(
        "list_dir",
        "List one directory. Not recursive — use grep to search a tree.",
        {"path": _PATH},
        [],
    ),
    _spec(
        "grep",
        "Search file contents by regex. Returns path, line number and the matching line.",
        {"pattern": {"type": "string", "description": "Python regular expression."},
         "path": _PATH,
         "glob": {"type": "string", "description": "Filename filter, e.g. '*.py'. Default '*'."}},
        ["pattern"],
    ),
    _spec(
        "write_file",
        "Write a file whole, creating missing parent directories. Overwrites existing content.",
        {"path": _PATH, "content": {"type": "string", "description": "The complete new content."}},
        ["path", "content"],
    ),
    _spec(
        "edit_file",
        "Replace an exact substring. Refused if 'old' is absent or appears more than once, so "
        "include enough surrounding lines to make it unique.",
        {"path": _PATH,
         "old": {"type": "string", "description": "Exact text to replace, matched literally."},
         "new": {"type": "string", "description": "Replacement text."}},
        ["path", "old", "new"],
    ),
    _spec(
        "run_tests",
        "Run this repository's declared test command. Takes no arguments — the command is "
        "fixed, and composing your own is not possible.",
        {},
        [],
    ),
    _spec(
        "git_diff",
        "Show what you have changed in the worktree so far.",
        {"path": _PATH},
        [],
    ),
]


@dataclass
class Toolset:
    """Dispatch for the execution layer, plus what it saw happen.

    `root` and `cfg` are held here rather than passed by the model: a model that could name its
    own worktree root could name a different one, and the boundary would be advisory.
    """

    root: Path
    cfg: VerifyConfig
    #: The graph layer (S6), or None when this agent has no server to ask. Advertised BEFORE
    #: the execution tools, because the order a model reads a tool list in is the cheapest
    #: nudge available and the whole point of D1 is that it reaches for the graph first.
    orientation: object | None = None
    #: The last `run_tests` outcome, as returned by `verify.run_tests`. `None` means the tests
    #: were never run — which is NOT the same as a clean run, and the handoff note says so.
    last_tests: dict | None = None
    #: Paths the model wrote or edited, in order, deduplicated.
    written: list[str] = field(default_factory=list)
    #: Files this run has read IN FULL, path -> content hash at the time (GRPH-515).
    #: A ranged read is deliberately absent: seeing lines 1-50 of 856 is not seeing the file.
    seen: dict = field(default_factory=dict)
    #: Tool calls that came back as errors. Counted for the handoff note: a run that spent
    #: thirty turns being refused failed differently from one that ran out of ideas.
    refusals: int = 0

    @property
    def claimed_item(self) -> str | None:
        """What the model claimed, so the heartbeat can extend that lease (P30 D4)
        and the give-up path can write a handoff about it."""
        return getattr(self.orientation, "claimed_item", None)

    @property
    def specs(self) -> list[ToolSpec]:
        if self.orientation is None:
            return SPECS
        return [*self.orientation.specs, *SPECS]

    #: Statuses that CLAIM the work is finished. Moving to either without having written
    #: anything is the failure the S7 walk found — see `_completion_guard`.
    COMPLETION_STATUSES = ("review", "done")

    def execute(self, call: ToolCall) -> ToolResult:
        """Run one tool call. Never raises for anything the model did wrong."""
        if self.orientation is not None and self.orientation.handles(call.name):
            refusal = self._completion_guard(call)
            if refusal is not None:
                return refusal
            call = self._with_measured_touchpoints(call)
            return self.orientation.execute(call)
        handler = getattr(self, f"_do_{call.name}", None)
        if handler is None or not call.name:
            self.refusals += 1
            return ToolResult(
                id=call.id,
                content=(
                    f"no tool named {call.name!r}. Available: "
                    + ", ".join(spec.name for spec in self.specs)
                ),
                is_error=True,
            )
        # Arguments are bound BEFORE the call, so a TypeError raised *inside* a tool is not
        # laundered into "you passed bad arguments". Those two are indistinguishable by type,
        # and reporting our own defect as the model's mistake would send it round the loop
        # trying different arguments until the budget ran out. A tool that raises TypeError
        # internally is an adapter fault and should read as one — a crash (D6).
        try:
            bound = inspect.signature(handler).bind(**call.input)
        except TypeError as exc:
            self.refusals += 1
            return ToolResult(
                id=call.id,
                content=f"{call.name}: bad arguments ({exc}). Expected: "
                        f"{sorted(next(s for s in SPECS if s.name == call.name).input_schema['properties'])}",
                is_error=True,
            )
        try:
            return ToolResult(id=call.id, content=handler(*bound.args, **bound.kwargs))
        except ToolError as exc:
            # Includes OutsideWorktree, which subclasses it. This is the refusal D2 promises
            # reaches the model as something it can correct.
            self.refusals += 1
            return ToolResult(id=call.id, content=str(exc), is_error=True)

    # -- handlers. Each returns the string the model reads. ------------------------------

    def _do_read_file(self, path: str, start: int = 1, count: int = 0) -> str:
        out = tools.read_file(self.root, path, start=start, count=count)
        # Recorded only for a WHOLE read. `write_file` may replace a file this run has seen,
        # and a range is not seeing it (GRPH-515) — the whole point is that nothing gets
        # deleted that nobody looked at.
        if out["start"] == 1 and out["lines"] == out["total_lines"]:
            self.seen[path] = out["hash"]
        header = f"{path} lines {out['start']}-{out['start'] + out['lines'] - 1} of {out['total_lines']}"
        return f"{header}\n{out['text']}"

    def _do_list_dir(self, path: str = ".") -> str:
        out = tools.list_dir(self.root, path)
        rows = [f"{e['name']}/" if e["kind"] == "dir" else e["name"] for e in out["entries"]]
        return f"{path}: {len(rows)} entries\n" + "\n".join(rows)

    def _do_grep(self, pattern: str, path: str = ".", glob: str = "*") -> str:
        out = tools.grep(self.root, pattern, path=path, glob=glob)
        if not out["hits"]:
            return f"no match for {pattern!r} under {path}"
        rows = [f"{h['path']}:{h['line']}: {h['text']}" for h in out["hits"]]
        return "\n".join(rows) + ("\n... truncated" if out["truncated"] else "")

    def _do_write_file(self, path: str, content: str) -> str:
        out = tools.write_file(self.root, path, content, seen=self.seen.get(path))
        # What it now holds is what it just wrote, so a second replacement in the same run is
        # not blocked by a hash it made stale itself.
        self.seen[path] = tools.content_hash(content)
        self._record(path)
        return f"wrote {path} ({out['bytes']} bytes, {'created' if out['created'] else 'replaced'})"

    def _do_edit_file(self, path: str, old: str, new: str) -> str:
        out = tools.edit_file(self.root, path, old, new)
        # NOTHING forgotten here on purpose. An edit moves the file underneath whatever was
        # read, and the HASH is what notices — a `seen.pop` alongside it was a second way of
        # saying the same thing, and sabotage showed it could not fail. If the edit happened
        # to produce identical content the memory is still accurate, which the hash gets
        # right and a pop would get wrong.
        self._record(path)
        return f"edited {path} ({out['replaced']} replacement)"

    def _do_run_tests(self) -> str:
        out = verify.run_tests(self.root, self.cfg)
        self.last_tests = out
        if out["ok"]:
            return f"PASS ({out['command']}): {_tally(out)}"
        named = "\n".join(out["failed_tests"])
        return (
            f"FAIL exit {out['exit_code']} ({out['command']}): {_tally(out)}\n"
            + (f"failed:\n{named}\n" if named else "")
            + f"--- last lines ---\n{out['tail']}"
        )

    def _do_git_diff(self, path: str = ".") -> str:
        out = verify.git_diff(self.root, path=path)
        if out["empty"]:
            return f"no changes under {path}"
        return out["diff"] + ("\n... truncated" if out["truncated"] else "")

    def _with_measured_touchpoints(self, call: ToolCall) -> ToolCall:
        """P30 D10. The harness owns the measurement; the model does not get to invent it.

        Sends this run's written paths only. Empty is not a write — stripping `[]` (and
        any paths the model guessed) is what stops a completion from wiping declared
        / predicted areas and reading as "no collision".
        """
        if call.name != "update_item":
            return call
        arguments = dict(call.input)
        if self.written:
            arguments["touchpoints"] = list(self.written)
        else:
            arguments.pop("touchpoints", None)
        return ToolCall(id=call.id, name=call.name, input=arguments)

    def _completion_guard(self, call: ToolCall) -> ToolResult | None:
        """Refuse to hand work over as finished when nothing was changed.

        **FOUND BY THE S7 ACCEPTANCE WALK, and it is the reason that walk exists.**
        `qwen3-coder:30b` claimed a real item, ran the suite — which passed, because it had
        changed nothing — and moved the item to `review` with a `test` receipt reading "Ran all
        tests and verified the fix". `git diff` was empty. Seven turns, 9.8 minutes, and a
        false completion in the ledger.

        The server cannot catch this: it does not know worktrees exist, and an item arriving in
        `review` with evidence looks exactly like finished work. But this agent owns the write
        tool, which is the same argument D2 makes about the worktree boundary — the one
        property we have that a vendor child does not.

        A refusal, not a crash: the model reads it and can go and do the work. The message says
        what is missing rather than that it was naughty, because the next turn costs 22-45
        seconds and a refusal it cannot act on costs the run.

        This does not make the agent honest. It makes ONE specific lie impossible to tell
        through this tool — a model can still write something irrelevant and claim it. That is
        what review is for, and `sign_off` still refuses the author.
        """
        if call.name != "update_item":
            return None
        if str(call.input.get("status") or "") not in self.COMPLETION_STATUSES:
            return None
        if self.written:
            return None
        self.refusals += 1
        return ToolResult(
            id=call.id,
            content=(
                "refused: you have not changed any file in this worktree, so this item is not "
                "ready for review. `git_diff` will show you the same thing. Make the change "
                "with write_file or edit_file, run_tests to check it, and then move it. "
                "Passing tests on an unchanged repository are not evidence of a fix."
            ),
            is_error=True,
        )

    def _record(self, path: str) -> None:
        if path not in self.written:
            self.written.append(path)


def _tally(out: dict) -> str:
    """Counts, or an admission. `None` is not zero — see `verify._read_counts`."""
    if out["passed"] is None and out["failed"] is None:
        return "counts unreadable from this runner's output; see the tail"
    return f"{out['passed']} passed, {out['failed']} failed"
