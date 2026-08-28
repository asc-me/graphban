"""`gbagent` — the first adapter that is ours (PRD-24 D8, S5).

Everything the other three adapters work around, this one simply does, and the reason is
ownership rather than cleverness:

- **The seat never enters the worktree.** Like `claude` it takes a config path, so the
  credential lives in a private temp file that git cannot see and salvage cannot commit.
  Unlike `claude`, that is not a lucky flag — we chose it.
- **The instruction reaches it by path**, so the enrolment code never appears on argv where
  every `ps` on the machine can read it, and never needs a stdin pipe.
- **The model can be checked before spawning.** `gbagent models` asks the configured endpoint
  what it serves. GRPH-485 was exactly this failure the long way round: a model name that did
  not exist, discovered as a grill that would not converge.

**The version is an exact pin, not a range.** The range machinery exists because three vendors
ship on their own schedules and we learn about it afterwards. `gbagent` is the same wheel as
the supervisor, so the only version mismatch that can happen is a `gbagent` on PATH from a
DIFFERENT install — and a range would accept exactly that. The pin is read from
`gbfleet.__version__` rather than written out, because a literal would refuse the next release
the moment somebody bumped one file and not the other.

**It gets no special handling in `resolve`.** G4: deleting this adapter must change nothing
about how the fleet is arbitrated. Being first-party buys better flags, not authority.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import gbfleet

from ..seat import Seat
from ..spawn import Launch
from ..worktree import Worktree
from . import Adapter, Support, Tuning, parse_version

#: Where `gbagent` looks for the model endpoint. Read here only to decide whether the model
#: CAN be checked; the child reads it itself.
BASE_URL_ENV = "GBAGENT_BASE_URL"


class GbAgent(Adapter):
    name = "gbagent"
    binary = "gbagent"
    support = Support(
        exact=parse_version(gbfleet.__version__),
        # Not derived from a claim somebody typed: `test_adapters.py` resolves this binary
        # for real on every CI run, so unlike a vendor row this one is re-verified
        # continuously rather than observed once on somebody's laptop.
        verified_against=gbfleet.__version__,
    )
    notes = (
        "First-party (PRD-24). Exact version pin rather than a range, because it ships in "
        "this same distribution and the only mismatch possible is a binary from another "
        "install. Seat stays out of the worktree; instruction arrives by path, never argv. "
        "Exits 75 when it gives up, which is distinct from a crash."
    )

    tuning = frozenset({"turns", "window"})

    def seat_path(self, worktree: Path) -> Path:
        # Outside the worktree, for the reason `claude` is: a credential that never enters
        # the project directory cannot be committed by salvage, cannot show up in
        # `git status`, and needs no entry in `worktree.SEAT_FILES`.
        handle = tempfile.NamedTemporaryFile(
            prefix="gbfleet-gbagent-", suffix=".json", delete=False
        )
        handle.close()
        return Path(handle.name)

    def tuning_argv(self, tuning: Tuning) -> list[str]:
        """`--turns` and `--window`, both of which the loop refuses to guess.

        Neither has a default in `loop.run` and neither gets one here. The window especially:
        assume too large and a run dies of an overflow compaction could have prevented, assume
        too small and it compacts constantly and throws away the 262k that made a local model
        worth using. A default silently picks one of those.
        """
        argv: list[str] = []
        if tuning.turns:
            argv += ["--turns", str(tuning.turns)]
        if tuning.window:
            argv += ["--window", str(tuning.window)]
        return argv

    def known_models(self, binary: Path) -> frozenset[str] | None:
        """`gbagent models`, which asks the configured endpoint what it actually serves.

        **None when there is no endpoint configured**, which is not the same answer as an
        empty set — the distinction the base class exists to keep. Without `GBAGENT_BASE_URL`
        there is nothing to ask, and refusing every model because we could not look would
        break a working setup over a missing environment variable.

        Shelled out rather than fetched here on purpose: `test_client.py` pins the two modules
        in this package that may open a socket, and an adapter is not one of them. The binary
        does the HTTP through the door that is allowed to.
        """
        if not os.environ.get(BASE_URL_ENV):
            return None
        from . import _run_version

        out = _run_version(binary, ("models",))
        names = {line.strip() for line in out.splitlines() if line.strip()
                 and not line.startswith(("#", " ", "\t"))}
        return frozenset(names) or None

    def debug_argv(self, path: Path) -> list[str]:
        """Nothing yet, and this one is ours.

        `gbagent run` takes no debug or verbose flag. Unlike the vendor CLIs that is
        fixable rather than a fact of life — it has a per-turn trace already (GRPH-506),
        it simply has no way to be told to write it somewhere. Left as [] rather than
        half-wired, so the support matrix does not claim a capability that is not there.
        """
        return []

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
                str(binary), "run",
                "--worktree", str(tree.path),
                "--mcp-config", str(seat_file),
                # By PATH, not on argv: the instruction carries the enrolment code, and argv
                # is readable by every process on this machine.
                "--instruction-file", str(instruction_file),
                # `await_registration` matches on worktree, and D-g is one worker one
                # worktree; the branch goes with it so the roster row names the diff.
                "--branch", tree.branch,
                *self.model_argv(model),
                *self.tuning_argv(tuning or Tuning()),
            ],
            seat_path=seat_file,
            config=seat.mcp_config(),
            instruction="",
        )

    def exit_meaning(self, code: int) -> str:
        """AC-7's other half: the supervisor tells surrender from failure without reading
        stderr. The words come from `gbagent.loop`, so there is one definition of what 75
        means rather than two that can drift."""
        from gbagent.loop import exit_meaning

        return exit_meaning(code)
