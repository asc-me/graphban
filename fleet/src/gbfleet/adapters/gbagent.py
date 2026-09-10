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
from . import Adapter, AdapterError, Support, Tuning, parse_version

#: Where `gbagent` looks for the model endpoint. Read here only to decide whether the model
#: CAN be checked; the child reads it itself.
BASE_URL_ENV = "GBAGENT_BASE_URL"


class MissingTuning(AdapterError):
    """A spawn was missing an argument this adapter will not default (GRPH-813)."""


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

    def check_tuning(self, tuning: Tuning) -> None:
        """Refuse a spawn missing what this adapter will not default (GRPH-813).

        SEPARATE FROM `tuning_argv`, and the separation is the point. The first version raised
        from there, which broke every caller that builds a launch to LOOK at it — argv
        inspection is not a spawn, and a rule that fires on both cannot tell the difference.
        This is asked once, by the code that is about to start a process.

        Omitting either used to emit nothing and `gbagent run` then exited 2 on an argparse
        error: a raw usage dump in a child's stderr, before it registered. On the board that is
        not "bad arguments" — it is a delegation reading `expired, nothing claimed`, which
        looks like a dead model rather than a missing flag.

        `AdapterError` is the type `spawn` already catches and reports, so the refusal reaches
        the caller who can fix it rather than the log of a process that is gone.
        """
        missing = [flag for flag, value in (("turns", tuning.turns), ("window", tuning.window))
                   if not value]
        if missing:
            raise MissingTuning(
                f"gbagent needs {' and '.join('--' + m for m in missing)} and refuses to "
                "guess: too large a window dies of an overflow compaction could have "
                "prevented, too small throws away the context that made a local model worth "
                "using. Pass them on spawn."
            )

    @staticmethod
    def result_facts(stdout: str) -> dict:
        """Read gbagent's own result record: one JSON line on stdout, last one wins.

        LAST rather than first: a run that printed a record, was resumed and printed another
        is describing the same attempt twice, and the later one is the one that finished.
        """
        import json as _json

        found: dict = {}
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{") or '"gbagent"' not in line:
                continue
            try:
                payload = _json.loads(line).get("gbagent")
            except ValueError:
                continue
            if isinstance(payload, dict):
                found = payload
        if not found:
            return {}
        # Only what the ledger records, and only when it was actually said. `tokens_in: None`
        # is the endpoint declining to report, and must not arrive as a zero.
        out = {"turns_used": found.get("turns")}
        for key in ("tokens_in", "tokens_out", "tool_errors"):
            if found.get(key) is not None:
                out[key] = found[key]
        return {k: v for k, v in out.items() if v is not None}


    def spawn_blocked(self, binary: Path) -> str:
        """No endpoint, no run. `gbagent run` refuses with exit 78 and says exactly this, so
        the supervisor can say it BEFORE a wave rather than after the first child dies.

        This is knowable without asking anything: the variable is set or it is not.
        """
        if not os.environ.get(BASE_URL_ENV):
            return (f"no model endpoint: {BASE_URL_ENV} is unset, so a spawn exits 78 before "
                    "the child registers. Set it to an OpenAI-compatible chat/completions "
                    "endpoint (a local Ollama needs no key)")
        return ""

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
                *(["--item", seat.item] if seat.item else []),
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
        means rather than two that can drift.

        An OS-imposed status is not a crash. The loop's default is `crashed (exit N)`,
        and STATUS_CONTROL_C_EXIT (3221225786) — what a polite Windows stop reports —
        would otherwise be recorded as a crash (GRPH-588 bounce).
        """
        from gbagent.loop import exit_meaning
        from ..hostos import terminated_by_signal

        if terminated_by_signal(code):
            return f"stopped by signal ({code})"
        return exit_meaning(code)
