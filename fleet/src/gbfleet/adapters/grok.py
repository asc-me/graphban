"""Grok CLI (`grok`, whose `--help` header calls it "Grok Build").

Verified against 1.0.5 on macOS, and re-measured against 1.0.5 on Windows 11 for
GRPH-575 — where the per-child seat question was settled and the answer turned out to
be the opposite of what this file used to say.

`--prompt-file <PATH>` is a first-class flag, so the enrolment code reaches the child
as a file path and needs neither argv nor a stdin pipe.

**Per-child seats work, via two facts that must both hold.**

1. `--scope project` config is `./.grok/config.toml` — per-directory, so each child's
   worktree carries its own. It is TOML with `mcp_servers` (snake_case), and grok reads
   no JSON at all. The previous version of this adapter wrote `.grok/mcp.json`, a name
   grok never looks for; the seat was discarded in silence.

2. A repo-local server is gated on **folder trust**. In a fresh worktree, `grok mcp
   doctor` reports `folder untrusted (repo-local (project-scoped) server not started
   for an untrusted folder)` — while `grok inspect` cheerfully lists the server as
   loaded. So the child starts with no tools and no error, and the inspector agrees
   with it. Launching with `--trust` records the decision in
   `~/.grok/trusted_folders.toml` and the server starts.

`--trust` is absent from `--help` but accepted, and is the mechanism grok's own bundled
docs name (`~/.grok/docs/user-guide/10-hooks.md`: trust is granted by `/hooks-trust`
"or launching with `--trust`", recorded in the folder-trust store, "the same gate that
governs repo-local MCP/LSP servers"). Being undocumented in `--help` is why `support`
pins the verified version: if a future grok drops it, the seat goes quiet again.

One thing the supervisor must not forget: a grok child whose MCP server fails to
connect **still runs to completion normally**. Measured — with a deliberately invalid
key the process answered its prompt and exited 0. A broken seat is not a crash; it is
an expensive silence, which is what `registration_latency` exists to catch.
"""

from __future__ import annotations

from pathlib import Path

from .. import seat as seat_mod
from ..seat import Seat
from ..spawn import Launch
from ..worktree import Worktree
from . import Adapter, Support, Tuning


class Grok(Adapter):
    name = "grok"
    binary = "grok"
    support = Support(minimum=(1, 0), maximum=(2, 0), verified_against="1.0.5")
    notes = (
        "--prompt-file takes the instruction by path, so nothing sensitive touches argv "
        "or stdin. Per-child seat is project-scoped .grok/config.toml (TOML, not JSON) "
        "and needs --trust: an untrusted folder starts no repo-local server and says "
        "nothing about it. A failed seat does not fail the run."
    )

    seat_format = seat_mod.TOML

    def seat_path(self, worktree: Path) -> Path:
        """`<worktree>/.grok/config.toml` — grok's own project scope.

        Not a guess and not `.grok/mcp.json`: `grok mcp add --help` names the two
        scopes as `user (~/.grok/config.toml)` and `project (./.grok/config.toml)`, and
        running it with `--scope project` writes exactly this path. Because the scope is
        the *directory*, each child's worktree is its own project and the seats do not
        race — which is the whole reason the fleet model works for this vendor.

        In the worktree, so `worktree.SEAT_FILES` must know it or salvage commits a live
        credential.
        """
        return Path(worktree) / ".grok" / "config.toml"

    tuning = frozenset({"effort"})

    def tuning_argv(self, tuning: Tuning) -> list[str]:
        """`--reasoning-effort` (alias `--effort`), passed through UNVALIDATED.

        Measured 2026-08-24 rather than assumed: `--help` says only "Reasoning effort for
        reasoning models" and enumerates no values, and `grok --reasoning-effort
        bogus-value models` is accepted without complaint — so the CLI does not validate it
        as an enum and the accepted set is not discoverable from the binary.

        Declaring a list here would be exactly the fabrication `codex.py` refuses to make.
        So it passes through, and the support matrix says it is unchecked.
        """
        return ["--reasoning-effort", tuning.effort] if tuning.effort else []

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

    def debug_argv(self, path: Path) -> list[str]:
        """`--debug` plus `--debug-file <FILE>`, both in `--help` on 1.0.5.

        Both are passed. `--debug-file` alone is not documented to imply `--debug` the
        way claude's does, and asking for a file without turning logging on is the sort
        of thing that produces an empty file and a confident operator.
        """
        return ["--debug", "--debug-file", str(path)]

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
                # Without this the worktree is untrusted, the project-scoped MCP server
                # is never started, and the child runs to completion with no tools and
                # no complaint. It is the difference between a worker and an expense.
                "--trust",
                *(self.debug_argv(debug_file) if debug_file else []),
                "--prompt-file", str(instruction_file),
                "--cwd", str(tree.path),
                *self.model_argv(model),
                *self.tuning_argv(tuning or Tuning()),
            ],
            seat_path=self.seat_path(tree.path),
            seat_format=self.seat_format,
            debug_path=debug_file,
            config=seat.mcp_config(),
            instruction="",
        )
