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
from . import Adapter, Support

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

    def launch(
        self, seat: Seat, tree: Worktree, instruction_file: Path, binary: Path
    ) -> Launch:
        seat_file = self.seat_path(tree.path)
        return Launch(
            adapter=self.name,
            argv=[
                str(binary),
                "--print",
                "--mcp-config", str(seat_file),
                # Headless means nobody is there to answer a permission prompt. PRD-22's
                # risk table names this and answers it with the worktree boundary plus
                # per-child config — explicitly NOT a sandbox (D-k, §7).
                "--dangerously-skip-permissions",
                POINTER,
            ],
            seat_path=seat_file,
            config=seat.mcp_config(),
            instruction="",
            stdin_file=instruction_file,
        )
