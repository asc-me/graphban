"""The `gbfleet` command line entry point.

Commands land as the slices of PRD-22 do. `up` is here (GRPH-448); `stop`, `ps` and
`orphans` arrive with the stdio MCP server (GRPH-450).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .adapters import ADAPTERS, AdapterError, Tuning, resolve
from .client import Graphban
from .lock import RepoLocked
from .seat import Seat
from dataclasses import replace

from .spawn import Launch
from .state import NotARepository
from .lock import hold
from .mcp import Fleet, serve
from .state import repo_root
from .supervisor import DEFAULT_MAX_WORKERS, Limits, Wave, up
from .worktree import Worktree

_DESCRIPTION = """\
Spawn and retire Graphban fleet members on this machine.

gbfleet holds no authority of its own: it can only launch a process holding a seat
the Graphban server issued, to do work the Graphban server arbitrates.
"""

#: Substituted into the child's argv. All of them are PATHS. Nothing carrying a
#: credential is ever passed as an argument, because argv is readable by every process
#: on the machine — the seat code lives in the instruction file and the API key in the
#: MCP config, both 0600. Declining to sandbox (D-k) is not the same as publishing a
#: live credential to `ps`.
PLACEHOLDERS = ("{seat_file}", "{instruction_file}", "{worktree}", "{branch}")

API_KEY_ENV = "GBFLEET_API_KEY"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gbfleet",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"gbfleet {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = sub.add_parser(
        "up",
        help="run one wave: spawn a child per seat, wait, reap",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The command after -- runs once per seat, with these substituted:\n"
            "  " + "  ".join(PLACEHOLDERS) + "\n\n"
            "Example:\n"
            "  gbfleet up --server https://cloud.agentldgr.dev --seats-file seats.txt \\\n"
            "      --adapter claude -- claude --mcp-config {seat_file} -p {instruction_file}\n\n"
            "The API key comes from $" + API_KEY_ENV + " and the seats from a file, never\n"
            "from argv: both are credentials, and argv is world-readable."
        ),
    )
    run.add_argument("--repo", default=".", help="repository to supervise (default: cwd)")
    run.add_argument("--server", required=True, help="Graphban base URL")
    run.add_argument(
        "--seats-file", required=True, help="one enrolment code per line; '-' reads stdin"
    )
    run.add_argument(
        "--adapter",
        required=True,
        help="which vendor to run: " + ", ".join(sorted(ADAPTERS)) + ". Named, never "
        "inferred: a fleet whose composition nobody chose defeats the one thing the "
        "supervisor can enforce. With a trailing -- command, this is only a label.",
    )
    run.add_argument(
        "--binary",
        default=None,
        help="override the resolved path for --adapter (skips the PATH lookup, not the "
        "version check)",
    )
    run.add_argument("--wave", default="wave", help="wave name, used in branch names")
    run.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    run.add_argument("--max-children", type=int, default=8)
    run.add_argument("--child-wall-clock", type=float, default=3600.0)
    run.add_argument("--workspace", default=None, help="where worktrees go")
    run.add_argument(
        "argv", nargs=argparse.REMAINDER, help="-- followed by the command to run per child"
    )

    stdio = sub.add_parser(
        "mcp",
        help="serve spawn/stop/ps/orphans over stdio, for a planner to drive",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Speaks JSON-RPC on stdin/stdout. There are no HTTP routes here and no\n"
            "credential: authentication is process ownership — the planner speaks over a\n"
            "pipe to a child it launched.\n\n"
            "`spawn` starts ONE child and takes no count. Mint a seat per child and call\n"
            "it once each; you already hold the Graphban server, so deciding how many is\n"
            "yours."
        ),
    )
    stdio.add_argument("--repo", default=".", help="repository to supervise (default: cwd)")
    stdio.add_argument("--server", required=True, help="Graphban base URL")
    stdio.add_argument("--workspace", default=None, help="where worktrees go")
    stdio.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    return parser


def read_seats(source: str, server: str, api_key: str) -> list[Seat]:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    codes = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return [Seat(code=c, server_url=server, api_key=api_key) for c in codes]


def make_adapter_factory(name: str, binary: str | None, model: str = "",
                         tuning: "Tuning | None" = None):
    """Resolve the named vendor NOW, so a bad one refuses before any worktree exists.

    The version check happens here rather than after launch, because a mismatch that
    surfaces as a child which starts, misbehaves and never registers costs a full
    registration window and blames the wrong component.
    """
    found = resolve(name, binary=binary, model=model, tuning=tuning)

    def factory(seat: Seat, tree: Worktree, instruction_file: Path) -> Launch:
        launch = found.adapter.launch(seat, tree, instruction_file, found.binary, model, tuning)
        return replace(launch, binary_version=found.version)

    return factory


def make_launch_factory(adapter: str, template: list[str]):
    def factory(seat: Seat, tree: Worktree, instruction_file: Path) -> Launch:
        seat_path = tree.path / ".cursor" / "mcp.json"
        values = {
            "{seat_file}": str(seat_path),
            "{instruction_file}": str(instruction_file),
            "{worktree}": str(tree.path),
            "{branch}": tree.branch,
        }
        return Launch(
            adapter=adapter,
            argv=[_substitute(part, values) for part in template],
            seat_path=seat_path,
            config=seat.mcp_config(),
            instruction="",  # already on disk; see supervisor._instruction_file
        )

    return factory


def _substitute(part: str, values: dict[str, str]) -> str:
    for token, value in values.items():
        part = part.replace(token, value)
    return part


def report(wave: Wave, out=sys.stdout) -> None:
    """Say what happened, including the parts that are absences.

    Every line answers a question somebody would otherwise infer from a length or a
    silence — how many seats went unused, whether a proposal of zero meant anything,
    whether a branch carries a credential the worker committed itself.
    """
    if wave.lock and wave.lock.takeover:
        print(f"took over a lock: {wave.lock.takeover.describe()}", file=out)

    if wave.before:
        note = (
            "  (no live agents yet — this describes the server's ignorance, not the work)"
            if wave.before.uninformative
            else ""
        )
        print(f"server proposed {wave.before.workers}w/{wave.before.reviewers}r{note}", file=out)
        print(f"  {wave.before.rationale}", file=out)

    print(f"spawned {len(wave.spawned)}", file=out)
    for child in wave.spawned:
        latency = (
            f"{child.registration_latency:.1f}s"
            if child.registration_latency is not None
            else "NEVER REGISTERED"
        )
        print(
            f"  {child.adapter} pid={child.pid} agent={child.agent_id} registered={latency}",
            file=out,
        )

    for reaped in wave.reaped:
        extra = ""
        if reaped.salvage and reaped.salvage.credential_in_history:
            extra = f"  !! seat in branch history: {reaped.salvage.credential_in_history}"
        print(f"  reaped {reaped.branch}: {reaped.disposition.value}{extra}", file=out)
        if reaped.reason:
            print(f"    {reaped.reason}", file=out)

    if wave.unused_seats:
        print(f"{wave.unused_seats} seat(s) never redeemed", file=out)
    for failure in wave.failures:
        print(f"FAILED {failure}", file=out)


def _serve_stdio(args) -> int:
    """Hold the repository and serve the local surface until stdin closes.

    The lock is held for the whole process (D-h): a second supervisor on this repository
    refuses to start rather than exceeding --max-workers between them, which is what
    makes that cap correct rather than approximate.
    """
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"gbfleet mcp: ${API_KEY_ENV} is not set", file=sys.stderr)
        return 2

    repo = Path(args.repo)
    client = Graphban(base_url=args.server, api_key=api_key)
    try:
        root = repo_root(repo)
    except NotARepository as exc:
        print(f"gbfleet mcp: {exc}", file=sys.stderr)
        return 2

    workspace = Path(args.workspace) if args.workspace else root.parent / f"{root.name}-gbfleet"
    try:
        with hold(root) as acquired:
            fleet = Fleet(
                repo=root,
                workspace=workspace,
                client=client,
                launch_for=lambda name, model="", tuning=None: make_adapter_factory(name, None, model, tuning),
                lock=acquired,
                limits=Limits(max_workers=args.max_workers),
            )
            serve(fleet)
    except RepoLocked as exc:
        print(f"gbfleet mcp: {exc}", file=sys.stderr)
        return 3
    finally:
        client.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_usage(sys.stderr)
        print("gbfleet: no command given. Try `gbfleet up --help`.", file=sys.stderr)
        return 2

    if args.command == "mcp":
        return _serve_stdio(args)

    template = [a for a in (args.argv or []) if a != "--"]
    try:
        # An explicit trailing command wins and `--adapter` is then just a label — the
        # escape hatch for a vendor with no adapter yet, and still explicit about which
        # binary runs. Otherwise the named adapter is resolved and version-checked.
        factory = (
            make_launch_factory(args.adapter, template)
            if template
            else make_adapter_factory(args.adapter, args.binary)
        )
    except AdapterError as exc:
        print(f"gbfleet up: {exc}", file=sys.stderr)
        return 2

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"gbfleet up: ${API_KEY_ENV} is not set", file=sys.stderr)
        return 2

    seats = read_seats(args.seats_file, args.server, api_key)
    if not seats:
        print(f"gbfleet up: no seats in {args.seats_file}", file=sys.stderr)
        return 2

    client = Graphban(base_url=args.server, api_key=api_key)
    try:
        wave = up(
            Path(args.repo),
            seats,
            factory,
            client,
            wave_name=args.wave,
            limits=Limits(
                max_workers=args.max_workers,
                max_children=args.max_children,
                child_wall_clock=args.child_wall_clock,
            ),
            workspace=Path(args.workspace) if args.workspace else None,
        )
    except RepoLocked as exc:
        print(f"gbfleet up: {exc}", file=sys.stderr)
        return 3
    except NotARepository as exc:
        print(f"gbfleet up: {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()

    report(wave)
    return 0 if wave.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
