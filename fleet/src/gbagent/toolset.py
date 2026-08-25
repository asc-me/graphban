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
    #: The last `run_tests` outcome, as returned by `verify.run_tests`. `None` means the tests
    #: were never run — which is NOT the same as a clean run, and the handoff note says so.
    last_tests: dict | None = None
    #: Paths the model wrote or edited, in order, deduplicated.
    written: list[str] = field(default_factory=list)
    #: Tool calls that came back as errors. Counted for the handoff note: a run that spent
    #: thirty turns being refused failed differently from one that ran out of ideas.
    refusals: int = 0

    @property
    def specs(self) -> list[ToolSpec]:
        return SPECS

    def execute(self, call: ToolCall) -> ToolResult:
        """Run one tool call. Never raises for anything the model did wrong."""
        handler = getattr(self, f"_do_{call.name}", None)
        if handler is None or not call.name:
            self.refusals += 1
            return ToolResult(
                id=call.id,
                content=(
                    f"no tool named {call.name!r}. Available: "
                    + ", ".join(s.name for s in SPECS)
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
        out = tools.write_file(self.root, path, content)
        self._record(path)
        return f"wrote {path} ({out['bytes']} bytes, {'created' if out['created'] else 'replaced'})"

    def _do_edit_file(self, path: str, old: str, new: str) -> str:
        out = tools.edit_file(self.root, path, old, new)
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

    def _record(self, path: str) -> None:
        if path not in self.written:
            self.written.append(path)


def _tally(out: dict) -> str:
    """Counts, or an admission. `None` is not zero — see `verify._read_counts`."""
    if out["passed"] is None and out["failed"] is None:
        return "counts unreadable from this runner's output; see the tail"
    return f"{out['passed']} passed, {out['failed']} failed"
