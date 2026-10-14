"""Antigravity CLI (`agy`). Verified against 1.3.5 on macOS, 2026-10-13, by running it.

Google's terminal agent (the binary's `--help` header is just `Usage of agy:`). Headless
shape is **stdin without `--print`**. `--print` is a flag that *takes the next argv token as
the prompt* — measured, `agy --print --output-format json …` exits 2 with `Error: --print
took "--output-format" as its prompt`. `--print` with no argument is also refused
(`flag needs an argument: -print`). Piping the instruction on stdin and omitting `--print`
runs one shot, prints JSON, and exits 0. Putting the enrolment code on `--print=…` would
publish it to every `ps` on the machine.

**Per-child seat is the worktree.** There is no `--mcp-config` flag (checked: `--help` has
none, and `strings` on the binary has no `--mcp`). `agy mcp add` writes the *global* file
`~/.gemini/config/mcp_config.json`. Workspace servers live at `.agents/mcp_config.json`
(Antigravity's own docs, and measured: a Graphban HTTP server written there was callable
from `--output-format json` print mode; `list_projects` came back). Each child's worktree
is its own project directory, so the file is already per-child. It is a live credential
inside the tree, which is why it is in `worktree.SEAT_FILES`.

**The JSON shape is not the vendor-neutral one.** `seat.mcp_config()` writes
`{"type":"http","url":…,"headers":…}`. This binary's `mcp add` wrote, and the docs
require, `serverUrl` — "Legacy fields like `url` or `httpUrl` aren't supported." The
adapter rewrites. `disabled: false` is what `mcp add` stamps; left off it still parsed
in the probe, but the rewrite matches the file the CLI writes.

**No confinement flag.** `claude` has `--strict-mcp-config`, `qwen-code` has
`--allowed-mcp-server-names`. This CLI has neither on 1.3.5. The workspace file is the
seat; `~/.gemini/config/mcp_config.json` and plugins still load. A child may hold tools
nobody in gbfleet granted. The seat still wins a name collision on `graphban` because it
is written into the worktree the child is standing in.

**Models and effort are checked by the binary, and we check them first.** `agy --output-format
json models` returns `command.data.models[].id`. `--model definitely-not-a-model-zzz` exits
before a turn with `is not recognized as a known model`. `--effort bogus` names the valid
set: `low, medium, high, xhigh, max`. Refusing at `resolve` / `check_tuning` is the same
reason as everywhere else: a child that starts, errors, and never registers looks like a
broken adapter.

`--dangerously-skip-permissions` is required for a headless run (init.permission_mode
became `always-proceed`). `--output-format json` is how usage is read after reap.
`--log-file` is the debug flag. `--sandbox` *enables* a sandbox and is not passed; a
spawned child inherits whatever the parent already has.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..seat import Seat
from ..spawn import Launch
from ..worktree import Worktree
from . import Adapter, AdapterError, Support, Tuning

#: What `agy --help` enumerates for `--effort`, and what `--effort bogus` quoted back.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class Agy(Adapter):
    name = "agy"
    binary = "agy"
    support = Support(minimum=(1, 3), maximum=(2, 0), verified_against="1.3.5")
    notes = (
        "Antigravity CLI. Instruction on stdin — do not pass --print, it swallows the next "
        "argv token as the prompt. Seat is .agents/mcp_config.json inside the worktree, "
        "rewritten to serverUrl (url/httpUrl are not supported). No allowlist flag, so the "
        "operator's ~/.gemini/config/mcp_config.json still loads. --effort is "
        "low|medium|high|xhigh|max; --model is checked against `agy models`."
    )

    tuning = frozenset({"effort"})

    def seat_path(self, worktree: Path) -> Path:
        return Path(worktree) / ".agents" / "mcp_config.json"

    def model_argv(self, model: str) -> list[str]:
        return ["--model", model] if model else []

    def tuning_argv(self, tuning: Tuning) -> list[str]:
        return ["--effort", tuning.effort] if tuning.effort else []

    def check_tuning(self, tuning: Tuning) -> None:
        """Refuse a value the binary will refuse after the process starts.

        Measured: `--effort bogus` exits before a turn with `valid: low, medium, high,
        xhigh, max`. That is a dead registration window if we let it through.
        """
        if tuning.effort and tuning.effort not in EFFORTS:
            raise AdapterError(
                f"adapter 'agy' --effort {tuning.effort!r} is not one of "
                f"{', '.join(EFFORTS)}."
            )

    def known_models(self, binary: Path) -> frozenset[str] | None:
        """`agy --output-format json models` → `command.data.models[].id`.

        `--output-format` is a global flag and must precede the subcommand; `agy models
        --output-format json` is `flags provided but not defined: -output-format`.
        An unparseable or empty listing is None, not an empty set — same reason as
        cursor-agent's 'No models available for this account'.
        """
        from . import _run_version

        out = _run_version(binary, ("--output-format", "json", "models"))
        start = (out or "").find("{")
        if start < 0:
            return None
        try:
            data = json.loads(out[start:])
        except ValueError:
            return None
        models = ((data.get("command") or {}).get("data") or {}).get("models") or []
        names = {
            str(entry["id"])
            for entry in models
            if isinstance(entry, dict) and entry.get("id")
        }
        return frozenset(names) or None

    def debug_argv(self, path: Path) -> list[str]:
        """`--log-file`, in `--help` on 1.3.5. Implies nothing else."""
        return ["--log-file", str(path)]

    def seat_config(self, seat: Seat) -> dict:
        """Rewrite the vendor-neutral `url` shape into `serverUrl` (measured)."""
        servers = {}
        for name, stanza in (seat.mcp_config().get("mcpServers") or {}).items():
            if isinstance(stanza, dict):
                servers[name] = self._stanza(stanza)
        return {"mcpServers": servers}

    @staticmethod
    def _stanza(stanza: dict) -> dict:
        url = stanza.get("serverUrl") or stanza.get("url") or stanza.get("httpUrl")
        if url:
            out: dict = {"disabled": False, "serverUrl": url}
            headers = stanza.get("headers")
            if isinstance(headers, dict) and headers:
                out["headers"] = dict(headers)
            return out
        out = {"disabled": False}
        for key in ("command", "args", "env", "cwd"):
            if key in stanza:
                out[key] = stanza[key]
        return out

    @staticmethod
    def result_facts(stdout: str) -> dict:
        """Read agy's `--output-format json` object (measured 1.3.5).

        One JSON object, not a stream. `usage.input_tokens` / `output_tokens` and
        `num_turns` were present on a successful print-mode run. The object does not
        name the model that answered, so this does not guess one.
        """
        text = (stdout or "").strip()
        start = text.find("{")
        if start < 0:
            return {}
        try:
            data = json.loads(text[start:])
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict = {}
        usage = data.get("usage")
        if isinstance(usage, dict):
            for key, field in (("input_tokens", "tokens_in"),
                               ("output_tokens", "tokens_out")):
                if isinstance(usage.get(key), (int, float)):
                    out[field] = int(usage[key])
        if isinstance(data.get("num_turns"), int):
            out["turns_used"] = data["num_turns"]
        return out

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
                "--output-format", "json",
                # Headless: nobody answers a permission prompt. Same posture as claude.
                "--dangerously-skip-permissions",
                *(self.debug_argv(debug_file) if debug_file else []),
                *self.model_argv(model),
                *self.tuning_argv(tuning or Tuning()),
            ],
            seat_path=self.seat_path(tree.path),
            debug_path=debug_file,
            config=self.seat_config(seat),
            instruction="",
            stdin_file=instruction_file,
        )
