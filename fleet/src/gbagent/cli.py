"""`gbagent` — the command the supervisor launches (PRD-24 D8, S5).

Three commands, and the small one matters most: `--version` is what
`adapters/gbagent.py` resolves against, and because the pin is exact it is also how a
`gbagent` from a different install gets caught before a process does any work.

**`models` exists so a typo is refused at spawn.** GRPH-485 was this failure the long way
round — a model name that did not exist, found days later as a grill that would not converge.
The adapter cannot ask an endpoint itself (only two modules in this package may open a socket),
so it shells out to here.

**`run` refuses when it has no item.** It can orient itself — S6 advertises the graph reads —
but `claim_next` is deliberately absent from `coord.WORKER_TOOLS`, so it cannot pick up its own
work. Rather than start, do nothing useful and exit 0, it says which slice owns the gap. A
child that starts and quietly achieves nothing is the failure PRD-22 keeps naming.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import gbfleet

from . import loop
from .config import ConfigRefused, load
from .coord import Coordinator
from .heartbeat import Heartbeat
from .llm import ModelUnreachable, OllamaSession
from .orient import INSTRUCTION as ORIENT_INSTRUCTION, OrientationUnavailable, build as build_orientation
from .toolset import Toolset

#: Where the model endpoint lives. Named, never discovered — the same argument D3 makes
#: about the test command.
BASE_URL_ENV = "GBAGENT_BASE_URL"

#: What the model is told before anything else. Two jobs: say what it cannot do, so it does
#: not spend 30-second turns finding out, and say what to reach for FIRST (S6).
SYSTEM = (
    "You are gbagent, an unattended coding agent working inside one git worktree.\n"
    "Use the tools. Do not narrate what you are about to do — do it.\n"
    "Paths are relative to the worktree root. You cannot write outside it and you have no "
    "shell; run_tests runs the command this repository declares.\n"
    "\n" + ORIENT_INSTRUCTION + "\n"
    "\nWhen the tests pass, say DONE and stop calling tools."
)


class SeatUnreadable(RuntimeError):
    """The MCP config the supervisor wrote is not one this agent can use."""


def read_seat(path: Path) -> tuple[str, str]:
    """Pull the server URL and credential out of the seat file `spawn` wrote.

    Refuses rather than degrading. A missing key here means an agent that starts, cannot
    reach the server, and burns its whole turn budget discovering it — the expensive shape
    of the same mistake `config.load` refuses at spawn.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        server = data["mcpServers"]["graphban"]
        url = str(server["url"])
        key = str(server["headers"]["X-API-Key"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SeatUnreadable(f"{path}: not a Graphban MCP config ({exc})") from None
    if not url or not key:
        raise SeatUnreadable(f"{path}: the Graphban entry has no url or no X-API-Key")
    # `mcp_config` writes the endpoint, and the client appends it again.
    return url[: -len("/api/mcp")] if url.endswith("/api/mcp") else url, key


def _models(base_url: str) -> list[str]:
    """What the endpoint serves, one per line. Empty when there is nothing to ask."""
    if not base_url:
        return []
    session = OllamaSession(base_url, "", system="", task="")
    try:
        return session.list_models()
    finally:
        session.close()


def _run(args: argparse.Namespace) -> int:
    root = Path(args.worktree).resolve()
    try:
        cfg = load(root)
    except ConfigRefused as exc:
        print(f"gbagent: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG. Distinct from a crash, and from giving up.

    if not args.item:
        print(
            "gbagent: no item to work on. This agent can ORIENT itself (S6) but cannot yet "
            "CLAIM — `claim_next` is deliberately absent from coord.WORKER_TOOLS, and making "
            "it work end to end is the acceptance walk (GRPH-493). Pass --item with an id "
            "somebody already claimed for it.",
            file=sys.stderr,
        )
        return 78

    try:
        base_url, api_key = read_seat(Path(args.mcp_config))
    except SeatUnreadable as exc:
        print(f"gbagent: {exc}", file=sys.stderr)
        return 78

    task = Path(args.instruction_file).read_text(encoding="utf-8") if args.instruction_file else ""
    coordinator = Coordinator.connect(base_url, api_key, item_id=args.item,
                                      agent_id=args.agent_id)
    try:
        orientation = build_orientation(coordinator.client)
    except OrientationUnavailable as exc:
        print(f"gbagent: {exc}", file=sys.stderr)
        return 78
    toolset = Toolset(root=root, cfg=cfg, orientation=orientation)
    session = OllamaSession(
        args.base_url, args.model,
        system=SYSTEM,
        task=f"{task}\n\nYou are working on {args.item}.".strip(),
    )
    # Started BEFORE the loop and stopped in the same `finally` as the session, because the
    # window it exists to cover opens on the first blocking tool call (GRPH-496). Nothing
    # else refreshes `last_seen_at`, so without this the agent reads `offline` to the whole
    # fleet one presence TTL in — 150s by default — while its item lease quietly expires.
    heartbeat = Heartbeat(coordinator)
    heartbeat.start()
    try:
        outcome = loop.run(session, toolset, coordinator=coordinator,
                           window=args.window, budget=args.turns, heartbeat=heartbeat)
    except ModelUnreachable as exc:
        print(f"gbagent: {exc}", file=sys.stderr)
        return 69  # EX_UNAVAILABLE. The endpoint, not this agent, and not a give-up.
    finally:
        heartbeat.stop()
        session.close()

    # `None`, not 0, when it never wrote — see docs/orientation-metric-prd24.md. A run that
    # read for forty turns and changed nothing must not average in as the best one.
    first = outcome.turns_to_first_write
    print(f"gbagent: {outcome.status} after {outcome.turns} turns "
          f"({outcome.compactions} compaction(s), {orientation.calls} graph call(s), "
          f"{heartbeat.beats} heartbeat(s), "
          f"first write on turn {first if first is not None else 'NEVER'}) — {outcome.meaning}",
          file=sys.stderr)
    return outcome.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gbagent", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version",
                        version=f"gbagent {gbfleet.__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="build one item in one worktree")
    run.add_argument("--worktree", required=True)
    run.add_argument("--mcp-config", required=True)
    run.add_argument("--instruction-file", default="")
    run.add_argument("--item", default="")
    run.add_argument("--agent-id", default="")
    run.add_argument("--model", required=True)
    # No defaults. `loop.run` refuses to guess either of these and so does this.
    run.add_argument("--turns", type=int, required=True)
    run.add_argument("--window", type=int, required=True)
    run.add_argument("--base-url", default="")

    sub.add_parser("models", help=f"list what {BASE_URL_ENV} serves")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error("no command given — try `gbagent --help`")
    if args.command == "models":
        for name in _models(os.environ.get(BASE_URL_ENV, "")):
            print(name)
        return 0
    if not args.base_url:
        args.base_url = os.environ.get(BASE_URL_ENV, "")
    if not args.base_url:
        print(f"gbagent: no model endpoint. Set {BASE_URL_ENV} or pass --base-url.",
              file=sys.stderr)
        return 78
    return _run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
