"""DeepSeek Harness (`dsh`). Verified against 0.2.0-rc.2 on macOS, 2026-10-14, by running it.

The desktop app's CLI is a shell wrapper around Electron (`ELECTRON_RUN_AS_NODE=1`).
`--version` prints `0.2.0-rc.2`. It is **not** on PATH after a desktop install; the binary
lives at `DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`. `--binary`
overrides, same as every other adapter.

Headless shape: `dsh --profile headless`. The task is a positional, or stdin when the
positional is omitted (measured: empty stdin is `a task is required`; `--help` names the
pipe). Putting the enrolment code on argv would publish it to every `ps`. `--print` is
`unknown option`. `--json` writes newline-delimited events on stdout.

**There is no `--mcp-config`.** Measured: `unknown option '--mcp-config'`. MCP is a Cordis
plugin, one server per `@deepseek-ai/dsh-mcp-client` entry, via `--patch <path>` (a
launcher flag, path anywhere on disk — official memory-MCP docs say so, and dump-config
composed an overlay from `/tmp`). The overlay is YAML:

    - insert:
      - id: mcp-graphban
        name: '@deepseek-ai/dsh-mcp-client'
        config:
          serverName: graphban
          transport: streamable-http
          url: http://…
          headers:
            X-API-Key: …
          failOnStartupError: true

`url` is the field that dump-config echoed back. `failOnStartupError: true` still does not
abort the process: a dead endpoint printed `dsh: warning: 1 entry did not activate` and
continued. A broken seat is an expensive silence, same as grok.

`--patch` ADDS. `~/.dsh/profiles/headless/cordis.patch.yml` and `~/.dsh/cordis.patch.yml`
still load. No allowlist flag exists. CONFINEMENT is None.

**The seat is a private temp YAML file**, not in the worktree.

**`--model` is `unknown option`.** `dsh models` boots a profile named `models`. Model is
`agent-default-model` config. Headless dump-default-config says `provider: deepseek-official`
`model: deepseek-flash`. A live `--json` run with that default exited 1
`MISSING_CREDENTIAL` for `deepseek-official` (wants `DEEPSEEK_API_KEY`). The desktop
profile that the logged-in install actually uses is `provider: deepseek-account` /
`model: deepseek-flash`. Overlaying that, the same prompt exited 0 and printed `pong`.
The adapter always writes that provider, and the named model or `deepseek-flash`.

**Unattended permission.** dump-default-config: `DSH_PERMISSION_MODE` defaults to
`workspace-write`, and approval is `ask` unless the mode is `danger-full-access` (preset
`approval: never`). A fleet child has nobody to answer. Launch sets the env var.

`--json` usage (measured on a successful account-provider run): a `status`/`step_end`
event carries `usage.inputTokens` / `outputTokens` (camelCase). The stream does not name
the model that answered.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .. import seat as seat_mod
from ..seat import Seat
from ..spawn import Launch
from ..worktree import Worktree
from . import Adapter, Support, Tuning

#: dump-default-config on 0.2.0-rc.2, and the desktop profile's model.
DEFAULT_MODEL = "deepseek-flash"
#: The provider the desktop login writes credentials for. Headless default
#: `deepseek-official` failed MISSING_CREDENTIAL without DEEPSEEK_API_KEY.
DEFAULT_PROVIDER = "deepseek-account"

#: dump-default-config: approval is `ask` unless this.
PERMISSION_MODE = "danger-full-access"


class Dsh(Adapter):
    name = "dsh"
    binary = "dsh"
    support = Support(minimum=(0, 2), maximum=(0, 3), verified_against="0.2.0-rc.2")
    notes = (
        "DeepSeek Harness. Instruction on stdin; --profile headless --json --patch <yaml>. "
        "No --mcp-config / --model / --print (all unknown option). Seat is a private Cordis "
        "YAML overlay (streamable-http url + headers). Headless default provider "
        "deepseek-official needs DEEPSEEK_API_KEY; the adapter overlays deepseek-account, "
        "which is what the desktop login uses. DSH_PERMISSION_MODE=danger-full-access or "
        "the child waits on an approval prompt. Desktop app does not put dsh on PATH."
    )
    seat_format = seat_mod.YAML

    def seat_path(self, worktree: Path) -> Path:
        # --patch takes a path, so the credential never enters the project directory.
        handle = tempfile.NamedTemporaryFile(
            prefix="gbfleet-dsh-", suffix=".yml", delete=False
        )
        handle.close()
        return Path(handle.name)

    def model_argv(self, model: str) -> list[str]:
        """No --model flag (measured: unknown option). The overlay carries the name."""
        return []

    def debug_argv(self, path: Path) -> list[str]:
        """Headless --help has no log-file flag."""
        return []

    def seat_config(self, seat: Seat, model: str = "") -> dict:
        """Vendor-neutral mcpServers plus the agent-default-model overlay fields.

        Always sets provider+model: omitting them uses headless `deepseek-official`,
        which on this install exited 1 MISSING_CREDENTIAL. Named model replaces the
        default `deepseek-flash`; the provider stays the desktop-login one.
        """
        config = dict(seat.mcp_config())
        config["provider"] = DEFAULT_PROVIDER
        config["model"] = model or DEFAULT_MODEL
        return config

    @staticmethod
    def result_facts(stdout: str) -> dict:
        """Read dsh `--json` NDJSON (measured 0.2.0-rc.2).

        A successful run's `status`/`step_end` carried
        `usage: {inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, totalTokens}`.
        A MISSING_CREDENTIAL run's step_end had no `usage` key — that must not become
        tokens 0. The stream does not name the model, so this does not guess one.
        `turn` on status events is the turn count.
        """
        tokens_in = tokens_out = 0
        saw_usage = False
        max_turn = 0
        for raw in (stdout or "").splitlines():
            line = raw.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "status":
                turn = event.get("turn")
                if isinstance(turn, int) and turn > max_turn:
                    max_turn = turn
                usage = event.get("usage")
                if isinstance(usage, dict) and event.get("phase") == "step_end":
                    inn = usage.get("inputTokens")
                    out = usage.get("outputTokens")
                    if isinstance(inn, (int, float)) and isinstance(out, (int, float)):
                        tokens_in += int(inn)
                        tokens_out += int(out)
                        saw_usage = True
        facts: dict = {}
        if saw_usage:
            facts["tokens_in"] = tokens_in
            facts["tokens_out"] = tokens_out
        if max_turn:
            facts["turns_used"] = max_turn
        return facts

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
                "--profile", "headless",
                "--patch", str(seat_file),
                "--json",
            ],
            seat_path=seat_file,
            seat_format=self.seat_format,
            debug_path=debug_file,
            config=self.seat_config(seat, model),
            instruction="",
            stdin_file=instruction_file,
            env={"DSH_PERMISSION_MODE": PERMISSION_MODE},
        )
