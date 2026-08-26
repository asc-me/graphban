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
import re
import sys
from pathlib import Path

import gbfleet

from . import loop
from .config import ConfigRefused, load, prepare
from .coord import Coordinator
from .heartbeat import Heartbeat
from .llm import ModelUnreachable, OllamaSession
from .orient import (
    COORDINATION_TOOLS,
    INSTRUCTION as ORIENT_INSTRUCTION,
    OrientationUnavailable,
    build as build_orientation,
)
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


def assignment_for(item: str) -> str:
    """What the model is told to work on.

    `--item` is optional from S7 on. Without one the model calls `claim_next` itself, which is
    what AC-5 asks for — an agent that cannot take its own work is not a fleet member. With one
    it works the item it was handed, which is what a re-run of a stuck item needs.

    The claim instruction says `wait_seconds=0` and what to do with nothing: PRD-22 D-c makes
    exiting on an empty queue the normal end of a worker's life, and a model that waits instead
    is a process nobody will notice is idle.
    """
    if item:
        return f"You are working on {item}. Do not claim anything else."
    return (
        "Call claim_next with wait_seconds=0 to take the next ready item, then build it. "
        "If there is nothing to claim, say DONE and stop — exiting on an empty queue is the "
        "normal end of your run, not a failure."
    )


#: How the enrolment code is read back out of the instruction file.
#:
#: `spawn` writes the code there and deliberately never into the MCP config — the code is an
#: argument to `register_agent`, not a config value (`seat.mcp_config`). The format is OURS
#: (`seat.INSTRUCTION`), and a test pins this pattern against that constant, so a reworded
#: instruction fails a test rather than producing a child that silently never registers.
ENROLMENT = re.compile(r"enrolment_code=['\"]([^'\"]+)['\"]")


class NotRegistered(RuntimeError):
    """Registration failed, so the supervisor is going to kill this child anyway.

    Refusing here names the cause. `await_registration` can only report that nothing appeared
    on the roster, which reads as a broken adapter — the misattribution PRD-22 S2 exists to
    prevent.
    """


def register(client, *, code: str, model: str, worktree: str, branch: str) -> tuple[str, str]:
    """Redeem the seat and come back with this child's server-side identity.

    Takes the client rather than building one, so the wiring is testable without a server —
    the property that matters is that the id the SERVER returned is the one the run uses, and
    a helper that made its own connection could only be checked by reading the source.

    `worktree` is what `spawn.await_registration` matches on (D-g: one worker, one worktree).
    `capabilities.vendor` is what drives review diversity, so a local tier is distinguishable
    from a frontier one on the roster.
    """
    try:
        me = client.call(
            "register_agent",
            enrolment_code=code,
            label=f"gbagent/{model}",
            worktree=worktree,
            branch=branch,
            capabilities={"vendor": "gbagent", "model": model, "tier": "local"},
        )
    except Exception as exc:  # noqa: BLE001 — every failure here has the same consequence
        raise NotRegistered(f"could not register: {exc}") from None
    agent_id = str(me.get("agent_id") or "")
    if not agent_id:
        raise NotRegistered("register_agent returned no agent_id")
    return agent_id, str(me.get("active_role") or "")


def enrolment_code(instruction: str) -> str:
    """The seat out of the instruction the supervisor wrote. "" when there is none."""
    found = ENROLMENT.search(instruction or "")
    return found.group(1) if found else ""


def task_from(instruction: str) -> str:
    """The instruction with the REGISTRATION sentence removed.

    **FOUND BY THE FIRST SUPERVISOR-SPAWNED BUILD.** `spawn` writes one instruction for every
    adapter and it opens by telling the child to call `register_agent` — correct for a vendor
    harness, which registers by being prompted to. gbagent registers in `_run` before the model
    exists, and `register_agent` is deliberately not among the tools it advertises. So the
    model was being told, as its first instruction, to call a tool it does not have. It spent
    thirty turns on it and claimed nothing.

    Only that sentence goes. Everything after it is still exactly right for this agent: it IS a
    separate process, it must NOT declare parentage, and exiting on an empty queue is the
    normal end of its run (D-b, D-c).

    Keyed on the sentence rather than on line 1, so a reordered instruction loses the right
    line — and a test renders `seat.INSTRUCTION` and asserts what survives.
    """
    kept = [line for line in (instruction or "").splitlines()
            if "register_agent" not in line]
    return "\n".join(kept).strip()


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


def _trace(event: "loop.Trace") -> None:
    """One line per thing that happened, to the child's own stderr (GRPH-506).

    stderr because that is what `spawn` captures to a file the supervisor can read, and
    because a fleet child has nowhere else to say anything. One line each, bounded upstream —
    a forty-turn run should be readable, not re-livable.
    """
    if event.kind == "turn":
        said = f" {event.text}" if event.text else ""
        print(f"gbagent: [{event.turn:>2}] model:{said}", file=sys.stderr, flush=True)
    else:
        mark = "ok " if event.ok else "ERR"
        print(f"gbagent: [{event.turn:>2}] {mark} {event.name}: {event.text}",
              file=sys.stderr, flush=True)


def _run(args: argparse.Namespace) -> int:
    root = Path(args.worktree).resolve()
    try:
        # BEFORE `load`, because the executable check inside it is exactly what an unbuilt
        # worktree fails. A fresh `git worktree` is what PRD-22 hands every child (GRPH-502).
        built = prepare(root)
        for command in built:
            print(f"gbagent: setup ran {command!r}", file=sys.stderr)
        cfg = load(root)
    except ConfigRefused as exc:
        print(f"gbagent: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG. Distinct from a crash, and from giving up.

    try:
        base_url, api_key = read_seat(Path(args.mcp_config))
    except SeatUnreadable as exc:
        print(f"gbagent: {exc}", file=sys.stderr)
        return 78

    written = Path(args.instruction_file).read_text(encoding="utf-8") if args.instruction_file else ""
    # The registration sentence is the harness's job and names a tool the model does not have.
    task = task_from(written)

    # REGISTER FIRST (GRPH-503). `spawn.await_registration` polls the roster for an agent whose
    # `worktree` matches this child and kills it when none appears, blaming the adapter. Every
    # walk before this invoked the CLI directly with `--agent-id`, so the adapter's argv was
    # verified and the thing the argv is FOR never was.
    agent_id = args.agent_id
    if not agent_id:
        code = enrolment_code(written)
        if not code:
            print(
                "gbagent: no enrolment code in the instruction file and no --agent-id. A child "
                "that does not register is one the supervisor kills for looking like a broken "
                "adapter, so this refuses instead and says which it was.",
                file=sys.stderr,
            )
            return 78
        try:
            agent_id, role = register(
                Coordinator.connect(base_url, api_key, item_id="").client,
                code=code, model=args.model, worktree=str(root), branch=args.branch,
            )
        except NotRegistered as exc:
            print(f"gbagent: {exc}", file=sys.stderr)
            return 78
        print(f"gbagent: registered {agent_id} as {role!r}", file=sys.stderr)
    assignment = assignment_for(args.item)
    coordinator = Coordinator.connect(base_url, api_key, item_id=args.item,
                                      agent_id=agent_id)
    try:
        orientation = build_orientation(coordinator.client, extra=COORDINATION_TOOLS,
                                        agent_id=agent_id)
    except OrientationUnavailable as exc:
        print(f"gbagent: {exc}", file=sys.stderr)
        return 78
    toolset = Toolset(root=root, cfg=cfg, orientation=orientation)
    session = OllamaSession(
        args.base_url, args.model,
        system=SYSTEM,
        task=f"{task}\n\n{assignment}".strip(),
    )
    # Started BEFORE the loop and stopped in the same `finally` as the session, because the
    # window it exists to cover opens on the first blocking tool call (GRPH-496). Nothing
    # else refreshes `last_seen_at`, so without this the agent reads `offline` to the whole
    # fleet one presence TTL in — 150s by default — while its item lease quietly expires.
    heartbeat = Heartbeat(coordinator)
    heartbeat.start()
    try:
        outcome = loop.run(session, toolset, coordinator=coordinator,
                           window=args.window, budget=args.turns, heartbeat=heartbeat,
                           trace=_trace)
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
    # An OVERRIDE, not the normal path: a walk re-running a stuck item should not have to mint
    # a seat. Given one, registration is skipped entirely.
    run.add_argument("--agent-id", default="")
    run.add_argument("--branch", default="")
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
