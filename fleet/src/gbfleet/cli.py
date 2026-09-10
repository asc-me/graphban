"""The `gbfleet` command line entry point.

Commands land as the slices of PRD-22 do. `up` is here (GRPH-448); `stop`, `ps` and
`orphans` arrive with the stdio MCP server (GRPH-450).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from . import adopt as adopt_mod
from .adapters import ADAPTERS, AdapterError, Tuning, checked_tuning, resolve
from .client import ALLOWED_TOOLS, Graphban
from . import doctor
from . import service as service_mod
from .lock import RepoLocked
from .seat import Seat, codes_from_text, parse_seat_line
from dataclasses import replace

from .spawn import Launch
from .state import NotARepository
from .lock import hold
from .mcp import Fleet, serve, read_preferences
from .state import repo_root
from .supervisor import DEFAULT_MAX_WORKERS, Limits, Merger, Wave, up
from .tiers import TierTable
from . import matrix as matrix_mod
from .until import PLANNER_TOOLS, emit as emit_until, run as run_until
from .worktree import Worktree, default_ref, remote_for

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

#: `ALLOWED_TOOLS` stays two reads (P30 G5). Resume (D9) needs item status so a
#: salvage branch is reused without the caller injecting `items=`. `search_items`
#: is a read; this set is the CLI/MCP process, not a widening of the supervisor
#: authority table.
SPAWN_READS: frozenset[str] = ALLOWED_TOOLS | frozenset({"search_items"})

#: What `up --merge` needs beyond `SPAWN_READS` (GRPH-846): the item and its attestations,
#: the dependency rows, and ONE write — the receipt naming the merge commit. Only under the
#: flag; a plain `up` keeps the two-reads-plus-search client it has always had. `update_item`
#: on a supervisor is the widening PRD-22 §4 warns about, and it is here because the operator
#: asked for the merge by name and the receipt is the half that makes the merge visible to
#: the ledger. The server still bounds what that call may write by role.
MERGE_TOOLS: frozenset[str] = frozenset({"get_item_details", "related_work", "update_item"})

_MERGE_HELP = (
    "after an item reaches `done`, mark its PR ready and enable squash auto-merge via gh — "
    "only when the PR head is the commit the sign-off attestation names, CI attested "
    "suite_green on it, and the forge reports it MERGEABLE/CLEAN. Any miss is reported and "
    "the item is left alone. Default off (GRPH-846)")


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
    run.add_argument("--project", default="", help="the Graphban project this fleet works; named on every call so a credential spanning several projects lands where the seats were minted (GRPH-718)")
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
    run.add_argument(
        "--mcp-server", action="append", default=[], metavar="NAME",
        help="share one of YOUR MCP servers with each child, by exact name (e.g. context7). "
             "Repeatable. Exact names only — no patterns — and an unknown name refuses the "
             "run. Sharing a server shares its credential")
    run.add_argument(
        "--allow", action="append", default=[], metavar="NAME",
        help="let children run this command despite the default deny-list (e.g. psql for "
             "local container checks). Repeatable")
    run.add_argument(
        "--deny", action="append", default=[], metavar="NAME",
        help="add a command to the deny-list children get a refusing stub for. Repeatable")
    run.add_argument("--wave", default="wave", help="wave name, used in branch names")
    run.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    run.add_argument("--max-children", type=int, default=8)
    run.add_argument("--child-wall-clock", type=float, default=3600.0)
    run.add_argument("--workspace", default=None, help="where worktrees go")
    run.add_argument(
        "--debug",
        action="store_true",
        help=(
            "ask each vendor to write a debug log beside its stdout, and emit a per-poll "
            "reading of what every child is producing. Adapters with no debug flag "
            "(cursor-agent, gbagent) are named on the summary rather than passed over"
        ),
    )
    run.add_argument(
        "--quiet-after",
        type=float,
        default=Limits.quiet_after,
        help=(
            "seconds of no output before a child is REPORTED as quiet (default: "
            f"{Limits.quiet_after:.0f}). Nothing is stopped on it — a child inside one "
            "long tool call is legitimately silent"
        ),
    )
    run.add_argument("--merge", action="store_true", default=False, help=_MERGE_HELP)
    run.add_argument(
        "argv", nargs=argparse.REMAINDER, help="-- followed by the command to run per child"
    )

    doc = sub.add_parser(
        "doctor",
        help="check everything that can be checked before a child is spawned",
        description=(
            "Answers the questions that otherwise cost a wave: does this repository "
            "commit a seat path, can a credential file be kept private on this "
            "filesystem, is the vendor binary in range, does the server accept this "
            "key. Reports PASS, FAIL and UNKNOWN — a check that could not run is not "
            "a check that passed."
        ),
    )
    doc.add_argument("--repo", default=".", help="repository to check (default: cwd)")
    doc.add_argument("--workspace", default=None, help="where worktrees would go")
    doc.add_argument("--adapter", default="", help="vendor you intend to run")
    doc.add_argument("--server", default="", help="Graphban base URL")
    doc.add_argument("--project", default="", help="the Graphban project this fleet works; named on every call so a credential spanning several projects lands where the seats were minted (GRPH-718)")
    doc.add_argument("--matrix", default="", help="path to a preference matrix (PRD-37); default is the one shipped with gbfleet")
    doc.add_argument("--seats-file", default=None, help="seats file `up` would read")

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
    stdio.add_argument("--project", default="", help="the Graphban project this fleet works; named on every call so a credential spanning several projects lands where the seats were minted (GRPH-718)")
    stdio.add_argument("--matrix", default="", help="path to a preference matrix (PRD-37); default is the one shipped with gbfleet")
    stdio.add_argument("--workspace", default=None, help="where worktrees go")
    stdio.add_argument(
        "--mcp-server", action="append", default=[], metavar="NAME",
        help="share one of YOUR MCP servers with each child, by exact name (e.g. context7). "
             "Repeatable. Exact names only — no patterns — and an unknown name refuses the "
             "run. Sharing a server shares its credential")
    stdio.add_argument(
        "--allow", action="append", default=[], metavar="NAME",
        help="let children run this command despite the default deny-list (e.g. psql for "
             "local container checks). Repeatable")
    stdio.add_argument(
        "--deny", action="append", default=[], metavar="NAME",
        help="add a command to the deny-list children get a refusing stub for. Repeatable")
    stdio.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    stdio.add_argument(
        "--tier", action="append", default=[], metavar="NAME=ADAPTER[:MODEL]",
        help="what a tier means on this machine, e.g. cheap=gbagent:qwen3.6:35b-a3b-coding-mtp-det "
             "or frontier=claude:opus; repeatable. spawn(tier=...) resolves through it (PRD-36). "
             "Fixed for the life of the process",
    )

    svc = sub.add_parser(
        "service",
        help="run a drain under launchd / systemd --user, so it outlives your terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Installs `gbfleet until` as a user-domain service. Everything after the "
            "subcommand is passed to `until` unchanged, so `gbfleet until --help` stays the "
            "authority on its own flags.\n\n"
            "The API key is read from $" + API_KEY_ENV + " at install time and written to an "
            "owner-only file the unit references. It is NEVER put in the unit itself: a unit "
            "file is world-readable.\n\n"
            "User domain only. A root installer is a different program with different "
            "failure modes, and even the server's has never been walked privileged."),
        epilog=(
            "Example:\n"
            "  gbfleet service install -- --repo /srv/graphban --server http://box:8080 \\\n"
            "      --project graphban --adapter claude --max-workers 2\n"
            "  gbfleet service status\n"
            "  gbfleet service uninstall\n"),
    )
    svc_do = svc.add_subparsers(dest="act", metavar="ACT")
    svc_install = svc_do.add_parser(
        "install", help="write the unit, load it, and check it is actually running",
        add_help=False)
    svc_install.add_argument(
        "--name", default=service_mod.DEFAULT_NAME,
        help=f"one service per name (default {service_mod.DEFAULT_NAME}), so two clones of a "
             "repository can each have a drain")
    svc_install.add_argument(
        "--dry-run", action="store_true",
        help="print the unit that WOULD be written and change nothing. Read it before you "
             "trust it; that is the whole point of a file on disk")
    svc_install.add_argument(
        "--every", type=int, default=service_mod.RESTART_SEC, metavar="SECONDS",
        help=f"how often the drain looks for work (default {service_mod.RESTART_SEC}s). "
             "`until` exits when the backlog is empty, so this is the polling interval — and "
             "every cycle registers one planner agent, which is why the default is minutes "
             "rather than seconds")
    svc_install.add_argument("rest", nargs=argparse.REMAINDER,
                             help="arguments for `gbfleet until`")
    svc_status = svc_do.add_parser("status", help="installed? running? and with which PATH?")
    svc_status.add_argument(
        "--name", default=None,
        help="one drain. Without it, EVERY drain installed here — an operator who ran "
             "`--name nightly` and forgot has a service this program wrote and cannot see")
    svc_remove = svc_do.add_parser("uninstall",
                                   help="stop it and remove the unit AND its key file")
    svc_remove.add_argument("--name", default=service_mod.DEFAULT_NAME)

    until = sub.add_parser(
        "until",
        help="planner loop: mint, spawn, watch, until idle or a cap",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Planner-mode, in-process. Holds a planner key and the supervisor allowlist\n"
            "as two clients. Minting is just in time. Idle is no ready work, no unsigned\n"
            "review, and no live lease — not merely that the last child exited.\n\n"
            "Stdout: human lines, then one JSON object keyed on reason + exit."
        ),
    )
    until.add_argument("--repo", default=".", help="repository to supervise (default: cwd)")
    until.add_argument("--server", required=True, help="Graphban base URL")
    until.add_argument("--project", default="", help="the Graphban project this fleet works; named on every call so a credential spanning several projects lands where the seats were minted (GRPH-718)")
    until.add_argument("--matrix", default="", help="path to a preference matrix (PRD-37); default is the one shipped with gbfleet")
    until.add_argument(
        "--seats-file", default=None,
        help="optional pre-minted enrolment codes, consumed before minting",
    )
    until.add_argument(
        "--adapter", required=True,
        help="which vendor to run: " + ", ".join(sorted(ADAPTERS)),
    )
    until.add_argument("--binary", default=None, help="override the resolved path for --adapter")
    until.add_argument("--wave", default="wave", help="wave name, used in branch names")
    until.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    until.add_argument(
        "--request", choices=["cheap", "frontier"], default=None,
        help="tier to REQUEST on each delegation this loop writes (PRD-35/36); default follows "
             "the brief's suggestion. Observational: the child declares what it actually is",
    )
    until.add_argument(
        "--tier", action="append", default=[], metavar="NAME=ADAPTER[:MODEL]",
        help="what a tier means on this machine (PRD-36 D6), repeatable. When the requested "
             "tier is mapped here its adapter runs the child; otherwise --adapter does",
    )
    until.add_argument(
        "--prd", default="",
        help="scope the wave to ONE PRD's items. Without it the loop drains every ready item "
             "in the project, and `backlog` is no defence — backlog is claimable by design "
             "(GRPH-397). Needs a credential with the `fleet` tool tier to be advertised")
    until.add_argument(
        "--mcp-server", action="append", default=[], metavar="NAME",
        help="share one of YOUR MCP servers with each child, by exact name (e.g. context7). "
             "Repeatable. Exact names only — no patterns — and an unknown name refuses the "
             "run. Sharing a server shares its credential")
    until.add_argument(
        "--allow", action="append", default=[], metavar="NAME",
        help="let children run this command despite the default deny-list (e.g. psql for "
             "local container checks). Repeatable")
    until.add_argument(
        "--deny", action="append", default=[], metavar="NAME",
        help="add a command to the deny-list children get a refusing stub for. Repeatable")
    until.add_argument(
        "--budget", type=int, default=None, metavar="TOKENS",
        help="end the wave once the children that REPORT their usage have spent this many "
             "tokens. Refused up front when the adapter reports nothing, because a cap that "
             "can never be exceeded never fires")
    until.add_argument(
        "--dry-run", action="store_true",
        help="print what this wave WOULD delegate and exit, without minting a seat or cutting "
             "a worktree. The scoping bug existed for exactly as long as nobody could see the "
             "plan")
    until.add_argument("--max-children", type=int, default=8)
    until.add_argument("--child-wall-clock", type=float, default=3600.0)
    until.add_argument("--workspace", default=None, help="where worktrees go")
    until.add_argument("--debug", action="store_true")
    until.add_argument(
        "--quiet-after", type=float, default=Limits.quiet_after,
        help="seconds of no output before a child is REPORTED as quiet",
    )
    until.add_argument("--merge", action="store_true", default=False, help=_MERGE_HELP)
    until.add_argument(
        "argv", nargs=argparse.REMAINDER, help="-- followed by the command to run per child"
    )
    return parser


def read_seats(source: str, server: str, api_key: str) -> list[Seat]:
    """One seat per line: `CODE [item=GRPH-nn] [role=reviewer]` (`seat.parse_seat_line`).
    Raises ValueError on a mistyped line, and `up` exits 2 on it before any worktree."""
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    seats: list[Seat] = []
    for line in codes_from_text(text):
        code, fields = parse_seat_line(line)
        seats.append(Seat(code=code, server_url=server, api_key=api_key,
                          role=fields.get("role", "worker"), item=fields.get("item")))
    return seats


def make_adapter_factory(name: str, binary: str | None, model: str = "",
                         tuning: "Tuning | None" = None):
    """Resolve the named vendor NOW, so a bad one refuses before any worktree exists.

    The version check happens here rather than after launch, because a mismatch that
    surfaces as a child which starts, misbehaves and never registers costs a full
    registration window and blames the wrong component.
    """
    found = resolve(name, binary=binary, model=model, tuning=tuning)
    # GRPH-831: and the arguments this adapter refuses to guess, asked HERE — where the
    # vendor is resolved and before any worktree exists — for the same reason the version is.
    # Asked at spawn instead, the operator has already paid for a worktree and a seat.
    checked_tuning(found.adapter, tuning or Tuning())

    def factory(seat: Seat, tree: Worktree, instruction_file: Path,
                debug_file: Path | None = None) -> Launch:
        launch = found.adapter.launch(
            seat, tree, instruction_file, found.binary, model, tuning,
            debug_file=debug_file,
        )
        return replace(launch, binary_version=found.version)

    return factory


def make_launch_factory(adapter: str, template: list[str]):
    """A factory from a literal argv template, for stand-ins and probes.

    It takes `debug_file` and does nothing with it, on purpose. A template is not a
    vendor and has no flags to add, so `debug_path` stays None — which is the same answer
    `cursor-agent` and `gbagent` give, and makes the supervisor report the gap rather
    than pretend the child is writing a debug log somewhere.
    """

    def factory(seat: Seat, tree: Worktree, instruction_file: Path,
                debug_file: Path | None = None) -> Launch:
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


def report(wave: Wave, out=None) -> None:
    """Say what happened, including the parts that are absences.

    Every line answers a question somebody would otherwise infer from a length or a
    silence — how many seats went unused, whether a proposal of zero meant anything,
    whether a branch carries a credential the worker committed itself.
    """
    # Resolved here rather than as a default: `out=sys.stdout` in the signature binds
    # whatever stdout was at IMPORT, so an in-process redirect gets nothing while the
    # summary goes somewhere nobody is reading (found in `doctor`, same shape).
    out = sys.stdout if out is None else out
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

    for branch in wave.resumed:
        print(f"RESUMED {branch}", file=out)
    for miss in wave.resume_misses:
        print(f"RESUME MISS {miss}", file=out)
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

    # Both silences, and they are different claims about different evidence. `silent` is
    # what this machine saw: the child's own log files stopped growing, which needs no
    # network and no vendor cooperation. `quiet` is what the SERVER saw: no heartbeat
    # inside the presence TTL, which a partition produces just as readily as a stuck
    # child. Printing one and not the other would let a fleet look healthy from whichever
    # side happened to be reported.
    #
    # `quiet` was populated by the supervisor and printed by nothing at all until
    # GRPH-579 — the field existed, its docstring said it was there so an operator would
    # not have to work it out afterwards, and no output surface ever mentioned it.
    # THE PARTITION, CHECKED AGAINST WHAT HAPPENED (GRPH-785). `collided` first: it is the
    # failure itself, observed, and needs no declaration to be right. `undeclared` is the
    # cause the failure usually has. Both were computed at reap and printed by nothing,
    # which is the shape GRPH-579 already caught once on `quiet`.
    for path, branches in sorted(wave.collided.items()):
        print(f"COLLIDED {path}: changed on {', '.join(branches)}", file=out)
    for branch, paths in sorted(wave.undeclared.items()):
        shown = ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")
        print(f"UNDECLARED {branch}: changed {len(paths)} file(s) no touchpoint covers — "
              f"{shown}", file=out)
    # HOW STALE the reviewer's diff is (GRPH-786). The supervisor is the only party that
    # can answer it: the server has no git, and the reviewer sees a branch with no
    # indication of what it is a diff against.
    for branch, (behind, ref) in sorted(wave.stale.items()):
        print(f"BEHIND {branch}: cut from a base {behind} commit(s) behind {ref}", file=out)
    if wave.stale_unmeasured:
        print(f"BEHIND unmeasured: {wave.stale_unmeasured}", file=out)
    for key, seconds in sorted(wave.silent.items()):
        print(f"QUIET {key}: wrote nothing for {seconds:.0f}s (local)", file=out)
    for key, seconds in sorted(wave.quiet.items()):
        print(f"QUIET {key}: no heartbeat for {seconds:.0f}s (server)", file=out)
    for gap in wave.debug_gaps:
        print(f"NO DEBUG {gap}", file=out)

    if wave.unused_seats:
        print(f"{wave.unused_seats} seat(s) never redeemed", file=out)
    # Immediately after the count, because it is the answer to the question the count
    # raises. "3 seats never redeemed" with no reason reads as a bug in the fleet.
    for gate in wave.gated:
        print(f"NO ROOM {gate}", file=out)
    for failure in wave.failures:
        print(f"FAILED {failure}", file=out)
    for give_up in wave.give_ups:
        print(f"STUCK {give_up}", file=out)
    # WHAT BECAME OF EACH MERGE (GRPH-846). Four lines for four outcomes, because a merge
    # the forge refused, a merge that is armed and waiting, and an item left alone because
    # its head moved are different facts a person acts on differently.
    for item_id, got in sorted(wave.merged.items()):
        if got.ok:
            print(f"MERGED {item_id}: {got.commit[:12]} {got.url}".rstrip(), file=out)
        elif got.pending:
            print(f"MERGE PENDING {item_id}: {got.reason}", file=out)
        elif got.skipped:
            print(f"MERGE SKIPPED {item_id}: {got.reason}", file=out)
        else:
            print(f"MERGE HELD {item_id}: {got.reason}"
                  + (f" (checked: {', '.join(got.checked)})" if got.checked else ""),
                  file=out)


def _until(args) -> int:
    """Planner loop. Two clients, one key: minting never goes through ALLOWED_TOOLS."""
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"gbfleet until: ${API_KEY_ENV} is not set", file=sys.stderr)
        return 2

    template = [a for a in (args.argv or []) if a != "--"]
    try:
        factory = (
            make_launch_factory(args.adapter, template)
            if template
            else make_adapter_factory(args.adapter, args.binary)
        )
    except AdapterError as exc:
        print(f"gbfleet until: {exc}", file=sys.stderr)
        return 2

    try:
        tiers = TierTable.parse(args.tier)
    except ValueError as exc:
        print(f"gbfleet until: {exc}", file=sys.stderr)
        return 2

    pool: list[Seat] = []
    if args.seats_file:
        try:
            pool = read_seats(args.seats_file, args.server, api_key)
        except ValueError as exc:
            print(f"gbfleet until: {exc}", file=sys.stderr)
            return 2

    planner = Graphban(base_url=args.server, api_key=api_key, allowed=PLANNER_TOOLS, project_id=args.project)
    supervisor = Graphban(base_url=args.server, api_key=api_key, allowed=ALLOWED_TOOLS, project_id=args.project)
    if args.dry_run:
        # BEFORE the lock and before anything is minted (GRPH-819). A dry run that acquired the
        # repository would stop being one, and a planner asking "what would this take?" while a
        # wave runs is exactly who needs to ask.
        from . import until as until_mod

        try:
            got = until_mod.plan(planner, args.prd or None, args.max_workers)
        except (ToolFailed, NotPermitted, ServerUnreachable) as exc:
            print(f"gbfleet until --dry-run: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(got, indent=1, sort_keys=True))
        seeds = got["would_delegate"]
        print(f"\nwould delegate {len(seeds)}: {', '.join(str(x) for x in seeds) or 'nothing'}",
              file=sys.stderr)
        if got["clusters_held"]:
            print(f"{got['clusters_held']} cluster(s) held by another agent and not counted",
                  file=sys.stderr)
        if got["capped_by_max_workers"]:
            print(f"capped at --max-workers {args.max_workers}; "
                  f"{got['clusters_free']} clusters are free", file=sys.stderr)
        return 0
    try:
        result = run_until(
            Path(args.repo),
            factory,
            planner,
            supervisor,
            api_key=api_key,
            server=args.server,
            adapter=args.adapter,
            seats=pool,
            wave_name=args.wave,
            limits=Limits(
                max_workers=args.max_workers,
                max_children=args.max_children,
                child_wall_clock=args.child_wall_clock,
                quiet_after=args.quiet_after,
            ),
            workspace=Path(args.workspace) if args.workspace else None,
            debug=args.debug,
            request=args.request,
            prd=args.prd or None,
            budget=args.budget or None,
            shared=_shared_servers(args),
            tiers=tiers,
            launch_for=lambda name, model="": make_adapter_factory(name, None, model),
            matrix=matrix_mod.load(Path(args.matrix)) if args.matrix else matrix_mod.load(),
            merge=bool(args.merge),
        )
    except RepoLocked as exc:
        print(f"gbfleet until: {exc}", file=sys.stderr)
        return 3
    except NotARepository as exc:
        print(f"gbfleet until: {exc}", file=sys.stderr)
        return 2
    finally:
        planner.close()
        supervisor.close()

    emit_until(result)
    return result.exit


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
    client = Graphban(base_url=args.server, api_key=api_key, allowed=SPAWN_READS, project_id=args.project)
    try:
        root = repo_root(repo)
    except NotARepository as exc:
        print(f"gbfleet mcp: {exc}", file=sys.stderr)
        return 2

    workspace = Path(args.workspace) if args.workspace else root.parent / f"{root.name}-gbfleet"
    try:
        tiers = TierTable.parse(args.tier)
    except ValueError as exc:
        print(f"gbfleet stdio: {exc}", file=sys.stderr)
        return 2
    try:
        with hold(root) as acquired:
            fleet = Fleet(
                repo=root,
                workspace=workspace,
                client=client,
                launch_for=lambda name, model="", tuning=None: make_adapter_factory(name, None, model, tuning),
                lock=acquired,
                limits=Limits(max_workers=args.max_workers),
                tiers=tiers,
                matrix=matrix_mod.load(Path(args.matrix)) if args.matrix else matrix_mod.load(),
                shared=_shared_servers(args),
            )
            fleet.profile, fleet.policy, pref_note, fleet.measured, fleet.cap_measured = read_preferences(client)
            print(f"gbfleet mcp: {pref_note}", file=sys.stderr)
            if acquired.takeover:
                leftover, _occupied, notes = adopt_mod.recover(root, workspace)
                fleet.children.extend(leftover)
                for child in leftover:
                    tail = child.branch.rsplit("-", 1)[-1]
                    if tail.isdigit():
                        fleet.started = max(fleet.started, int(tail))
                for note in notes:
                    print(f"gbfleet mcp: {note}", file=sys.stderr)
            serve(fleet)
    except RepoLocked as exc:
        print(f"gbfleet mcp: {exc}", file=sys.stderr)
        return 3
    finally:
        client.close()
    return 0


def _shared_servers(args) -> dict:
    """The servers named with `--mcp-server`, resolved before anything is spawned (GRPH-816).

    Resolved HERE rather than per child so a typo refuses the run instead of the fourth
    worktree, and so the refusal reaches a terminal rather than a child's stderr.
    """
    from . import mcpshare

    try:
        return mcpshare.select(list(getattr(args, "mcp_server", []) or []))
    except mcpshare.ShareRefused as exc:
        print(f"gbfleet: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _service(args) -> int:
    """`gbfleet service …`. Every refusal is printed and returns 2; nothing half-writes."""
    act = getattr(args, "act", None)
    if not act:
        print("gbfleet service: no action given. Try `gbfleet service --help`.",
              file=sys.stderr)
        return 2

    if act == "status":
        found = service_mod.host()
        if not found.kind:
            print(f"gbfleet service: {found.why}", file=sys.stderr)
            return 1
        names = [args.name] if args.name else service_mod.installed_names(found.kind)
        if not names:
            print(f"no drain installed ({found.kind})")
            return 0
        for one in names:
            _service_status(service_mod.status(one, kind=found.kind))
        return 0

    if act == "uninstall":
        try:
            removed = service_mod.uninstall(args.name)
        except service_mod.Refused as exc:
            print(f"gbfleet service: {exc}", file=sys.stderr)
            return 2
        for path in removed:
            print(f"removed {path}")
        if not removed:
            print(f"nothing to remove for {args.name!r}")
        return 0

    rest = [a for a in (args.rest or []) if a != "--"]
    try:
        plan = service_mod.make_plan(rest, name=args.name, every=args.every)
    except service_mod.Refused as exc:
        print(f"gbfleet service: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"# would write {plan.unit_path}")
        print(f"# and {plan.env_path} (owner-only, ${service_mod.API_KEY_ENV})")
        print(service_mod.render(plan).decode("utf-8", "replace"))
        return 0

    try:
        done = service_mod.install(plan, os.environ.get(API_KEY_ENV, ""))
    except service_mod.Refused as exc:
        print(f"gbfleet service: {exc}", file=sys.stderr)
        return 2

    print(f"runs  {plan.binary} until {' '.join(plan.until_args)}")
    print(f"every {plan.every}s (one planner registration per cycle)")
    print(f"wrote {plan.unit_path}")
    print(f"wrote {plan.env_path} (owner-only)")
    print(f"logs  {plan.log_path}")
    # Named because it is a consequence nobody expects: the lock is per git COMMON DIR, so a
    # running drain refuses every interactive `gbfleet up` on this clone for as long as it
    # runs. Two drains want two clones, not two worktrees.
    print(f"holds the repo lock for {plan.common_dir} while it runs")
    for warning in done.warnings:
        print(f"WARNING {warning}", file=sys.stderr)
    if done.running:
        print(f"{plan.name}: running")
        return 0
    # Accepted is not running — but for THIS service, not running is usually correct. `until`
    # exits when there is no ready work, so a healthy drain is idle for most of every cycle,
    # and reporting that as a failed install would train an operator to ignore the one case
    # that matters. The last exit code is what separates them.
    if done.state is not None and done.state.idle:
        print(f"{plan.name}: ran and exited 0 — that is what `until` does when there is no "
              f"ready work. Next run in {plan.every}s")
        return 0
    code = "unknown" if done.state is None or done.state.last_exit is None \
        else done.state.last_exit
    print(f"{plan.name}: NOT running, last exit {code} — "
          f"{done.detail or 'the supervisor gave no reason'}", file=sys.stderr)
    print(f"        {plan.err_path} is where it said why", file=sys.stderr)
    return 1


def _service_status(state) -> None:
    """One drain, and the two facts that are invisible from anywhere else."""
    print(state.line())
    if state.kind == "systemd":
        # Printed whatever the answer, because "no" here is the difference between a service
        # that survives your logout and one that does not, and it is invisible from every
        # other reading.
        answer = {True: "yes", False: "NO — services stop at your last logout "
                                      "(`loginctl enable-linger`)",
                  None: "could not ask"}[state.linger]
        print(f"  linger: {answer}")
    if not state.installed:
        return
    if state.path_env:
        print(f"  PATH:   {state.path_env}")
    else:
        print("  PATH:   EMPTY — this service cannot find any vendor CLI")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_usage(sys.stderr)
        print("gbfleet: no command given. Try `gbfleet up --help`.", file=sys.stderr)
        return 2

    if args.command == "mcp":
        return _serve_stdio(args)

    if args.command == "doctor":
        # Not `report`: that name is the module-level wave printer, and binding it here made
        # it a local of ALL of main(), so `up`'s `report(wave)` below raised UnboundLocalError
        # after the wave had run, reaped and pushed. The report was the only thing lost,
        # which is the quietest way a supervisor can fail.
        findings = doctor.run(
            repo=Path(args.repo),
            workspace=Path(args.workspace) if args.workspace else None,
            adapter=args.adapter,
            server=args.server,
            api_key=os.environ.get(API_KEY_ENV),
            seats_file=args.seats_file,
            project=args.project,
            matrix_path=args.matrix or None,
        )
        # FAIL only. An UNKNOWN is loud in the report and does not stop a run — refusing
        # on a check that could not be made would ground the fleet on a slow network.
        return 0 if findings.ok else 1

    if args.command == "service":
        return _service(args)

    if args.command == "until":
        return _until(args)

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

    try:
        shared = _shared_servers(args)
        seats = [replace(s, shared=shared) for s in
                 read_seats(args.seats_file, args.server, api_key)]
    except ValueError as exc:
        print(f"gbfleet up: {exc}", file=sys.stderr)
        return 2
    if not seats:
        print(f"gbfleet up: no seats in {args.seats_file}", file=sys.stderr)
        return 2

    merger = None
    if args.merge:
        # The ONE widening, under the one flag that asks for it (GRPH-846).
        client = Graphban(base_url=args.server, api_key=api_key,
                          allowed=SPAWN_READS | MERGE_TOOLS, project_id=args.project)
        repo = Path(args.repo)
        remote = remote_for(repo)
        merger = Merger(repo, client, enabled=True, remote=remote,
                        base=default_ref(repo, remote) if remote else "")
    else:
        client = Graphban(base_url=args.server, api_key=api_key, allowed=SPAWN_READS, project_id=args.project)
    try:
        wave = up(
            Path(args.repo),
            seats,
            factory,
            client,
            merger=merger,
            wave_name=args.wave,
            limits=Limits(
                max_workers=args.max_workers,
                max_children=args.max_children,
                child_wall_clock=args.child_wall_clock,
                quiet_after=args.quiet_after,
            ),
            workspace=Path(args.workspace) if args.workspace else None,
            debug=args.debug,
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
