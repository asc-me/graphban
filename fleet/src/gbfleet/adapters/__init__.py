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
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

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


def parse_version(text: str) -> tuple[int, ...]:
    """The first run of dotted numbers, as ints. Empty tuple when there is none."""
    match = _NUMERIC.search(text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _fmt(version: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in version) or "unknown"


@dataclass(frozen=True)
class Support:
    """The range an adapter was written against.

    `verified_against` is a version somebody actually RAN, or None. It is separate from
    the range on purpose: a declared range is a claim, and a claim nobody has exercised
    should not read the same as one that has. The support matrix prints both.
    """

    minimum: tuple[int, ...]
    maximum: tuple[int, ...] | None = None
    verified_against: str | None = None

    def permits(self, version: tuple[int, ...]) -> bool:
        if not version or version < self.minimum:
            return False
        return self.maximum is None or version < self.maximum

    def describe(self) -> str:
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

    def seat_path(self, worktree: Path) -> Path:
        """Where this vendor's MCP config must be written.

        Inside the worktree only where the vendor forces it. Anything under the worktree
        is a live credential in the project directory and `worktree.SEAT_FILES` has to
        know about it, or salvage will commit it.
        """
        raise NotImplementedError

    def launch(
        self, seat: Seat, tree: Worktree, instruction_file: Path, binary: Path
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


def resolve(name: str, *, binary: str | Path | None = None) -> tuple[Adapter, Path]:
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
    return adapter, found


from .claude import ClaudeCode  # noqa: E402
from .cursor import CursorAgent  # noqa: E402
from .grok import Grok  # noqa: E402

#: Only vendors whose flags and version strings were read off a real binary. `codex` is
#: absent on purpose — see `adapters/codex.py`. A fabricated adapter fails as a child
#: that starts and never registers, which costs a registration window and blames the
#: vendor for the supervisor's mistake.
ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (ClaudeCode(), CursorAgent(), Grok())
}

__all__ = [
    "ADAPTERS", "Adapter", "AdapterError", "AdapterUnavailable", "Support",
    "UnknownAdapter", "VersionUnsupported", "parse_version", "resolve",
]
