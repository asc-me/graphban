"""One module per vendor binary, each declaring exactly four things.

PRD-22 S2: argv construction, config format and where it is written, stdout parsing,
and exit-code semantics. Four headless CLIs that agree on none of it.

**Selection is explicit, never inferred.** The spawn call names the vendor. There is no
scan of PATH for whichever agent CLI happens to be installed, because that produces a
fleet whose composition nobody chose — quietly defeating G5, the one thing the
supervisor is uniquely able to enforce. Resolving the NAMED vendor's binary on PATH is
a different act and is fine; `--binary` overrides it.

**A broken adapter fails at spawn, loudly.** The version is checked before a process
starts, and a mismatch refuses by name. The failure this exists to prevent is the child
that runs and never registers — the silent drop, indistinguishable from a slow start
until something puts a bound on it (`spawn.await_registration` is the other half).

**Versions do not share a scheme, which was measured rather than assumed:**

    claude        2.1.233 (Claude Code)          semver
    cursor-agent  2026.04.17-787b533             CalVer plus a git hash
    grok          grok 1.0.5 (5115b46bc909) [stable]

So there is no single semver comparison to make. What works for all three is the first
run of dotted numbers, compared as a tuple of ints: `2.1.233` -> (2, 1, 233) and
`2026.04.17` -> (2026, 4, 17). Both order correctly within their own scheme, which is
all a per-adapter range needs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, fields as dc_fields
from typing import NamedTuple
from pathlib import Path
from typing import ClassVar

from .. import seat as seat_mod
from ..seat import Seat
from ..spawn import Launch
from ..worktree import Worktree

_NUMERIC = re.compile(r"(\d+(?:\.\d+)*)")


class AdapterError(RuntimeError):
    """Base for every refusal that happens BEFORE a process starts."""


class UnknownAdapter(AdapterError):
    pass


class AdapterUnavailable(AdapterError):
    pass


class VersionUnsupported(AdapterError):
    pass


class TuningUnsupported(AdapterError):
    """A knob this vendor does not have.

    Refused rather than ignored. Silently dropping `effort` on a vendor with no such flag
    would let a caller believe it asked for something it did not get — and the bill would
    arrive as if it had. An unsupported knob is a mistake worth one clear error, not a
    setting that quietly evaporates.
    """


class ModelUnsupported(AdapterError):
    """A model this vendor says it does not have.

    Separate from `AdapterUnavailable` because the binary is fine and the operator's
    mistake is one word. Raised BEFORE a process starts, for the reason the version check
    is: a model the vendor rejects produces a child that starts, fails, and never
    registers — indistinguishable from a broken adapter until `await_registration` gives
    up, and blamed on the wrong component in the meantime.
    """


def parse_version(text: str) -> tuple[int, ...]:
    """The first run of dotted numbers, as ints. Empty tuple when there is none."""
    match = _NUMERIC.search(text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _fmt(version: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in version) or "unknown"


@dataclass(frozen=True)
class Tuning:
    """Per-vendor knobs beyond the model. All optional, all empty by default.

    These are NOT uniform across vendors and deliberately are not pretended to be: only
    `claude` takes a fallback list, only `grok` takes a reasoning effort. An adapter
    declares which it accepts and `resolve` refuses the rest by name.

    Both exist because a spawned child is UNATTENDED. Nobody is there to notice an
    overloaded model and retry, or to raise the effort when an answer comes back thin.
    """

    #: claude: comma-separated models to try when the primary is overloaded.
    fallback_model: str = ""
    #: grok: `--reasoning-effort`, free-form (see `Grok.tuning_argv`).
    effort: str = ""
    #: gbagent: the turn budget (PRD-24 D6). One turn is 22-45s against a local model.
    turns: str = ""
    #: gbagent: the model's context window in tokens, which compaction takes 70% of (D7).
    #: No default anywhere: assume too large and a run dies of an overflow compaction could
    #: have prevented; too small and it compacts constantly and throws away the 262k that
    #: made a local model worth using.
    window: str = ""

    def named(self) -> set[str]:
        """Which knobs were actually asked for.

        Derived from the dataclass rather than listed again here. The list was written out
        twice when there were two fields, which is one place to forget a third — and
        forgetting means `resolve` stops refusing an unsupported knob and starts silently
        dropping it, which is the exact failure `TuningUnsupported` exists to prevent.
        """
        return {f.name for f in dc_fields(self) if getattr(self, f.name)}


@dataclass(frozen=True)
class Support:
    """The range an adapter was written against.

    `verified_against` is a version somebody actually RAN, or None. It is separate from
    the range on purpose: a declared range is a claim, and a claim nobody has exercised
    should not read the same as one that has. The support matrix prints both.
    """

    minimum: tuple[int, ...] = ()
    maximum: tuple[int, ...] | None = None
    verified_against: str | None = None
    #: An EXACT version, for an adapter whose binary ships in this same distribution.
    #:
    #: The range machinery exists because three vendors release on their own schedules and
    #: we find out afterwards. `gbagent` is the one adapter where that problem does not
    #: arise — it is the same wheel as the supervisor — so a range would be ceremony, and
    #: worse than ceremony: it would accept a `gbagent` from a DIFFERENT install, which is
    #: the only version mismatch that can actually happen here (PRD-24 D8).
    exact: tuple[int, ...] | None = None

    def permits(self, version: tuple[int, ...]) -> bool:
        if self.exact is not None:
            return version == self.exact
        if not version or version < self.minimum:
            return False
        return self.maximum is None or version < self.maximum

    def describe(self) -> str:
        if self.exact is not None:
            return f"exactly {_fmt(self.exact)}"
        upper = f" and below {_fmt(self.maximum)}" if self.maximum else " or newer"
        return f"{_fmt(self.minimum)}{upper}"


class Adapter:
    """What the supervisor needs to know about one vendor CLI."""

    name: ClassVar[str]
    binary: ClassVar[str]
    version_argv: ClassVar[tuple[str, ...]] = ("--version",)
    support: ClassVar[Support]
    #: Human-readable, and printed in the support matrix. Says what is odd about this
    #: vendor, because every one of them is odd in a different way.
    notes: ClassVar[str] = ""
    #: Which `Tuning` fields this vendor actually has. Anything else is refused, never
    #: ignored — see `TuningUnsupported`.
    tuning: ClassVar[frozenset[str]] = frozenset()
    #: The language this vendor's seat file is written in. JSON for all but grok, which
    #: reads TOML and nothing else. Declared beside `seat_path` because the two are one
    #: fact: WHERE the file goes and WHAT it is written in are both vendor trivia, and
    #: getting either wrong produces the same symptom — a child with no tools and no
    #: error (GRPH-575).
    seat_format: ClassVar[str] = seat_mod.JSON

    def model_argv(self, model: str) -> list[str]:
        """The flag this vendor spells `--model`, or nothing when none was named.

        Per-adapter for the same reason `seat_path` and `exit_meaning` are: three CLIs,
        three spellings (`--model`, `--model`, `-m`). Returning [] for an empty model is
        what keeps the default path byte-identical to not having this feature at all.

        The supervisor does NOT choose the value. PRD-22 §1 is explicit that it "does not
        choose models for subagents", and it could not if it wanted to — a seat's role is
        fixed server-side at mint and opaque until redeemed (D-j), so there is nothing
        locally to key a policy on. The caller names the model exactly as it names the
        vendor: "Named, never inferred".
        """
        return ["--model", model] if model else []

    def tuning_argv(self, tuning: "Tuning") -> list[str]:
        """Vendor flags for the knobs this adapter declares. Empty unless overridden."""
        return []

    def debug_argv(self, path: Path) -> list[str]:
        """Flags that make this vendor write a debug log to `path`, or [] if it has none.

        **Empty means "this vendor cannot", and the caller must say so.** Measured per
        vendor rather than assumed, and the answers genuinely differ: `grok` and `claude`
        both take a debug file, `cursor-agent`'s `--help` has no debug or verbose flag at
        all, and first-party `gbagent` has none either. An operator who asks for `--debug`
        and silently gets nothing extra from half the fleet has been told the fleet is
        more observable than it is, which is worse than knowing it is not.

        The flags are placed by each adapter rather than appended by the caller, because
        two of these CLIs end their argv with a positional prompt and a flag after it
        would be read as part of the prompt.
        """
        return []

    def known_models(self, binary: Path) -> frozenset[str] | None:
        """What this binary says it can run, or None when it cannot be asked.

        **None and an empty set are different answers and must stay different.** `claude`
        has no listing flag at all, so nothing can be checked and a named model has to be
        passed through. `cursor-agent --list-models` answers "No models available for this
        account" for an account with no entitlements — which is not the same claim as
        "every model you could name is wrong", and refusing a spawn on it would break a
        working setup over an unrelated entitlement.

        So: None means "unchecked, passed through"; a non-empty set means "checked". The
        support matrix prints which, because an unvalidated pass-through must not read the
        same as a verified one.
        """
        return None

    def seat_path(self, worktree: Path) -> Path:
        """Where this vendor's MCP config must be written.

        Inside the worktree only where the vendor forces it. Anything under the worktree
        is a live credential in the project directory and `worktree.SEAT_FILES` has to
        know about it, or salvage will commit it.
        """
        raise NotImplementedError

    def launch(
        self, seat: Seat, tree: Worktree, instruction_file: Path, binary: Path,
        model: str = "", tuning: "Tuning | None" = None, *,
        debug_file: Path | None = None,
    ) -> Launch:
        raise NotImplementedError

    def exit_meaning(self, code: int) -> str:
        """What a non-zero exit means for THIS vendor.

        Default: exit 0 is the normal end of a worker's life (D-c) and anything else is
        the adapter's business to explain. Vendors that overload specific codes say so.
        """
        return "finished" if code == 0 else f"exited {code}"


def _run_version(binary: Path, argv: tuple[str, ...]) -> str:
    try:
        proc = subprocess.run(
            [str(binary), *argv], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterUnavailable(f"{binary}: could not run {' '.join(argv)}: {exc}") from exc
    return (proc.stdout or proc.stderr or "").strip()


class Resolved(NamedTuple):
    """A vendor, the binary that answered, and what it said its version was.

    The version string is carried rather than discarded because S6 puts it in every
    child's record: "adapter and resolved binary version" is the first line of the
    observability field list. A record naming the adapter but not the build is one that
    cannot answer "did this start failing when they shipped 2.2?".
    """

    adapter: "Adapter"
    binary: Path
    version: str


def resolve(
    name: str, *, binary: str | Path | None = None, model: str = "",
    tuning: "Tuning | None" = None,
) -> Resolved:
    """Find the NAMED vendor's binary and refuse if its version is out of range.

    Refusing here rather than after launch is the whole point: a version mismatch that
    surfaces as a child which starts, misbehaves and never registers costs a
    registration window and blames the wrong thing.
    """
    adapter = ADAPTERS.get(name)
    if adapter is None:
        raise UnknownAdapter(
            f"no adapter named {name!r}. Known: {', '.join(sorted(ADAPTERS))}. "
            "The vendor is named, never inferred — a fleet assembled from whatever "
            "happens to be on PATH is a fleet whose composition nobody chose."
        )

    found = Path(binary) if binary else (
        Path(p) if (p := shutil.which(adapter.binary)) else None
    )
    if found is None or not found.exists():
        raise AdapterUnavailable(
            f"adapter {name!r} needs {adapter.binary!r}, which is not on PATH. "
            "Install it, or pass --binary."
        )

    reported = _run_version(found, adapter.version_argv)
    version = parse_version(reported)
    if not adapter.support.permits(version):
        raise VersionUnsupported(
            f"adapter {name!r}: {found} reports {reported!r} (parsed {_fmt(version)}), "
            f"and this adapter supports {adapter.support.describe()}. Refusing to spawn."
        )

    asked = (tuning or Tuning()).named()
    if unsupported := asked - adapter.tuning:
        raise TuningUnsupported(
            f"adapter {name!r} has no {', '.join(sorted(unsupported))}. "
            f"It accepts: {', '.join(sorted(adapter.tuning)) or 'no tuning knobs'}. "
            "Refusing rather than ignoring it, so a caller cannot believe it asked for "
            "something it did not get."
        )

    if model:
        known = adapter.known_models(found)
        # `known` is None when the vendor cannot be asked, and that is NOT the same as an
        # empty set — see `known_models`. Only a non-empty listing can refuse anything.
        if known and model not in known:
            raise ModelUnsupported(
                f"adapter {name!r}: {found} does not list a model named {model!r}. "
                f"It offers: {', '.join(sorted(known))}. Refusing to spawn, because a "
                "model the vendor rejects costs a registration window and reads as a "
                "broken adapter rather than as a typo."
            )

    return Resolved(adapter=adapter, binary=found, version=reported)


from .claude import ClaudeCode  # noqa: E402
from .cursor import CursorAgent  # noqa: E402
from .gbagent import GbAgent  # noqa: E402
from .grok import Grok  # noqa: E402

#: Only vendors whose flags and version strings were read off a real binary. `codex` is
#: absent on purpose — see `adapters/codex.py`. A fabricated adapter fails as a child
#: that starts and never registers, which costs a registration window and blames the
#: vendor for the supervisor's mistake.
#: `gbagent` is the first-party one (PRD-24 D8). It is in this list on the same terms as
#: the rest and gets no special handling anywhere in `resolve` — G4 says deleting it must
#: change nothing about how the fleet is arbitrated.
ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (ClaudeCode(), CursorAgent(), GbAgent(), Grok())
}

__all__ = [
    "ADAPTERS", "Adapter", "AdapterError", "AdapterUnavailable", "Resolved", "Support",
    "UnknownAdapter", "VersionUnsupported", "parse_version", "resolve",
]
