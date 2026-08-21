"""Cursor Agent. Verified against 2026.04.17-787b533 on macOS.

**The constrained case, and it resolves through the worktree.** `cursor-agent` has no
per-invocation MCP config flag — checked, not assumed: its `--help` offers `-p/--print`,
`--force`, `--approve-mcps`, `--trust` and an `mcp` subcommand, and nothing that names a
config path. So it reads `.cursor/mcp.json` from the project directory.

Two children sharing one config would redeem the same seat and the second would fail on
single-use. But each child's worktree IS its own project directory, so the file is
already per-child. The catch is that `.cursor/` is a tracked path in this repository, so
the supervisor is writing a live credential into a tracked location — which is why
`.cursor/mcp.json` is in `worktree.SEAT_FILES` and salvage excludes and verifies it.

Its version is CalVer with a git hash (`2026.04.17-787b533`), so a semver range would be
meaningless here; the range below is in its own scheme.
"""

from __future__ import annotations

from pathlib import Path

from ..seat import Seat
from ..spawn import Launch
from ..worktree import Worktree
from . import Adapter, Support
from .claude import POINTER


class CursorAgent(Adapter):
    name = "cursor-agent"
    binary = "cursor-agent"
    support = Support(minimum=(2026, 1), maximum=(2027, 1), verified_against="2026.04.17-787b533")
    notes = (
        "No per-invocation MCP config flag, so the seat lands in .cursor/mcp.json inside "
        "the worktree — a tracked path in this repo, which is why it is in SEAT_FILES. "
        "CalVer, not semver."
    )

    def seat_path(self, worktree: Path) -> Path:
        return Path(worktree) / ".cursor" / "mcp.json"

    def launch(
        self, seat: Seat, tree: Worktree, instruction_file: Path, binary: Path
    ) -> Launch:
        return Launch(
            adapter=self.name,
            argv=[
                str(binary),
                "--print",
                "--force",         # nobody is there to approve a command
                "--approve-mcps",  # ...or to approve the seat we just handed it
                "--trust",         # ...or to trust the worktree it is standing in
                POINTER,
            ],
            seat_path=self.seat_path(tree.path),
            config=seat.mcp_config(),
            instruction="",
            stdin_file=instruction_file,
        )
