"""Qwen Code (`qwen`). Verified against 0.23.0 on macOS, 2026-09-05, by running it.

Headless shape: the instruction arrives on **stdin** (`--help`: the positional prompt "is
appended to input on stdin", and stdin alone runs one-shot — measured, the child answered
and exited 0 with `-o json`). Nothing carrying the seat touches argv.

**Per-child seat, via two facts that must both hold, both measured against the live server.**

1. `--mcp-config <path>` takes a file per invocation, so the seat lives OUTSIDE the worktree
   like claude's. The server entry must be spelled `httpUrl`: the seat's vendor-neutral
   `{"type": "http", "url": ...}` shape is accepted without complaint and never connects
   (`mcp_servers: disconnected`, zero tools), while `{"httpUrl": ...}` with the same headers
   connected and listed 57 `mcp__<name>__*` tools. `mcp_config()` is rewritten here.
2. `--allowed-mcp-server-names <name>` confines the child to the seat's server. Without it
   the operator's own `~/.qwen/settings.json` servers load too — a different credential, a
   different identity, in the child's tool list. Measured: with the allowlist the init
   record names exactly one server.

`--bare` is NOT the answer to (2): it also drops the model-provider config, and the child
dies at once with `No auth type is selected` (exit 1, `error_during_execution`).

**A named model is passed through UNCHECKED, and may be silently replaced.** There is no
listing flag, and `-m bogus-model-name` ran `qwen3.7-plus` — the configured default — with no
warning anywhere. So a matrix row for this vendor with a model name is a claim the binary
will not enforce; the row ships with no model (the vendor default) until a walk proves one.

Exit codes measured: 0 normal; 55 `FatalBudgetExceededError` when `--max-wall-time` or
`--max-tool-calls` is exceeded (the JSON tail names which); 1 when the run could not start.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..seat import Seat
from ..spawn import Launch
from ..worktree import Worktree
from . import Adapter, Support, Tuning, default_exit_meaning

#: The server name inside the seat file, and the one name the allowlist admits.
SERVER = "graphban"


class QwenCode(Adapter):
    name = "qwen-code"
    binary = "qwen"
    support = Support(minimum=(0, 23), maximum=(1, 0), verified_against="0.23.0")
    notes = (
        "Instruction on stdin; --mcp-config keeps the seat out of the worktree but the "
        "entry must be `httpUrl` (the generic url shape silently never connects); "
        "--allowed-mcp-server-names keeps the operator's own servers out of the child. "
        "-m is unchecked and an unknown name is silently replaced by the configured default. "
        "Exit 55 = wall-time or tool-call budget exceeded."
    )

    def seat_path(self, worktree: Path) -> Path:
        # Outside the worktree: --mcp-config takes a path, so the credential never enters
        # the project directory and needs no SEAT_FILES entry.
        handle = tempfile.NamedTemporaryFile(prefix="gbfleet-qwen-", suffix=".json", delete=False)
        handle.close()
        return Path(handle.name)

    tuning = frozenset({"turns"})

    def tuning_argv(self, tuning: Tuning) -> list[str]:
        """`--max-session-turns <n>`: the same budget gbagent spells `--turns`, and the one
        knob an UNATTENDED child of this vendor needs — without it a stuck loop runs until
        the supervisor's wall clock kills it."""
        return ["--max-session-turns", tuning.turns] if tuning.turns else []

    def model_argv(self, model: str) -> list[str]:
        return ["-m", model] if model else []

    # No listing flag; inherits known_models -> None ("cannot be asked"), and the docstring
    # above records that even a wrong name is not refused by the binary.

    def debug_argv(self, path: Path) -> list[str]:
        """`-d` exists but writes to stderr with no file flag; a path cannot be honoured, so
        this says "cannot" rather than pretend."""
        return []

    def seat_config(self, seat: Seat) -> dict:
        """The seat rewritten in the one shape this vendor connects with (measured)."""
        core = seat.mcp_config(SERVER)["mcpServers"][SERVER]
        return {"mcpServers": {SERVER: {"httpUrl": core["url"], "headers": dict(core["headers"])}}}

    def exit_meaning(self, code: int) -> str:
        if code == 55:
            return "budget exceeded (--max-wall-time or --max-tool-calls; the JSON tail says which)"
        if code == 1:
            return "could not run (exit 1: no auth type, bad flag, or an error before the first turn)"
        return default_exit_meaning(code)

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
                "--mcp-config", str(seat_file),
                "--allowed-mcp-server-names", SERVER,
                # Headless: nobody answers a permission prompt. Same posture as claude's
                # --dangerously-skip-permissions, answered by the worktree boundary (PRD-22 D-k).
                "--approval-mode", "yolo",
                "-o", "json",
                *self.model_argv(model),
                *self.tuning_argv(tuning or Tuning()),
            ],
            seat_path=seat_file,
            debug_path=debug_file,
            config=self.seat_config(seat),
            instruction="",
            stdin_file=instruction_file,
        )
