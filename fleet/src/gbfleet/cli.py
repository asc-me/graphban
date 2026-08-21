"""The `gbfleet` command line entry point.

Commands land in later slices of PRD-22 — `up` (GRPH-448), `orphans` (GRPH-443),
`stop`/`ps` alongside the stdio MCP server (GRPH-450). This module exists now so
each of those lands on a package that already installs, runs and is tested in CI,
rather than shipping the plumbing and the first feature in one reviewable lump.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__

_DESCRIPTION = """\
Spawn and retire Graphban fleet members on this machine.

gbfleet holds no authority of its own: it can only launch a process holding a seat
the Graphban server issued, to do work the Graphban server arbitrates.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gbfleet",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gbfleet {__version__}",
    )
    # Subcommands are registered here as the slices land. `dest` is read by main() to
    # tell "no command given" from a command that ran.
    parser.add_subparsers(dest="command", metavar="COMMAND")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_usage(sys.stderr)
        # Says what it is rather than just failing. A supervisor that exits 2 with a bare
        # usage string reads identically to one whose subcommands failed to register.
        print(
            "gbfleet: no commands are available in this build yet.",
            file=sys.stderr,
        )
        return 2

    raise AssertionError(f"unrouted command: {args.command!r}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
