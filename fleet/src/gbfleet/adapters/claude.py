"""Claude Code. Verified against 2.1.233 on macOS.

The only one of the three that takes an MCP config path per invocation, which means its
seat file can live OUTSIDE the worktree — a private temp file, never in the project
directory, never anywhere git could see it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..seat import Seat
from ..spawn import Launch
from ..worktree import Worktree
from . import Adapter, Support, Tuning

#: What goes on argv. Deliberately says nothing and carries nothing: the real
#: instruction, including the enrolment code, arrives on stdin.
#:
#: `cat file | claude -p 'prompt'` is the documented headless shape — stdin is content,
#: argv is the prompt — so this is the prompt and the seat never appears in `ps`.
POINTER = (
    "Follow the instructions on standard input exactly. They tell you how to register, "
    "what to claim, and when to exit."
)


class ClaudeCode(Adapter):
    name = "claude"
    binary = "claude"
    support = Support(minimum=(2, 0), maximum=(3, 0), verified_against="2.1.233")
    notes = (
        "Takes --mcp-config, so the seat file stays out of the worktree entirely. "
        "Prompt on stdin; --dangerously-skip-permissions is required for a headless "
        "run and is why the worktree boundary matters (PRD-22 D-k)."
    )

    def seat_path(self, worktree: Path) -> Path:
        # Not under the worktree: nothing forces it here, and a credential that never
        # enters the project directory cannot be committed by salvage, cannot be seen
        # by `git status`, and needs no entry in SEAT_FILES.
        handle = tempfile.NamedTemporaryFile(
            prefix="gbfleet-claude-", suffix=".json", delete=False
        )
        handle.close()
        return Path(handle.name)

    tuning = frozenset({"fallback_model"})

    def tuning_argv(self, tuning: Tuning) -> list[str]:
        """`--fallback-model`, the one knob that matters most for an UNATTENDED child.

        > "Enable automatic fallback to specified model(s) when the default model is
        > overloaded or not available. Accepts a comma-separated list to try each in
        > order. Re-tries the primary at the start..."

        On an interactive session an overloaded model is a wait. On a spawned child it is a
        dead registration window: the process starts, cannot get a model, never registers,
        and `await_registration` kills it — after which the supervisor reports the ADAPTER
        as broken, which is precisely the misattribution this package exists to avoid.

        Fallback converts that failure into a slower child. No other vendor here offers it.

        Unvalidated, for the same reason `--model` is: this CLI has no listing flag, so
        there is nothing to check a name against.
        """
        return ["--fallback-model", tuning.fallback_model] if tuning.fallback_model else []

    # No listing flag exists on this CLI, so a named model is passed through UNCHECKED and
    # the support matrix says so. Inherits `known_models` -> None deliberately rather than
    # returning an empty set, which would refuse every model instead of checking none.

    def debug_argv(self, path: Path) -> list[str]:
        """`--debug-file <path>`, which `--help` says "implicitly enables debug mode".

        So `--debug` is NOT also passed: it takes an optional category filter, and a bare
        `-d` sitting next to a positional would be one more chance for the filter to
        swallow something it should not.
        """
        return ["--debug-file", str(path)]

    def launch(
        self, seat: Seat, tree: Worktree, instruction_file: Path, binary: Path,
        model: str = "", tuning: Tuning | None = None, *,
        debug_file: Path | None = None,
    ) -> Launch:
        seat_file = self.seat_path(tree.path)
        return Launch(
            adapter=self.name,
            model=model,
            argv=[
                str(binary),
                "--print",
                # Before POINTER, which is positional: a flag after it is prompt text.
                *(self.debug_argv(debug_file) if debug_file else []),
                "--mcp-config", str(seat_file),
                *self.model_argv(model),
                *self.tuning_argv(tuning or Tuning()),
                # Headless means nobody is there to answer a permission prompt. PRD-22's
                # risk table names this and answers it with the worktree boundary plus
                # per-child config — explicitly NOT a sandbox (D-k, §7).
                "--dangerously-skip-permissions",
                POINTER,
            ],
            seat_path=seat_file,
            debug_path=debug_file,
            config=seat.mcp_config(),
            instruction="",
            stdin_file=instruction_file,
        )
