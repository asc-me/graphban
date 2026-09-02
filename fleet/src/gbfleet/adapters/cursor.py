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
from . import Adapter, Support, Tuning
from .claude import POINTER
from .cursor_stream import touched as stream_touched_paths


class CursorAgent(Adapter):
    name = "cursor-agent"
    binary = "cursor-agent"
    support = Support(minimum=(2026, 1), maximum=(2027, 1), verified_against="2026.04.17-787b533")
    notes = (
        "No per-invocation MCP config flag, so the seat lands in .cursor/mcp.json inside "
        "the worktree — a tracked path in this repo, which is why it is in SEAT_FILES. "
        "CalVer, not semver."
    )

    # No fallback list, no effort flag: `tuning` stays empty and `resolve` refuses either
    # by name rather than accepting a setting this binary would ignore.

    def known_models(self, binary: Path) -> frozenset[str] | None:
        """`cursor-agent --list-models`, or None when the account cannot enumerate.

        Measured 2026-08-24: an account with no entitlements answers "No models available
        for this account". That is a statement about the ACCOUNT, not about the model
        being asked for, and treating it as an empty listing would refuse every spawn on
        a setup that works. So anything that does not parse as a model list returns None
        and the model is passed through unchecked.
        """
        from . import _run_version

        out = _run_version(binary, ("--list-models",))
        names = {
            line.strip().lstrip("*-").strip().split()[0]
            for line in out.splitlines()
            if line.strip() and not line.strip().lower().startswith("no models")
        }
        names = {n for n in names if n and not n.endswith(":")}
        return frozenset(names) or None

    def seat_path(self, worktree: Path) -> Path:
        return Path(worktree) / ".cursor" / "mcp.json"

    def debug_argv(self, path: Path) -> list[str]:
        """Nothing. `cursor-agent --help` has no debug and no verbose flag.

        It does have `--output-format stream-json` and `--stream-partial-output`, which
        give structured progress rather than a debug log — a different feature, reached a
        different way, and not something to quietly substitute here. Declaring one would
        be the fabrication `codex.py` refuses to make.

        A Cursor child under `--debug` therefore gets the output sampling every child
        gets, and nothing more. The supervisor reports that by name rather than letting
        the operator assume otherwise.
        """
        return []

    def stream_touched(self, text: str) -> list[str]:
        """Write-tool paths from `--output-format stream-json`. Reads are not writes."""
        return stream_touched_paths(text)

    def launch(
        self, seat: Seat, tree: Worktree, instruction_file: Path, binary: Path,
        model: str = "", tuning: Tuning | None = None, *,
        debug_file: Path | None = None,
    ) -> Launch:
        return Launch(
            adapter=self.name,
            model=model,
            argv=[
                str(binary),
                "--print",
                *self.model_argv(model),
                "--force",         # nobody is there to approve a command
                "--approve-mcps",  # ...or to approve the seat we just handed it
                "--trust",         # ...or to trust the worktree it is standing in
                # GRPH-215 phase 1: structured writes, not a debug log. Must sit
                # before POINTER — a flag after it is prompt text.
                "--output-format", "stream-json",
                POINTER,
            ],
            seat_path=self.seat_path(tree.path),
            config=seat.mcp_config(),
            instruction="",
            stdin_file=instruction_file,
        )
