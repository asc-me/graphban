"""Grok CLI. Verified against 1.0.5 on macOS.

The tidiest of the three on the one thing that matters most here: `--prompt-file <PATH>`
is a first-class flag, so the enrolment code reaches the child as a file path and needs
neither argv nor a stdin pipe.

Its MCP configuration lives in `~/.grok/config.toml` and no per-invocation config flag
was found in `--help`. That is a real gap rather than a solved problem: a per-child seat
cannot be delivered through a shared user-level config without two children racing for
one file. Recorded in the support matrix as unresolved rather than papered over.
"""

from __future__ import annotations

from pathlib import Path

from ..seat import Seat
from ..spawn import Launch
from ..worktree import Worktree
from . import Adapter, Support


class Grok(Adapter):
    name = "grok"
    binary = "grok"
    support = Support(minimum=(1, 0), maximum=(2, 0), verified_against="1.0.5")
    notes = (
        "--prompt-file takes the instruction by path, so nothing sensitive touches argv "
        "or stdin. MCP config is user-level (~/.grok/config.toml) with no per-invocation "
        "flag found, so per-child seat delivery is UNRESOLVED — see docs/fleet-adapters.md."
    )

    def seat_path(self, worktree: Path) -> Path:
        # Namespaced to grok, not borrowed from Cursor. Writing a live seat to
        # `.cursor/mcp.json` for a grok child would be a file that looks like it belongs
        # to a vendor that is not running — the next reader would delete it as leftover,
        # or worse, trust it.
        #
        # Written into the worktree so it is at least per-child and salvage knows about
        # it. Whether grok READS it there is the unresolved question in the docstring;
        # the child failing to register is what surfaces that, inside the bounded window.
        return Path(worktree) / ".grok" / "mcp.json"

    def model_argv(self, model: str) -> list[str]:
        """`-m`, not `--model`. Both are accepted by the binary; the short form is what
        `--help` puts first (`-m, --model <MODEL>`) and is what the matrix documents."""
        return ["-m", model] if model else []

    def known_models(self, binary: Path) -> frozenset[str] | None:
        """`grok models`, which really does enumerate — the only one of the three that
        answered on this machine (`grok-4.6` default, `grok-4.5`).

        Parsed from the bullet list rather than the whole output, which also carries a
        login line and a "Default model:" line that are not model names.
        """
        from . import _run_version

        out = _run_version(binary, ("models",))
        names = set()
        for line in out.splitlines():
            bare = line.strip()
            if not bare.startswith(("*", "-")):
                continue
            token = bare.lstrip("*-").strip().split()[0] if bare.lstrip("*-").strip() else ""
            if token:
                names.add(token)
        return frozenset(names) or None

    def launch(
        self, seat: Seat, tree: Worktree, instruction_file: Path, binary: Path,
        model: str = "",
    ) -> Launch:
        return Launch(
            adapter=self.name,
            model=model,
            argv=[
                str(binary),
                "--prompt-file", str(instruction_file),
                "--cwd", str(tree.path),
                *self.model_argv(model),
            ],
            seat_path=self.seat_path(tree.path),
            config=seat.mcp_config(),
            instruction="",
        )
