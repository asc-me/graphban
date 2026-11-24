"""The `gbfleet` command line entry point.

Commands land as the slices of PRD-22 do. `up` is here (GRPH-448); `stop`, `ps` and
`orphans` arrive with the stdio MCP server (GRPH-450).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from . import adopt as adopt_mod
from .adapters import ADAPTERS, AdapterError, Tuning, checked_tuning, resolve
from .client import ALLOWED_TOOLS, Graphban, NotPermitted, ObservationRefused, ServerUnreachable, ToolFailed
from . import doctor
from . import service as service_mod
from . import surface as surface_mod
from . import teardown as teardown_mod
from .lock import RepoLocked, describe_holder
from .seat import Seat, codes_from_text, parse_seat_line
from dataclasses import replace

from .spawn import Launch
from .state import NotARepository
from .lock import hold
from .mcp import Fleet, serve, read_preferences
from .state import repo_root
from .supervisor import DEFAULT_MAX_WORKERS, Limits, Merger, Wave, resolve_wave_base, up
from .tiers import TierTable
from . import matrix as matrix_mod
from .watchdog import STALL_AFTER_S
from .until import PLANNER_TOOLS, emit as emit_until, run as run_until
from .worktree import BaseBranchNotFound, Worktree, default_ref, remote_for

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
#: is a read. `release_item` is the GRPH-850 reap verb: mcp/`up` construct this
#: client and pass it to `_reap_exited`, and without it a wall-clock death left
#: the row `in_progress` so `choose_resume` skipped the salvage. The supervisor
#: table is still two reads; this set is the CLI/MCP process. The server still
#: bounds `release_item` by role (the dead child's worker id).
#:
#: `get_context` is the GRPH-1012 base read. Step 2 of the three rules that name the ref a
#: child is cut from is the project's MEASURED `gitops.base_branch`, and `get_context` is
#: the only way to ask for it. A read, like the other two named here — and `ALLOWED_TOOLS`
#: above stays the supervisor's own two, which `test_supervisor.py` pins.
#:
#: `get_item_details` is the GRPH-1053 landed-commit read. `up` and `mcp` watch with this
#: set; `until`'s planner client already had the read and `up` did not, so a reap on `up`
#: could not see a fix already in the trunk and pushed the rework onto the branch that fix
#: uses. A read, same as `search_items`. `ALLOWED_TOOLS` stays the supervisor's own two.
SPAWN_READS: frozenset[str] = ALLOWED_TOOLS | frozenset(
    {"search_items", "release_item", "get_context", "get_item_details",
     # GRPH-1424: the reap reads `build.generated_files` for the undeclared-file hold.
     "get_rule_toggles"})

#: What `up --merge` needs beyond `SPAWN_READS` (GRPH-846): the item and its attestations,
#: the dependency rows, and ONE write — the receipt naming the merge commit. Only under the
#: flag; a plain `up` keeps the two-reads-plus-search client it has always had. `update_item`
#: on a supervisor is the widening PRD-22 §4 warns about, and it is here because the operator
#: asked for the merge by name and the receipt is the half that makes the merge visible to
#: the ledger. The server still bounds what that call may write by role.
MERGE_TOOLS: frozenset[str] = frozenset({"get_item_details", "related_work", "update_item"})
#: `bench run` reads a reviewer's verdict off its scratch item and does nothing else there.
BENCH_VERDICT_READS: frozenset[str] = frozenset({"get_item_details"})

_MERGE_HELP = (
    "finish the merge for items whose fleet.sign_off attestation names a commit — the "
    "reviewer's sign-off is the handoff, not the moment this process watched the item "
    "leave review. Only when the PR head is the reviewed commit, CI attested suite_green "
    "on it, and the forge reports it MERGEABLE/CLEAN. Scoped to --prd or --line when given; hold "
    "deps bypass the scope. Default off (GRPH-846, GRPH-880)")



def _env_window() -> float | None:
    """`GBFLEET_REGISTRATION_WINDOW` as seconds, or None to use the adapter's own window
    (GRPH-1400). A value that is not a positive number is ignored rather than fatal: a bad
    environment variable should not stop a drain from starting."""
    raw = os.environ.get("GBFLEET_REGISTRATION_WINDOW", "").strip()
    try:
        value = float(raw) if raw else None
    except ValueError:
        return None
    return value if value and value > 0 else None

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
             "run. Unions with the repo's .gbfleet/servers. Sharing a server shares its "
             "credential")
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
    run.add_argument("--registration-window", type=float, default=_env_window(),
        help="seconds a child gets to register before it is killed as never_registered (default: the adapter's own window, 240s for claude, else 90s; env GBFLEET_REGISTRATION_WINDOW)")
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
        "--base", default=None, metavar="BRANCH",
        help="cut children from origin/BRANCH instead of the remote's default ref (GRPH-847). "
             "Refuses at startup when the branch does not exist on the remote",
    )
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

    surf = sub.add_parser(
        "surface",
        help="compare the skills and MCP servers each harness would hand a child",
        description=(
            "Reads what a child of each installed harness actually loads — skills and "
            "MCP server names, never URLs or credentials — and prints where the lists "
            "differ. With --server, --project and $GBFLEET_API_KEY, stores the report "
            "for the Harness page. A harness that could not be read is 'not checked', "
            "not an empty list. `gbfleet doctor` runs this same check."
        ),
    )
    surf.add_argument("--repo", default=".", help="repository whose project skills count (default: cwd)")
    surf.add_argument("--server", default="", help="Graphban base URL; with --project, store the report")
    surf.add_argument("--project", default="", help="project the Harness page should show this on")

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
             "run. Unions with the repo's .gbfleet/servers. Sharing a server shares its "
             "credential")
    stdio.add_argument(
        "--allow", action="append", default=[], metavar="NAME",
        help="let children run this command despite the default deny-list (e.g. psql for "
             "local container checks). Repeatable")
    stdio.add_argument(
        "--deny", action="append", default=[], metavar="NAME",
        help="add a command to the deny-list children get a refusing stub for. Repeatable")
    stdio.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    stdio.add_argument("--child-wall-clock", type=float, default=3600.0,
        help="per-child wall-clock cap in seconds (default 3600). A spawn can override per-child via child_wall_clock=...")
    stdio.add_argument("--registration-window", type=float, default=_env_window(),
        help="seconds a child gets to register before it is killed as never_registered (default: the adapter's own window, 240s for claude, else 90s; env GBFLEET_REGISTRATION_WINDOW)")
    stdio.add_argument(
        "--tier", action="append", default=[], metavar="NAME=ADAPTER[:MODEL]",
        help="what a tier means on this machine, e.g. cheap=gbagent:qwen3.6:35b-a3b-coding-mtp-det "
             "or frontier=claude:opus; repeatable. spawn(tier=...) resolves through it (PRD-36). "
             "Fixed for the life of the process",
    )

    svc = sub.add_parser(
        "service",
        help="run a drain under launchd / systemd --user / Task Scheduler, so it outlives "
             "your terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Installs `gbfleet until` as a user-domain service. Everything after the "
            "subcommand is passed to `until` unchanged, so `gbfleet until --help` stays the "
            "authority on its own flags. `install --name X` with no arguments after it "
            "reloads the unit already written for X — the command `status` prints when "
            "that unit is installed but not loaded — and refuses when no such unit exists.\n\n"
            "The API key is read from $" + API_KEY_ENV + " at install time and written to an "
            "owner-only file the unit references. It is NEVER put in the unit itself: a unit "
            "file is world-readable.\n\n"
            "User domain only — LaunchAgent, systemd --user, or Task Scheduler (schtasks) on "
            "Windows. A root / Session-0 installer is a different program with different "
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
        "--subagents", type=int, default=None, metavar="N",
        help="tell each child it may start at most N vendor subagents (0 = none). Written "
             "into the instruction only; omitted leaves the instruction unchanged",
    )
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
        "--line", nargs="?", const="", default=None, metavar="LINE",
        help="drain the project's maintenance line (GRPH-1697): claims, reviews and exits on "
             "line items only. Bare `--line` is the project's line; a line id names it. "
             "Exclusive with --prd. Runs in its own clone: one supervisor lock per checkout, "
             "so do not start it on a checkout a --prd drain holds")
    until.add_argument(
        "--mcp-server", action="append", default=[], metavar="NAME",
        help="share one of YOUR MCP servers with each child, by exact name (e.g. context7). "
             "Repeatable. Exact names only — no patterns — and an unknown name refuses the "
             "run. Unions with the repo's .gbfleet/servers. Sharing a server shares its "
             "credential")
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
    until.add_argument(
        "--acknowledge-reach", default=None, metavar="ITEM[,ITEM]",
        help="pass acknowledge_reach=true on the delegate call for these items only "
             "(comma-separated). A repo-only item whose text names credential UI is refused "
             "until the delegator says it read those lines; items not named are unchanged, "
             "and a run without this flag sends no acknowledge_reach (GRPH-1311). The dry "
             "run lists the same ids and does not delegate")
    until.add_argument(
        "--entitlement", action="append", default=[], metavar="ADAPTER=ID",
        help="an entitlement id the adapter runs on (PRD-62); repeatable. The wave declares it "
             "at register_agent and reports spawn_start, registered, limit_hit and spawn_failed "
             "for it. Off a capacity-routing plan the wave runs as it does without the flag",
    )
    until.add_argument("--max-children", type=int, default=8)
    until.add_argument("--child-wall-clock", type=float, default=3600.0)
    until.add_argument("--registration-window", type=float, default=_env_window(),
        help="seconds a child gets to register before it is killed as never_registered (default: the adapter's own window, 240s for claude, else 90s; env GBFLEET_REGISTRATION_WINDOW)")
    until.add_argument("--workspace", default=None, help="where worktrees go")
    until.add_argument("--debug", action="store_true")
    until.add_argument(
        "--quiet-after", type=float, default=Limits.quiet_after,
        help="seconds of no output before a child is REPORTED as quiet",
    )
    until.add_argument(
        "--stall-after", type=float, default=STALL_AFTER_S, metavar="SECONDS",
        help="exit non-zero with a `stall` event when nothing has progressed for this long "
             "and no child is live, so the service manager restarts the process; 0 disables "
             "(GRPH-1179). `kill -USR1 <pid>` dumps every thread's stack to the log",
    )
    until.add_argument("--merge", action="store_true", default=False, help=_MERGE_HELP)
    until.add_argument(
        "--base", default=None, metavar="BRANCH",
        help="cut children from origin/BRANCH instead of the remote's default ref, and "
             "target PRs at that branch. For PRDs whose slices are sequential (S1→S2→S3): "
             "each slice lands on the integration branch and unblocks the next. Refuses at "
             "startup when the branch does not exist on the remote (GRPH-847)",
    )
    until.add_argument(
        "argv", nargs=argparse.REMAINDER, help="-- followed by the command to run per child"
    )

    td = sub.add_parser(
        "teardown",
        help="remove a FINISHED drain: service, key file, seats, worktrees, workspace, clone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Reads the clone, server, PRD and wave out of the installed unit, then refuses —\n"
            "naming each cause and deleting nothing — while the PRD has next, backlog,\n"
            "in_progress or review items, a PR from one of the clone's branches is still open,\n"
            "the service is mid-run, a local branch has a commit on no remote, the clone or\n"
            "any registered worktree (inside or outside <clone>-gbfleet/) has uncommitted\n"
            "work, a process has the clone open, or the repository lock is held. Unknown is a\n"
            "refusal too. `.gbfleet/servers` and uv.lock drift do not block.\n\n"
            "There is no --force: push or discard, then run it again.\n\n"
            "Then, in order: uninstall the service (unit + key file), retire the wave's seats,\n"
            "reap every registered worktree, delete <clone>-gbfleet/ and the clone."
        ),
    )
    td.add_argument("--name", required=True, help="the service name the drain was installed as")
    td.add_argument("--prd", default="",
                    help="the PRD that must be finished (default: the drain's own --prd)")
    td.add_argument("--dry-run", action="store_true",
                    help="print the plan and every refusal; change nothing")
    td.add_argument("--keep-clone", action="store_true",
                    help="do everything except delete the clone itself")

    chk = sub.add_parser(
        "check-touchpoints",
        help="run the PR touchpoint gate from inside a checkout, before moving an item to review",
        description=(
            "Lists files this checkout changed since the trunk that no declared touchpoint covers\n"
            "(the supervisor withholds the PR for those) and prints the update_item call that\n"
            "declares them. A file the project's build.generated_files rule ignores is not\n"
            "listed; while that rule is observing, the built-in list is used, matching the\n"
            "supervisor. With no seat to read the rule from, the built-in list is used and\n"
            "the output says the project's rule was not read. Exit 1 when any are outside;\n"
            "0 when the gate would pass."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    chk.add_argument("--declared", nargs="*", default=[],
                     help="the item's declared touchpoints (get_item_details)")
    chk.add_argument("--item", default="", help="the item id, for the printed update_item call")
    chk.add_argument("--path", default=".", help="the checkout to measure (default: here)")

    ent = sub.add_parser(
        "entitlement",
        help="register the accounts this machine can launch (add, list, remove)",
        description=(
            "A local file outside any worktree: name, vendor, kind and launch handle per record,\n"
            "and no secret field. For api_key the handle is an environment-variable NAME."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ent_do = ent.add_subparsers(dest="act", metavar="ACT")
    ent_add = ent_do.add_parser("add", help="add a record")
    ent_add.add_argument("--json", default="", help="the record as a JSON object (overrides flags)")
    ent_add.add_argument("--name", default="")
    ent_add.add_argument("--vendor", default="")
    ent_add.add_argument("--kind", default="")
    ent_add.add_argument("--handle", default="")
    ent_do.add_parser("list", help="print name, vendor, kind and handle")
    ent_rm = ent_do.add_parser("remove", help="delete one record, leaving the others")
    ent_rm.add_argument("name")

    bench = sub.add_parser("bench", help="bench suites: manifest checks (validate)")
    bench_do = bench.add_subparsers(dest="act", metavar="ACT")
    bench_val = bench_do.add_parser(
        "validate",
        help="check that every task in a suite manifest discriminates",
        description=(
            "Build task: the check must fail at base_sha and pass at the reference solution.\n"
            "Review task: the planted-defect ref must make a check red, the clean ref green.\n"
            "One line per task; exit 1 if any task does not discriminate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bench_val.add_argument("manifest", help="the suite manifest (TOML)")
    bench_val.add_argument("--repo", default=None,
                           help="the suite's git repository (default: here; for the shipped "
                                "template, a fixture repo built for this check)")
    bench_do.add_parser("list", help="name the shipped template suite, its version and task count")
    bench_har = bench_do.add_parser(
        "harvest",
        help="propose candidate tasks from the repo's merge history (writes nothing to the server)",
        description="Each merge's added or modified test files run 3 times at the first parent "
                    "(all red) and 3 times at the merge (all green). Dropped merges are listed "
                    "with flaky, green_at_parent, red_at_merge or no_tests.",
    )
    bench_har.add_argument("--repo", default=".", help="the git repository (default: here)")
    bench_har.add_argument("--branch", default="HEAD", help="the default branch to scan")
    bench_har.add_argument("--since", default="180d", help="how far back, e.g. 180d, 4w")
    bench_har.add_argument("--limit", type=int, default=50, help="most merges to scan")
    bench_har.add_argument("--sabotage-items", default="",
                           help="comma-separated item ids with a red sabotage: ranked first")
    bench_cmp = bench_do.add_parser("compare", help="show two baseline runs side by side")
    bench_cmp.add_argument("runs", nargs=2, metavar="RUN", help="two run ids (or record ids)")
    bench_cmp.add_argument("--server", required=True, help="Graphban base URL")
    bench_cmp.add_argument("--project", required=True, help="project id")
    bench_ad = bench_do.add_parser("adopt", help="post chosen harvested candidates to the repo suite")
    bench_ad.add_argument("ids", nargs="+", help="candidate ids from `bench harvest`")
    bench_ad.add_argument("--repo", default=".", help="the git repository (default: here)")
    bench_ad.add_argument("--server", required=True, help="Graphban base URL")
    bench_ad.add_argument("--project", required=True, help="project id")
    bench_ad.add_argument("--capability", default="B7", help="PRD-41 capability leaf for the tasks")
    bench_ad.add_argument("--expected-version", type=int, default=None,
                          help="suite version to write against (default: read the current one)")
    bench_run = bench_do.add_parser(
        "run", help="run an adapter against a bench suite and post the baseline",
        description="Per build task: a worktree at base_sha with the check's tests held out, "
                    "a child under ordinary supervision, then the check with the tests restored.",
    )
    bench_run.add_argument("--suite", choices=("template", "repo", "all"), required=True)
    bench_run.add_argument("--adapter", required=True, help="vendor[:model]")
    bench_run.add_argument("--dry-run", action="store_true",
                           help="list tasks, adapter, model and estimated spend; spawn nothing")
    bench_run.add_argument("--repo", default=".", help="the git repository (default: here)")
    bench_run.add_argument("--server", required=True, help="Graphban base URL")
    bench_run.add_argument("--project", required=True, help="project id")
    bench_run.add_argument("--binary", default=None, help="path to the vendor binary")
    bench_run.add_argument("--workspace", default=None, help="where worktrees go")
    bench_run.add_argument("--child-wall-clock", type=float, default=3600.0)
    bench_run.add_argument("--registration-window", type=float, default=None)

    harn = sub.add_parser("harness", help="read the Harness page's numbers (grid)")
    harn_do = harn.add_subparsers(dest="act", metavar="ACT")
    harn_grid = harn_do.add_parser(
        "grid",
        help="print the grid's cells with n, layer and the baseline column",
        description="Reads the project's Harness grid with $GBFLEET_API_KEY. The baseline is "
                    "its own column: never pooled with the natural or probe rate.",
    )
    harn_grid.add_argument("--server", required=True, help="Graphban base URL")
    harn_grid.add_argument("--project", required=True, help="project id")

    sub.add_parser(
        "gc",
        help="reap the child worktrees of repositories whose supervisor is gone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Walks the state directory rather than one --repo, because the trees that need\n"
            "this belong to the clone nobody starts a supervisor on again: reap runs inside\n"
            "the supervisor that owns a repository, and a clone that is never re-opened keeps\n"
            "every worktree it ever cut.\n\n"
            "Where a repository's lock is NOT held, every recorded worktree whose pid is dead\n"
            "is reaped — through the same reap a supervisor runs, so removal is never forced.\n"
            "A tree reap will not remove is printed and left IN THE RECORD, so the next pass\n"
            "still sees it. A repository whose lock is held is not touched at all, and no\n"
            "checkout is ever deleted: that checkout is another agent's supervisor, and one\n"
            "lock per repository is what lets two agents work at once.\n\n"
            "Exits 1 when something is still on disk this pass could not remove, so a\n"
            "scheduled gc is an alarm rather than a no-op. `up`, `until` and `mcp` run the\n"
            "same sweep at startup; this is for reading it on its own."
        ),
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


def with_foreign_variables_stripped(launch, entitlement, records=None):
    """Strip every OTHER stored record's API-key variable from what the child inherits."""
    from . import entitlements as ent_mod

    records = ent_mod.load() if records is None else records
    foreign = ent_mod.foreign_variables(entitlement, records)
    keep = set(launch.env)  # a variable the selection itself set is never stripped
    unset = sorted({*launch.env_unset, *(v for v in foreign if v not in keep)})
    return replace(launch, env_unset=tuple(unset))


def make_adapter_factory(name: str, binary: str | None, model: str = "",
                         tuning: "Tuning | None" = None, entitlement=None):
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
        if entitlement is not None:
            # Selects the record's handle or refuses; never falls back to the default login.
            launch = found.adapter.select_entitlement(launch, entitlement)
            launch = with_foreign_variables_stripped(launch, entitlement)
        return replace(launch, binary_version=found.version)

    # The version the supervisor stamps on natural attempts; `bench run` posts the same one.
    factory.binary_version = found.version
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

    # WHAT THE CHILDREN WERE CUT FROM, and which of the three rules named it (GRPH-1012).
    # First, because everything below is a diff against this ref — and because the two
    # readings of `origin/main` are different facts an operator acts on differently: the
    # project measured that as its trunk, or nobody measured anything and it is merely the
    # remote's default. `source` is printed alongside so the note cannot be read as one
    # when it says the other.
    if wave.base is not None:
        print(f"BASE {wave.base.describe()} [{wave.base.source}]", file=out)
    elif wave.spawned:
        # Children were cut and nothing records the base they were cut from. Not printed for
        # a wave that spawned nothing: there was no cut to explain, and a line that claims
        # otherwise is noise an operator learns to skip past.
        print("BASE unrecorded: this wave cut children without resolving a base", file=out)

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
    for taken in wave.remote_taken:
        print(f"BRANCH TAKEN {taken}", file=out)
    print(f"spawned {len(wave.spawned)}", file=out)
    # GRPH-1024. Always, zero included. `spawned 0` beside `HELD IN BASE 0` is nothing
    # to do. `spawned 0` beside a count is the queue that used to exit idle with no line.
    print(f"HELD IN BASE {len(wave.in_base)}", file=out)
    for item_id, why in sorted(wave.in_base.items()):
        print(f"  {item_id}: {why}", file=out)
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
    # GRPH-1025: which declaration each undeclared check compared against. Normally the item
    # as it stood at reap; a branch named here used the delegation-time snapshot, which a
    # child that widened its own touchpoints would fail through no fault of its own.
    for branch, why in sorted(wave.declared_unread.items()):
        print(f"DECLARATION unread {branch}: compared against delegation-time snapshot ({why})",
              file=out)
    # HOW STALE the reviewer's diff is (GRPH-786). The supervisor is the only party that
    # can answer it: the server has no git, and the reviewer sees a branch with no
    # indication of what it is a diff against. When the missed commits share no file
    # with the branch and the merge is clean, the line says so (GRPH-1087) — the same
    # sentence that goes in the PR body. A behind branch with no such note was refused.
    for branch, (behind, ref) in sorted(wave.stale.items()):
        line = f"BEHIND {branch}: cut from a base {behind} commit(s) behind {ref}"
        note = wave.behind_clean.get(branch)
        if note:
            line += f"; {note}"
        print(line, file=out)
    if wave.stale_unmeasured:
        print(f"BEHIND unmeasured: {wave.stale_unmeasured}", file=out)
    # GRPH-1053. A push that could not check whether a commit of the item is already in
    # the trunk is not "nothing had landed". The field was stored and the report never
    # mentioned it, so the wave read as a clean publish.
    if wave.landed_unmeasured:
        print(f"LANDED unmeasured: {wave.landed_unmeasured}", file=out)
    # THE REVIEWER'S OTHER PRECONDITION (GRPH-987). `BEHIND` says the diff is against a stale
    # world; this says there was no diff to read at all — an item in `review` whose branch never
    # reached the remote. A bounce for that is not a verdict on the work, but it is recorded as
    # one against the builder's vendor and model, so the exposure is printed where an operator
    # can see it rather than left to be inferred from the bounce_reason.
    for branch, items in sorted(wave.review_unreadable.items()):
        print(f"REVIEW UNREADABLE {branch}: {', '.join(items)} in review, branch not on the "
              f"remote — a bounce here is not the builder's", file=out)
    if wave.review_unmeasured:
        print(f"REVIEW UNREADABLE unmeasured: {wave.review_unmeasured}", file=out)
    for branch, (asked, ran) in sorted(wave.substituted.items()):
        print(f"SUBSTITUTED {branch}: asked for {asked}, {ran} answered — the measured cell "
              f"belongs to {ran}", file=out)
    for line in wave.lease_moved:
        print(f"LEASE MOVED {line}", file=out)
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
    # One line per seat: the count cannot say which enrolment, and a wave that
    # stopped can have a different reason than a full machine.
    for line in wave.unredeemed:
        print(f"UNREDEEMED {line}", file=out)
    for gate in wave.gated:
        print(f"NO ROOM {gate}", file=out)
    for failure in wave.failures:
        print(f"FAILED {failure}", file=out)
    for give_up in wave.give_ups:
        print(f"STUCK {give_up}", file=out)
    # WHAT BECAME OF EACH MERGE (GRPH-846). Four lines for four outcomes, because a merge
    # the forge refused, a merge that is armed and waiting, and an item left alone because
    # its head moved are different facts a person acts on differently.
    # GRPH-P57 D1: every line names the release ref it used, so drift between boxes shows.
    for item_id, got in sorted(wave.merged.items()):
        ref = f" [{got.release}]" if got.release else ""
        if got.ok:
            print(f"MERGED {item_id}: {got.commit[:12]} {got.url}".rstrip() + ref, file=out)
        elif got.pending:
            print(f"MERGE PENDING {item_id}: {got.reason}{ref}", file=out)
        elif got.skipped:
            print(f"MERGE SKIPPED {item_id}: {got.reason}{ref}", file=out)
        else:
            print(f"MERGE HELD {item_id}: {got.reason}"
                  + (f" (checked: {', '.join(got.checked)})" if got.checked else "") + ref,
                  file=out)


def _acknowledge_reach(text: str | None) -> set[str] | None:
    """`--acknowledge-reach ITEM[,ITEM]` as a set of item ids (GRPH-1311).

    None when the flag was not given, which is what keeps the delegate call unchanged.
    A flag that names nothing is an error: it would otherwise be indistinguishable from
    omitting the flag.
    """
    if text is None:
        return None
    ids = {part.strip() for part in str(text).split(",") if part.strip()}
    if not ids:
        raise ValueError("--acknowledge-reach needs one or more item ids")
    return ids


def _until(args) -> int:
    """Planner loop. Two clients, one key: minting never goes through ALLOWED_TOOLS."""
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"gbfleet until: ${API_KEY_ENV} is not set", file=sys.stderr)
        return 2

    if args.line is not None and args.prd:
        print("gbfleet until: --prd and --line cannot be combined", file=sys.stderr)
        return 2
    if args.subagents is not None and args.subagents < 0:
        print("gbfleet until: --subagents must be 0 or more", file=sys.stderr)
        return 2
    try:
        acknowledge = _acknowledge_reach(args.acknowledge_reach)
    except ValueError as exc:
        print(f"gbfleet until: {exc}", file=sys.stderr)
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

    entitlements: dict[str, list[str]] = {}
    for pair in args.entitlement or []:
        adapter_name, _, ent_id = pair.partition("=")
        if not adapter_name.strip() or not ent_id.strip():
            print(f"gbfleet until: --entitlement wants ADAPTER=ID, got {pair!r}", file=sys.stderr)
            return 2
        entitlements.setdefault(adapter_name.strip(), []).append(ent_id.strip())

    planner = Graphban(base_url=args.server, api_key=api_key, allowed=PLANNER_TOOLS, project_id=args.project)
    supervisor = Graphban(base_url=args.server, api_key=api_key, allowed=ALLOWED_TOOLS, project_id=args.project)
    if args.dry_run:
        # BEFORE the lock and before anything is minted (GRPH-819). A dry run that acquired the
        # repository would stop being one, and a planner asking "what would this take?" while a
        # wave runs is exactly who needs to ask.
        from . import until as until_mod

        # GRPH-1348. A real run refuses this key in `_preflight_observations`. A dry run
        # used to say nothing, so `service install` stored a key that cannot record a cut.
        # The warning does not change the exit: the plan is still the answer.
        _print_observations_warning(until_mod.observations_role_warning(supervisor))
        try:
            # GRPH-950: the base the loop would measure dependencies against, resolved the way
            # the loop resolves it but without the fetch — a dry run changes nothing, refs too.
            repo = Path(args.repo)
            remote = remote_for(repo)
            dry_base = ""
            if args.base and remote:
                from .worktree import resolve_base, BaseBranchNotFound
                try:
                    dry_base = resolve_base(repo, remote, args.base)
                except BaseBranchNotFound:
                    dry_base = ""
            elif remote:
                dry_base = default_ref(repo, remote)
            got = until_mod.plan(planner, args.prd or None, args.max_workers,
                                 repo=repo, base=dry_base, line=args.line,
                                 acknowledge_reach=acknowledge)
        except until_mod.ConfigError as exc:
            print(f"gbfleet until --dry-run: {exc}", file=sys.stderr)
            return 2
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
        for row in got.get("dependency_holds") or []:
            print(f"{row['item']} held: {row['waiting_on']}", file=sys.stderr)
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
                registration_window=args.registration_window,
                subagents=args.subagents,
            ),
            workspace=Path(args.workspace) if args.workspace else None,
            debug=args.debug,
            request=args.request,
            prd=args.prd or None,
            line=args.line,
            budget=args.budget or None,
            shared=_shared_servers(args),
            tiers=tiers,
            launch_for=lambda name, model="", entitlement=None: make_adapter_factory(
                name, None, model, entitlement=entitlement),
            matrix=matrix_mod.load(Path(args.matrix)) if args.matrix else matrix_mod.load(),
            merge=bool(args.merge),
            base_branch=args.base,
            entitlements=entitlements,
            stall_after=args.stall_after,
            acknowledge_reach=acknowledge,
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

    # GRPH-928: when a user-level MCP config outranks the repo's .mcp.json, this process
    # starts with --repo pointing at a DIFFERENT repository than the session's cwd. The
    # planner then sees fleet tools for the wrong project and believes they work. Refuse
    # rather than silently serve the wrong repo — the fix is to remove the shadowing entry
    # from the user config, not to guess which repo was meant.
    try:
        cwd_root = repo_root(Path.cwd())
    except NotARepository:
        cwd_root = None
    if cwd_root is not None and root.resolve() != cwd_root.resolve():
        print(
            f"gbfleet mcp: --repo {root} does not match this session's cwd ({Path.cwd()}).\n"
            f"     A user-level MCP config (~/.claude.json or ~/.grok/config.toml) is likely "
            f"shadowing the repo's .mcp.json.\n"
            f"     Remove the gbfleet entry for {root.name} from the user config, or start "
            f"this session from inside {root}.",
            file=sys.stderr,
        )
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
                limits=Limits(max_workers=args.max_workers, child_wall_clock=args.child_wall_clock,
                          registration_window=args.registration_window),
                tiers=tiers,
                matrix=matrix_mod.load(Path(args.matrix)) if args.matrix else matrix_mod.load(),
                shared=_shared_servers(args),
            )
            fleet.profile, fleet.policy, pref_note, fleet.measured, fleet.cap_measured, tier_map = read_preferences(client)
            print(f"gbfleet mcp: {pref_note}", file=sys.stderr)
            _apply_tier_map(fleet, tier_map)
            # GRPH-1012: the ref every child this process cuts, resolved ONCE here and
            # fetched, so `spawn` cannot inherit whatever branch this checkout happens to be
            # standing on. A base the remote does not have is printed and left UNRESOLVED
            # rather than exiting: the MCP client has already opened a handshake and dying
            # here breaks it (GRPH-881), while `spawn` then refuses as a tool error naming
            # it — which is where a planner can actually read the reason.
            try:
                fleet.base = resolve_wave_base(root, client)
                print(f"gbfleet mcp: base {fleet.base.describe()}", file=sys.stderr)
            except BaseBranchNotFound as exc:
                print(f"gbfleet mcp: base unresolved — {exc}", file=sys.stderr)
            # GRPH-1011. The same sweep `gc` runs, over EVERY repository this state
            # directory names rather than only this one — the trees that need it belong to
            # the clone nobody starts a supervisor on again. Inside the lock, so this
            # repository answers "held" to the sweep's own try-acquire and is skipped by the
            # rule that protects every other live supervisor.
            for line in adopt_mod.sweep().lines:
                print(f"gbfleet mcp: gc: {line}", file=sys.stderr)
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
        # GRPH-881: instead of exiting, attach read-only. The MCP client (Grok) auto-starts
        # this process at session start; exiting before initialize breaks the handshake.
        # A planner holding `mcp` must still see the running wave and be able to stop children,
        # while spawn refuses as a tool error naming the holder.
        return _serve_attached(root, workspace, client, exc, args)
    finally:
        client.close()
    return 0


def _serve_attached(root: Path, workspace: Path, client: Graphban,
                    exc: RepoLocked, args) -> int:
    """Serve MCP read-only, attached to the holder's children (GRPH-881).

    The lock is held by another supervisor. We do NOT take it. We read the holder's
    children.json, attach to live pids, and serve ps/stop/orphans while spawn refuses.
    """
    holder = exc.holder
    who = describe_holder(holder) if holder else "unknown holder"
    print(f"gbfleet mcp: attached read-only to {root} held by {who}", file=sys.stderr)

    try:
        tiers = TierTable.parse(args.tier)
    except ValueError as exc_tier:
        print(f"gbfleet mcp: {exc_tier}", file=sys.stderr)
        return 2

    children = _load_holders_children(root)
    print(f"gbfleet mcp: observing {len(children)} live child(ren)", file=sys.stderr)

    fleet = Fleet(
        repo=root,
        workspace=workspace,
        client=client,
        launch_for=lambda name, model="", tuning=None: make_adapter_factory(name, None, model, tuning),
        lock=None,
        limits=Limits(max_workers=args.max_workers, child_wall_clock=args.child_wall_clock,
                          registration_window=args.registration_window),
        tiers=tiers,
        matrix=matrix_mod.load(Path(args.matrix)) if args.matrix else matrix_mod.load(),
        shared=_shared_servers(args),
        attached_holder=holder,
        # GRPH-1011: attach mode is STATED here, not read off `holder`. `holder` is None
        # whenever the lock file was empty, and an empty lock file is not an unheld one —
        # a gc sweep holds the flock for the length of its pass and deliberately writes no
        # record (see `lock.sweep_hold`). Inferring read-only from a parseable holder let an
        # `mcp` started during any sweep serve as a FULL supervisor without the lock.
        attached=True,
    )
    fleet.children.extend(children)
    fleet.profile, fleet.policy, pref_note, fleet.measured, fleet.cap_measured, tier_map = read_preferences(client)
    print(f"gbfleet mcp: {pref_note}", file=sys.stderr)
    _apply_tier_map(fleet, tier_map)
    serve(fleet)
    return 0


def _load_holders_children(root: Path) -> list:
    """Read the holder's children.json and attach to live pids. Dead children are skipped.

    Does NOT salvage: we are observers, not owners. The holder manages those trees.
    """
    path = adopt_mod.children_path(root)
    loaded = adopt_mod.load(path)
    if isinstance(loaded, adopt_mod.UnadoptableFile):
        return []
    attached = []
    for snap in loaded:
        verdict = adopt_mod.classify(snap)
        if verdict.fate == "attached":
            try:
                attached.append(adopt_mod.attach(snap))
            except OSError:
                pass
    return attached


def _apply_tier_map(fleet: Fleet, tier_map) -> None:
    """Layer this deployment's tier map onto the packaged matrix, once, at wave start.

    Fixed for the life of the process like the tier table beside it (PRD-36 D16): a map that
    could change mid-wave would make "what did that child run" unanswerable from the record
    afterwards. An unreachable server leaves the packaged matrix in place — `read_preferences`
    already said so in the note printed beside this call, and `tier_map.reachable` keeps the
    two states apart for anything that reads the fleet later.
    """
    fleet.tier_map = tier_map
    if fleet.matrix is None:
        return
    fleet.matrix, notes = tier_map.apply(fleet.matrix)
    for note in notes:
        print(f"gbfleet mcp: {note}", file=sys.stderr)


def _shared_servers(args) -> dict:
    """The servers named with `--mcp-server`, resolved before anything is spawned (GRPH-816).

    Resolved HERE rather than per child so a typo refuses the run instead of the fourth
    worktree, and so the refusal reaches a terminal rather than a child's stderr.

    Unions with the project's `.gbfleet/servers` (GRPH-998), read from the repository ROOT:
    `--repo` may be a subdirectory, and reading beside it would find nothing and grant nothing
    — a mis-pointed flag passing as a project that listed no servers.
    """
    from . import mcpshare

    try:
        root = repo_root(Path(getattr(args, "repo", ".") or "."))
    except NotARepository as exc:
        print(f"gbfleet: {exc}", file=sys.stderr)
        raise SystemExit(2)
    try:
        return mcpshare.grants(list(getattr(args, "mcp_server", []) or []), root)
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
        code = 0
        for one in names:
            state = service_mod.status(one, kind=found.kind)
            _service_status(state)
            # A unit file with no job will not restart. Exit 0 here read as a
            # drain between cycles (GRPH-1852). Any other not-running state
            # still exits 0: last exit is the signal, and idle is success.
            if state.not_loaded:
                code = 1
        return code

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
    # No until arguments is not "nothing to run" when that name already has a unit.
    # Status tells the operator to run exactly `install --name X`, and that command
    # has to load the unit as written (GRPH-1872). A name with no unit still falls
    # through to the refusal inside this branch.
    if not rest:
        return _reload_named_service(args)
    try:
        plan = service_mod.make_plan(rest, name=args.name, every=args.every)
    except service_mod.Refused as exc:
        print(f"gbfleet service: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        _print_observations_warning(_observations_warning_for_plan(plan))
        print(f"# would write {plan.unit_path}")
        print(f"# and {plan.env_path} (owner-only, ${service_mod.API_KEY_ENV})")
        print(service_mod.render(plan).decode("utf-8", "replace"))
        return 0

    try:
        done = service_mod.install(plan, os.environ.get(API_KEY_ENV, ""))
    except service_mod.Refused as exc:
        print(f"gbfleet service: {exc}", file=sys.stderr)
        return 2
    role = _observations_warning_for_plan(plan)
    if role:
        done.warnings.append(role)

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
    if done.state is not None and done.state.unsettled:
        # GRPH-1698: exited 0 but not idle — unready line work, or no reason in the log yet.
        # Not idle, and not a crash either: say which, and do not fail the install.
        print(f"{done.state.line()}. Next run in {plan.every}s")
        return 0
    code = "unknown" if done.state is None or done.state.last_exit is None \
        else done.state.last_exit
    # GRPH-1312: the err log's last line is the drain's own reason; the loader's output
    # (`done.detail`) is launchctl/systemctl talking, and usually says nothing.
    why = ((done.state.reason if done.state is not None else "")
           or service_mod.err_reason(plan.name) or done.detail
           or "the supervisor gave no reason")
    print(f"{plan.name}: NOT running, last exit {code} — {why}", file=sys.stderr)
    print(f"        {plan.err_path} is where it said why", file=sys.stderr)
    return 1


def _reload_named_service(args) -> int:
    """`install --name X` with no `until` arguments: load the unit already on disk.

    Does not render a new unit and does not read a key. Both already belong to the
    file this command is loading; rewriting either would need the arguments the
    operator was not given.
    """
    try:
        if args.dry_run:
            path = service_mod.installed_unit_path(args.name)
            print(f"# would load {path} without rewriting it")
            return 0
        state = service_mod.reload_installed(args.name)
    except service_mod.Refused as exc:
        print(f"gbfleet service: {exc}", file=sys.stderr)
        return 2
    print(f"loaded {state.unit_path} (existing arguments unchanged)")
    _service_status(state)
    # Still not loaded means the supervisor did not take the unit. A job we could
    # not ask about is the same failure: reporting success would hide it.
    if state.not_loaded or state.running is None:
        return 1
    if state.running or state.idle or state.unsettled:
        return 0
    return 1


def _print_observations_warning(text: str) -> None:
    if text:
        print(f"WARNING {text}", file=sys.stderr)


def _observations_warning_for_plan(plan) -> str:
    """Warn when the key this unit would store cannot record observations (GRPH-1348).

    No server or no key is "", not a warning: the probe cannot be asked, and a warning
    for a flag the operator did not pass would be a guess. The probe stays out of
    `service.install` so a unit test that writes a plan does not dial the network.
    """
    from .until import observations_role_warning

    server = service_mod._value_of(plan.until_args, "--server")
    key = os.environ.get(API_KEY_ENV, "")
    if not server or not key:
        return ""
    project = service_mod._value_of(plan.until_args, "--project")
    client = Graphban(base_url=server, api_key=key, project_id=project or "")
    return observations_role_warning(client)


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
    if state.kind == "schtasks" and state.logon_policy:
        # Named, not guessed (GRPH-852): InteractiveToken ≈ LaunchAgent.
        print(f"  logon:  {state.logon_policy}")
    if not state.installed:
        return
    if state.path_env:
        print(f"  PATH:   {state.path_env}")
    else:
        print("  PATH:   EMPTY — this service cannot find any vendor CLI")


def _surface(args) -> int:
    """Print the discrepancy report, and store it when a project was named."""
    inventory = surface_mod.scan(Path(args.repo))
    print(surface_mod.render(inventory), end="")
    api_key = os.environ.get(API_KEY_ENV)
    if not (args.server and api_key and args.project):
        print(
            "\nnot stored. Pass --server, --project and set "
            f"${API_KEY_ENV} to put this on the Harness page.",
            file=sys.stderr,
        )
        return 0
    posted = Graphban(args.server, api_key, project_id=args.project).post_surface(
        project_id=args.project, host=surface_mod.host_name(), harnesses=inventory["harnesses"],
    )
    if posted is None:
        print("\nnot stored: the ledger did not accept the report", file=sys.stderr)
        return 1
    print(f"\nstored for {args.project} from {surface_mod.host_name()}")
    return 0


#: GRPH-1823. Printed when check-touchpoints could not read `build.generated_files`
#: and fell back to the built-in list. The supervisor logs the same fact; this command
#: is the only place the child looks, so a missing sentence would read as "the project
#: allows exactly these files".
RULE_UNREAD = "the project's generated-files rule was not read"

#: Seat files that carry the graphban URL and key inside the checkout. Claude's seat
#: is a temp file outside the tree; with neither URL nor key here the command says
#: the rule was not read rather than guessing a server.
_SEAT_SPECS = (
    (".grok/config.toml", "toml"),
    (".cursor/mcp.json", "json"),
    (".grok/mcp.json", "json"),
    (".agents/mcp_config.json", "json"),
)


def _base_of_mcp(url: str) -> str:
    """The Graphban base URL. A seat stores the MCP endpoint, which already ends in `/api/mcp`."""
    url = url.strip().rstrip("/")
    suffix = "/api/mcp"
    if url.endswith(suffix):
        url = url[: -len(suffix)].rstrip("/")
    return url


def _header_key(headers: object) -> str:
    if not isinstance(headers, dict):
        return ""
    for name, value in headers.items():
        if str(name).lower() == "x-api-key" and isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pair_from_spec(spec: object) -> tuple[str, str] | None:
    if not isinstance(spec, dict):
        return None
    # Antigravity's seat (`.agents/mcp_config.json`) writes `serverUrl` and the
    # binary refuses `url` / `httpUrl`. Skipping `serverUrl` returns no pair for
    # that file, and check-touchpoints then holds a generated file the supervisor
    # lets through.
    raw = spec.get("serverUrl") or spec.get("url") or spec.get("httpUrl") or ""
    if not isinstance(raw, str) or not raw.strip().startswith(("http://", "https://")):
        return None
    key = _header_key(spec.get("headers"))
    base = _base_of_mcp(raw)
    if not key or not base:
        return None
    return base, key


def _credentials_in(doc: object, *, toml: bool) -> tuple[str, str] | None:
    if not isinstance(doc, dict):
        return None
    servers = doc.get("mcp_servers") if toml else (doc.get("mcpServers") or doc.get("mcp_servers"))
    if not isinstance(servers, dict):
        return None
    # The seat's own entry. A shared server can also send an API-key header, and
    # using that would read some other project's rule — or none.
    return _pair_from_spec(servers.get("graphban"))


def seat_credentials(checkout: Path) -> tuple[str, str] | None:
    """`(base URL, api key)` from a seat file in `checkout`, or None.

    The child is told to run check-touchpoints with no server flag. The seat the
    supervisor wrote into the worktree is the pair it already uses to reach the
    ledger. A key with no server, or a file that does not parse, is not that pair.
    """
    import tomllib

    root = Path(checkout)
    for rel, kind in _SEAT_SPECS:
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            doc = tomllib.loads(text) if kind == "toml" else json.loads(text)
        except (OSError, UnicodeError, ValueError):
            continue
        found = _credentials_in(doc, toml=kind == "toml")
        if found:
            return found
    return None


def _generated_decision(client) -> tuple[tuple[str, ...], str]:
    """Paths the hold ignores, and why the project's rule was not read (or "").

    The same split `_note_touchpoints` makes. An observing rule was read and still
    ignores only the built-in list. `generated_paths` is not this split: it returns
    the configured list even while the rule is observing, which would let a file
    through that the supervisor still holds.
    """
    from .supervisor import DEFAULT_GENERATED, _generated_rule

    configured, observing, why = _generated_rule(client)
    # Same line as `_note_touchpoints`. An unread rule already came back as the
    # built-in list; an observing one still has to be swapped for it here.
    allowed = DEFAULT_GENERATED if observing else configured
    return allowed, why


def _check_touchpoints(args) -> int:
    from . import touchpoints as tp_mod
    from .supervisor import is_generated

    try:
        measured = tp_mod.measure_checkout(args.path)
    except ValueError as exc:
        print(f"gbfleet check-touchpoints: {exc}", file=sys.stderr)
        return 2

    client = None
    try:
        creds = seat_credentials(Path(args.path))
        if creds is not None:
            base, key = creds
            # The default allowlist is the supervisor's two reads and does not include
            # this one. Passing it is what makes the read happen; omitting it falls
            # back to the built-in list and says so.
            client = Graphban(
                base_url=base, api_key=key, allowed=frozenset({"get_rule_toggles"}),
            )
        allowed, why = _generated_decision(client)
    finally:
        if client is not None:
            client.close()

    missing = tp_mod.gate_missing(
        measured, args.declared, ignore=lambda path, paths=allowed: is_generated(path, paths),
    )
    said = gate_report(missing, item_id=args.item)
    text = said or "touchpoint gate: every changed file is covered by a declared touchpoint"
    if why:
        text = f"{text}\n{RULE_UNREAD} ({why})"
    print(text)
    return 1 if said else 0


def gate_report(missing: list[str], *, item_id: str = "") -> str:
    """What to tell a child whose branch the touchpoint gate would refuse (GRPH-1393).

    Names the files and the exact call that declares them; that call's `evidence` note is the
    record of the declaration. "" when nothing is outside.
    """
    if not missing:
        return ""
    shown = ", ".join(missing)
    return (
        f"touchpoint gate: {len(missing)} changed file(s) are outside every declared touchpoint "
        f"and the supervisor will NOT open a PR for this branch: {shown}\n"
        f"Declare them: update_item(id=\"{item_id or '<item>'}\", touchpoints={missing!r}, "
        f"evidence=[{{\"kind\": \"note\", \"detail\": \"declared touchpoints: {shown}\"}}])"
    )


def _teardown(args) -> int:
    return teardown_mod.run(args.name, prd=args.prd, dry_run=args.dry_run,
                            keep_clone=args.keep_clone)


def _gc() -> int:
    """Print what a sweep of the state directory did — including what it could not do.

    Exit 1 when a tree is still on disk that the pass could not take away. That is the
    normal outcome for a dirty tree and exactly the one a person has to look at, so a
    scheduled `gc` that always exited 0 would be a tidy-up nobody is ever told failed.
    """
    swept = adopt_mod.sweep()
    for line in swept.lines:
        print(line)
    print(
        f"gbfleet gc: {swept.repos} roster(s) naming {swept.records} worktree(s) — "
        f"{swept.removed} removed ({swept.logs} log dir(s) with them), {swept.live} still "
        f"running, {swept.left_dirty} left for a human, {swept.absent} already gone, "
        f"{swept.locked} repo(s) locked, {swept.unreadable} unreadable"
    )
    if swept.needs_a_human:
        print("gbfleet gc: something is still on disk that this pass could not remove",
              file=sys.stderr)
        return 1
    return 0


def _entitlement(args) -> int:
    from . import entitlements as ent_mod

    try:
        if args.act == "add":
            payload = (json.loads(args.json) if args.json else
                       {"name": args.name, "vendor": args.vendor, "kind": args.kind,
                        "handle": args.handle})
            ent_mod.add(ent_mod.parse(payload))
        elif args.act == "list":
            print(ent_mod.format_list(ent_mod.load()))
        elif args.act == "remove":
            ent_mod.remove(args.name)
        else:
            print("gbfleet entitlement: choose add, list or remove.", file=sys.stderr)
            return 2
    except (ent_mod.EntitlementError, json.JSONDecodeError) as exc:
        print(f"gbfleet entitlement: {exc}", file=sys.stderr)
        return 2
    return 0


def _bench_harvest(args: argparse.Namespace) -> int:
    from .bench import harvest as harvest_mod

    sab = frozenset(i.strip() for i in args.sabotage_items.split(",") if i.strip())
    try:
        found = harvest_mod.harvest(Path(args.repo), branch=args.branch, since=args.since,
                                    limit=args.limit, red_sabotage=sab)
        saved = harvest_mod.save(Path(args.repo), found)
    except (RuntimeError, OSError) as exc:
        print(f"gbfleet bench harvest: {exc}", file=sys.stderr)
        return 1
    for c in found.candidates:
        mark = " [sabotage]" if c.sabotage else ""
        print(f"{c.id}: {c.title} base={c.base_sha[:10]} check={c.command} {' '.join(c.tests)}{mark}")
    for d in found.dropped:
        print(f"dropped {d.merge_sha[:10]}: {d.reason}" + (f" ({d.detail})" if d.detail else ""))
    print(f"{len(found.candidates)} candidate(s), {len(found.dropped)} dropped; "
          f"nothing was sent to the server. Choose with `gbfleet bench adopt IDS` ({saved}).")
    return 0


def _bench_adopt(args: argparse.Namespace) -> int:
    from .bench import harvest as harvest_mod

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"gbfleet bench adopt: set ${API_KEY_ENV} to an agent key", file=sys.stderr)
        return 2
    cands = harvest_mod.load_candidates(Path(args.repo))
    missing = [i for i in args.ids if i not in cands]
    if missing:
        print(f"gbfleet bench adopt: no harvested candidate {', '.join(missing)}; "
              "run `gbfleet bench harvest` first", file=sys.stderr)
        return 1
    client = Graphban(args.server, api_key, project_id=args.project)
    # Reading the named item is the one extra read adopt needs, on this instance only.
    client.allowed = client.allowed | {"get_item_details"}

    def fetch_item(item_id: str):
        return client.call("get_item_details", id=item_id)

    tasks = []
    for i in args.ids:
        spec = harvest_mod.resolve_spec(cands[i], fetch_item)
        if spec is None:
            print(f"dropped {i}: no_spec (no readable item description or merge body)")
            continue
        tasks.append(harvest_mod.to_task(cands[i], args.capability, spec))
    if not tasks:
        print("gbfleet bench adopt: nothing to adopt; every candidate had no_spec", file=sys.stderr)
        return 1
    try:
        version = args.expected_version
        if version is None:
            version = int(client.get_suite(args.project).get("version", 0))
        got = client.adopt_suite(expected_version=version, tasks=tasks)
    except ObservationRefused as exc:
        if exc.status == 409:
            try:
                current = json.loads(exc.body).get("current_version")
            except (ValueError, AttributeError):
                current = None
            print(f"gbfleet bench adopt: the suite moved: expected_version {version} is stale; "
                  f"current version is {current}. Re-run to adopt against it.", file=sys.stderr)
            return 1
        print(f"gbfleet bench adopt: {exc}", file=sys.stderr)
        return 1
    print(f"adopted {len(tasks)} task(s); suite is now version {got.get('version')}")
    return 0


def _bench_run(args: argparse.Namespace) -> int:
    from . import supervisor as sup
    from . import worktree as wt_mod
    from .bench import review as review_mod
    from .bench import run as run_mod
    from .spawn import Reason, stop as stop_child

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"gbfleet bench run: set ${API_KEY_ENV} to an agent key", file=sys.stderr)
        return 2
    vendor, _, model = args.adapter.partition(":")
    client = Graphban(args.server, api_key, project_id=args.project)
    tasks: list = []
    counts: dict[str, int] = {}
    ids: dict[str, str] = {}
    tpl = None
    if args.suite in ("template", "all"):
        tpl = run_mod.template_suite()
        got = list(tpl.tasks) if tpl else []
        counts["template"] = len(got)
        tasks += got
        if tpl:
            ids["template"] = f"{tpl.id}@{tpl.version}"
    if args.suite in ("repo", "all"):
        try:
            suite_row = client.get_suite(args.project)
        except ObservationRefused as exc:
            print(f"gbfleet bench run: {exc}", file=sys.stderr)
            return 1
        got = [run_mod.task_from_row(r) for r in suite_row.get("tasks") or []]
        counts["repo"] = len(got)
        tasks += got
        ids["repo"] = f"repo@{suite_row.get('version', 0)}"
        if args.suite == "repo" and not got:
            print(run_mod.EMPTY_REPO, file=sys.stderr)
            return 2
    suite_id = args.suite if args.suite != "all" else "all"
    suite_version = "+".join(ids.values()) or "0"
    _p, policy, _n, _m, _c, _t = read_preferences(client)
    plan = run_mod.build_plan(tasks, adapter=args.adapter, model=model, suite_id=suite_id,
                              suite_version=suite_version, caps=policy.caps, counts=counts)
    for line in plan.describe():
        print(line)
    if args.dry_run:
        print("dry run: nothing was spawned")
        return 0
    if not plan.tasks:
        print("nothing to run")
        return 0
    try:
        factory = make_adapter_factory(vendor, args.binary, model)
    except AdapterError as exc:
        print(f"gbfleet bench run: {exc}", file=sys.stderr)
        return 2
    # Template tasks live in the shipped fixture repo, built into the workspace; only
    # repo-suite tasks need the user's repo (PRD-98 section 2: a template run needs no code).
    template_ids = {id(t) for t in (tpl.tasks if tpl else ())}
    needs_user = any(id(p.task) not in template_ids for p in plan.tasks)
    user_repo = repo_root(Path(args.repo)) if needs_user else None
    if args.workspace:
        workspace = Path(args.workspace).expanduser().resolve()
    elif user_repo is not None:
        workspace = user_repo.parent / f"{user_repo.name}-bench"
    else:
        workspace = Path(tempfile.gettempdir()) / "gbfleet-bench"
    fixture_repo = None
    if any(id(p.task) in template_ids for p in plan.tasks):
        try:
            fixture_repo = run_mod.template_fixture(workspace)
        except (OSError, RuntimeError) as exc:
            print(f"gbfleet bench run: cannot build the template fixture: {exc}", file=sys.stderr)
            return 1

    def repo_for(task) -> Path:
        return fixture_repo if id(task) in template_ids else user_repo

    repo = user_repo or fixture_repo
    wave = "bench"
    limits = Limits(max_workers=1, child_wall_clock=args.child_wall_clock,
                    registration_window=args.registration_window)

    def cut(task, base=None):
        # `base` is a review task's ref: its reviewer reads the change at that commit.
        return sup._tree_for(repo_for(task), workspace, wave, "".join(
            c if c.isalnum() else "-" for c in task.id), base=base or task.base_sha)

    def start(tree, code, task, scratch, review_skip=()):
        # The scratch client carries the scratch project's key; the source key cannot
        # register a child there (it is scoped to the source project).
        declare = {"vendor": vendor, "model": model, "tier": "cheap"}
        if task.kind == "review":
            # A review-only seat: the child gets the review instruction (claim_review, then
            # sign_off or bounce), never the build one (GRPH-1874 bounce).
            seat = Seat(code, args.server, scratch.api_key, role="reviewer", declare=declare,
                        review_only=True, review_skip=tuple(review_skip))
        else:
            seat = Seat(code, args.server, scratch.api_key, declare=declare, bound_only=True)
        return sup.start_one(tree, seat, factory, scratch, limits, sup.Partition(), workspace=workspace,
                             wave_name=wave, slot="".join(
                                 c if c.isalnum() else "-" for c in task.id))

    def reap(tree):
        wt_mod.reap_held(tree, vendor)

    def stop(child):
        stop_child(child, Reason.WALL_CLOCK)

    def read_verdict(task, scratch, item_id):
        """The reviewer's verdict on the task's own scratch item (`review.verdict_of`).

        Read on a client that may call `get_item_details` and nothing else. The scratch
        roster client carries the supervisor's allowlist, which reads no item.
        """
        reader = Graphban(args.server, scratch.api_key, project_id=getattr(scratch, "project_id", ""),
                          allowed=BENCH_VERDICT_READS)
        try:
            return review_mod.verdict_of(reader.call("get_item_details", id=item_id))
        finally:
            reader.close()

    posted = run_mod.run_plan(
        plan, repo=repo, client=client, read_verdict=read_verdict,
        open_scratch=lambda run: Graphban(args.server, run_mod.scratch_credential(run),
                                          project_id=run["project_id"]),
        start=start, cut=cut, limits=limits, stop=stop, reap=reap, wave=wave, repo_for=repo_for,
        binary_version=getattr(factory, "binary_version", "") or "")
    for r in posted.get("results", []):
        print(f"{r.task_id}: {r.outcome}" + (f" ({r.detail})" if r.detail else ""))
    print(f"posted {len(posted.get('results', []))} outcome(s) for {args.adapter}")
    return 0


def _bench_compare(args: argparse.Namespace) -> int:
    from .bench import compare as compare_mod

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"gbfleet bench compare: set ${API_KEY_ENV} to an agent key", file=sys.stderr)
        return 2
    client = Graphban(args.server, api_key, project_id=args.project)
    try:
        results = client.list_bench_results(args.project)
        a = compare_mod.find_run(results, args.runs[0])
        b = compare_mod.find_run(results, args.runs[1])
        lines = compare_mod.compare(a, b)
    except (ObservationRefused, compare_mod.CompareRefused) as exc:
        print(f"gbfleet bench compare: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


def _bench(args: argparse.Namespace) -> int:
    from .bench import suite as suite_mod
    from .bench import validate as validate_mod

    if args.act == "run":
        return _bench_run(args)
    if args.act == "harvest":
        return _bench_harvest(args)
    if args.act == "adopt":
        return _bench_adopt(args)
    if args.act == "compare":
        return _bench_compare(args)
    if args.act == "list":
        try:
            tpl = suite_mod.load(suite_mod.template_manifest())
        except suite_mod.SuiteError as exc:
            print(f"gbfleet bench list: {exc}", file=sys.stderr)
            return 1
        print(f"template: {tpl.id} v{tpl.version} ({len(tpl.tasks)} tasks)")
        return 0
    if args.act != "validate":
        print("gbfleet bench: say what to do. Try `gbfleet bench validate MANIFEST`.", file=sys.stderr)
        return 2
    try:
        loaded = suite_mod.load(args.manifest)
    except suite_mod.SuiteError as exc:
        print(f"gbfleet bench validate: {exc}", file=sys.stderr)
        return 1
    problems = suite_mod.leaf_floor_problems(loaded) if loaded.id == suite_mod.TEMPLATE_ID else []
    for line in problems:
        print(f"gbfleet bench validate: {line}", file=sys.stderr)
    if args.repo is None and loaded.id == suite_mod.TEMPLATE_ID:
        from .bench.template import fixture

        with tempfile.TemporaryDirectory(prefix="gbfleet-template-") as tmp:
            try:
                verdicts = validate_mod.validate(loaded, fixture.build(Path(tmp) / "repo"))
            except (OSError, RuntimeError) as exc:
                print(f"gbfleet bench validate: cannot build the template fixture: {exc}",
                      file=sys.stderr)
                return 1
    else:
        verdicts = validate_mod.validate(loaded, Path(args.repo or "."))
    for v in verdicts:
        print(v.line())
    return 0 if all(v.ok for v in verdicts) and not problems else 1


def _grid_baseline(b: dict | None) -> str:
    if not b:
        return "no baseline"
    rate = "-" if b.get("rate") is None else f"{round(b['rate'] * 100)}%"
    grey = " (below floor)" if b.get("below_floor") else ""
    excl = f" excluded={b['excluded']}" if b.get("excluded") else ""
    return f"{rate} n={b['n']}{excl} {b['suite_id']} v{b['suite_version']}{grey}"


def _harness(args: argparse.Namespace) -> int:
    if args.act != "grid":
        print("gbfleet harness: say what to do. Try `gbfleet harness grid`.", file=sys.stderr)
        return 2
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print(f"gbfleet harness grid: set ${API_KEY_ENV} to an agent key", file=sys.stderr)
        return 2
    try:
        data = Graphban(args.server, api_key, project_id=args.project).get_grid(args.project)
    except ObservationRefused as exc:
        print(f"gbfleet harness grid: {exc}", file=sys.stderr)
        return 1
    print("cell\tn\tlayer\trate\tbaseline")
    for cell in data.get("cells") or []:
        for c in (cell.get("leaves") or [cell]):
            k = c["key"]
            samples = c.get("samples") or {}
            if c.get("layer") == "baseline":
                label = (f"{k['vendor']}:{k['model']}@{k.get('binary_version') or '?'} "
                         f"{k['capability']} band {k.get('size_band') or '?'}")
                print(f"{label}\t0\tbaseline\t-\t{_grid_baseline(c.get('baseline'))}")
                continue
            nat = samples.get("natural") or {"finished": c["finished"], "rate": c.get("rate")}
            prb = samples.get("probe")
            for layer, side in (("natural", nat), ("probe", prb)):
                if side is None or (layer == "probe" and not side.get("finished")):
                    continue
                n = side.get("finished", side.get("n", 0))
                rate = "-" if side.get("rate") is None else f"{round(side['rate'] * 100)}%"
                floor = " (below floor)" if n < data.get("floor", 5) else ""
                base = _grid_baseline(c.get("baseline")) if layer == "natural" else ""
                label = (f"{k['vendor']}:{k['model']}@{k.get('binary_version') or '?'} "
                         f"{k['capability']} band {k.get('size_band') or '?'}")
                print(f"{label}\t{n}{floor}\t{layer}\t{rate}\t{base}")
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
            surface=True,
        )
        # FAIL only. An UNKNOWN is loud in the report and does not stop a run — refusing
        # on a check that could not be made would ground the fleet on a slow network.
        return 0 if findings.ok else 1

    if args.command == "surface":
        return _surface(args)

    if args.command == "gc":
        return _gc()

    if args.command == "check-touchpoints":
        return _check_touchpoints(args)

    if args.command == "entitlement":
        return _entitlement(args)

    if args.command == "bench":
        return _bench(args)

    if args.command == "harness":
        return _harness(args)

    if args.command == "teardown":
        return _teardown(args)

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

    repo = Path(args.repo)
    # Built before the base is resolved, because step 2 of the three rules asks the ledger
    # for the project's measured `gitops.base_branch` (GRPH-1012). Until now `up` resolved
    # only `--base` and otherwise passed nothing, so `_tree_for` fell back to `HEAD` and a
    # supervisor standing on a feature branch built every child on it.
    if args.merge:
        # The ONE widening, under the one flag that asks for it (GRPH-846).
        client = Graphban(base_url=args.server, api_key=api_key,
                          allowed=SPAWN_READS | MERGE_TOOLS, project_id=args.project)
    else:
        client = Graphban(base_url=args.server, api_key=api_key, allowed=SPAWN_READS, project_id=args.project)
    try:
        try:
            chosen = resolve_wave_base(repo, client, base_branch=args.base or "")
        except BaseBranchNotFound as exc:
            print(f"gbfleet up: {exc}", file=sys.stderr)
            return 2

        merger = None
        if args.merge:
            # `chosen.ref` IS the resolved ref — the remote's default when neither `--base`
            # nor a measured gitops base named one — so the Merger measures against the same
            # commit the children are cut from instead of working out a second answer.
            merger = Merger(repo, client, enabled=True, remote=remote_for(repo),
                            base=chosen.ref)
        wave = up(
            repo,
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
                registration_window=args.registration_window,
            ),
            workspace=Path(args.workspace) if args.workspace else None,
            debug=args.debug,
            wave_base=chosen,
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
