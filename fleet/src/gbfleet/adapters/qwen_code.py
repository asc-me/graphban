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

**A named model is passed through UNCHECKED and may be silently replaced — but the
substitution is not invisible.** There is still no listing flag, so nothing can be validated
BEFORE a spawn: `-m bogus-model-name` starts a run and no warning is printed anywhere.

What that paragraph used to conclude — that a matrix row naming a model is a claim nothing
could ever check — was wrong, and wrong in the direction this repo keeps getting wrong: an
absence of warning read as an absence of information. The init record already quoted above
for the server allowlist carries `"model"`, and it reports the model that ANSWERED rather than
an echo of argv. Measured 2026-09-28, same binary, two runs:

    qwen -m qwen3.8-max                 ->  init.model = "qwen3.8-max"
    qwen -m definitely-not-a-model-zzz  ->  init.model = "qwen3.7-plus"

Children are already launched with `-o json` (see `launch`), and their stdout is already kept
in `stdout.log` — so every qwen child in every wave has been announcing its effective model,
and its `result.usage` token counts, into a file nothing parses. Reading it is GRPH-982.

Until that lands the pre-spawn answer stays "unchecked": `known_models` returns None, a wrong
name is not refused, and a matrix row for a named model stays `unverified` because no attempt
has been ATTRIBUTED to a model even though every attempt recorded one.

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

    # No listing flag, so `known_models` stays None ("cannot be asked") and a wrong name is
    # not refused before the spawn. It is legible AFTER one: the child's `-o json` init event
    # names the model that answered, and a substituted name shows up there as the default
    # rather than as what was asked for. See the docstring; the reader is GRPH-982.

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
                # The allowlist has to name the SHARED servers too, or the child is handed a
                # config it is then refused (GRPH-816). The seat file is the grant; this flag
                # is qwen's way of enforcing it, and the two must agree or the operator gets a
                # docs server that is present and unusable.
                "--allowed-mcp-server-names", ",".join([SERVER, *sorted(seat.shared or {})]),
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
