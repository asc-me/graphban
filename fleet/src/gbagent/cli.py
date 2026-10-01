"""`gbagent` — the command the supervisor launches (PRD-24 D8, S5).

Three commands, and the small one matters most: `--version` is what
`adapters/gbagent.py` resolves against, and because the pin is exact it is also how a
`gbagent` from a different install gets caught before a process does any work.

**`models` exists so a typo is refused at spawn.** GRPH-485 was this failure the long way
round — a model name that did not exist, found days later as a grill that would not converge.
The adapter cannot ask an endpoint itself (only two modules in this package may open a socket),
so it shells out to here.

**`run` can pick up its own work.** `--item` is optional from S7 on: without one the model
calls `claim_cluster` itself, which is in `coord.WORKER_TOOLS` along with the rest of
`COORDINATION_TOOLS` (P30 D3). `claim_next` is not advertised: it reserves no files.

**The seat file is also the build grant (GRPH-997).** Every stanza in it besides the ledger's
own is an MCP server an operator chose to share, and `read_shared` hands those to
`buildtools`, which advertises their tools to the model. gbagent has no shell and no web, so a
docs server and a browser are the build tools it cannot fake with `curl` — and before this it
read the seat, kept the credential, and dropped the rest of the file on the floor.

This paragraph used to say the opposite, and was true when written — a later slice wired the
thing it described as unwired, and the prose did not follow (GRPH-562). Corrected rather than
deleted, because the mistake is worth not repeating: **a tool set is a declaration of intent,
not an enforcement boundary**, so a docstring reasoning about authority from set membership is
describing the wrong object. The file even disagreed with itself — `assignment_for` below has
said all along that the model claims for itself.

What actually stops a worker overreaching is on the server: `TOOL_ROLES` gates what a
credential may call, `independent()` refuses a sign-off from the author whatever tools it
holds, and D5 clamps a worker at `review` — done is not the agent's word. `assert not
WORKER_TOOLS & ALLOWED_TOOLS` pins that a worker is not a supervisor, which is the one thing
the set itself is good for.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import gbfleet

from . import buildtools, loop
from .config import ConfigRefused, load, prepare
from .coord import (
    MERGED_COORDINATION, MERGED_TOOLS, REVIEWER_COORDINATION, REVIEWER_TOOLS,
    WORKER_TOOLS, Coordinator,
)
from .heartbeat import Heartbeat
from .llm import ModelUnreachable, OllamaSession
from .orient import (
    COORDINATION_TOOLS,
    INSTRUCTION as ORIENT_INSTRUCTION,
    OrientationUnavailable,
    build as build_orientation,
)
from .toolset import Toolset
from .workspace import OutsideWorktree, safe_path

#: Where the model endpoint lives. Named, never discovered — the same argument D3 makes
#: about the test command.
BASE_URL_ENV = "GBAGENT_BASE_URL"
#: A bearer for the model endpoint, when it wants one. Environment only, never argv: the
#: fleet's rule is that nothing carrying a credential goes on a command line (`ps` shows it),
#: and the supervisor's child inherits the operator's environment (spawn.py). Unset means an
#: unauthenticated endpoint — a local Ollama — which is what every walk so far has used.
API_KEY_ENV = "GBAGENT_API_KEY"


def endpoint_key() -> str:
    """What `GBAGENT_API_KEY` holds, or "" — the only way a model credential reaches gbagent."""
    return os.environ.get(API_KEY_ENV, "")

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


#: The repository's own instructions, read from the worktree root (GRPH-999). Grok and Claude
#: open these themselves; gbagent's prompt is fixed text plus the brief, so without this the
#: build rules every other child follows are markdown it never reads.
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
#: Where project skills live, relative to the worktree. Never a home directory: the operator's
#: personal skills are theirs, and copying them into a child's prompt is the MCP leak again.
SKILL_DIRS = (".claude/skills", ".grok/skills", ".agents/skills")
#: One budget for all of it — the same order as `orient.MAX_RESULT_CHARS`.
INSTRUCTIONS_CAP = 12_000


def _instruction_sources(root: Path) -> list[tuple[str, Path]]:
    """Every instruction file in the worktree, in prompt order, each resolved inside it.

    `safe_path` on every candidate, so a symlinked `AGENTS.md`, skill file or skills
    directory that leaves the worktree is skipped rather than read. `os.walk` does not
    descend symlinked directories, and a directory link that stays inside is found by
    its real path anyway.
    """
    found: list[tuple[str, Path]] = []
    for name in INSTRUCTION_FILES:
        try:
            path = safe_path(root, name)
        except OutsideWorktree:
            continue
        if path.is_file():
            found.append((name, path))
    for rel in SKILL_DIRS:
        try:
            base = safe_path(root, rel)
        except OutsideWorktree:
            continue
        if not base.is_dir():
            continue
        skills = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            if "SKILL.md" in filenames:
                candidate = Path(dirpath) / "SKILL.md"
                try:
                    path = safe_path(root, str(candidate))
                except OutsideWorktree:
                    continue
                if path.is_file():
                    skills.append((candidate.relative_to(root.resolve()).as_posix(), path))
        found.extend(sorted(skills))
    return found


def project_instructions(root: Path, cap: int = INSTRUCTIONS_CAP) -> str:
    """The worktree's AGENTS.md, CLAUDE.md and project skills, as prompt text under `cap`.

    Absent is normal and returns "" — a repo with no instructions starts the run with none.
    A file that does not fit is cut at the cap and SAYS so, with how much went unread; a file
    after the cap is spent is named with its size. Dropping either silently would read as a
    repo with no instructions, which is the one wrong conclusion this exists to prevent.
    """
    sections: list[str] = []
    left = cap
    for name, path in _instruction_sources(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        if left <= 0:
            sections.append(f"--- {name} ---\n[not included: the {cap}-character budget for "
                            f"project instructions was spent; {len(text)} characters unread. "
                            f"read_file {name} if you need it.]")
            continue
        body = text[:left]
        if len(text) > left:
            body += (f"\n[truncated at the {cap}-character budget: {len(text) - left} of "
                     f"{len(text)} characters left unread. read_file {name} for the rest.]")
        left -= min(len(text), left)
        sections.append(f"--- {name} ---\n{body}")
    if not sections:
        return ""
    return ("This repository's own instructions, from the worktree. Follow them.\n\n"
            + "\n\n".join(sections))


def system_prompt(root: Path) -> str:
    """`SYSTEM`, plus the repository's instructions when it has any."""
    extra = project_instructions(root)
    return f"{SYSTEM}\n\n{extra}" if extra else SYSTEM


def assignment_for(item: str, role: str = "worker") -> str:
    """What the model is told to work on.

    S6 (PRD-39 D-h): one loop — try `claim_review`, fall through to `claim_cluster`,
    exit when both are empty. The role parameter is kept for backward compatibility
    but no longer changes the assignment: a worker now claims, builds, AND reviews.

    `--item` is optional from S7 on. Without one the model calls `claim_review` then
    `claim_cluster` itself. With one it works the item it was handed.
    """
    if item:
        return (
            f"You are working on {item}. Do not claim other build work. When {item} is in "
            "review, call claim_review with wait_seconds=0 and review what you did not build "
            "— sign_off, or bounce with a reason — until it answers nothing; then say DONE "
            "and stop."
        )
    return (
        "Call claim_review with wait_seconds=0. If there is nothing to review, "
        "call claim_cluster with wait_seconds=0 to take the next ready non-colliding "
        "cluster. If both are empty, say DONE and stop — exiting on an empty queue "
        "is the normal end of your run, not a failure. You may sign_off work you "
        "did not build. If the work is not ready — tests fail, the change is wrong — "
        "bounce it with a reason naming what is wrong, then say DONE."
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


def register(client, *, code: str, model: str, worktree: str, branch: str) -> tuple[str, str, dict, str]:
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
    role = str(me.get("active_role") or "")
    off = me.get("tools_off_limits") or []
    if "create_item" in off:
        # P30 D11. A worker that cannot create cannot file a typed human wait.
        # That seat is a mis-mint, not a child that should limp on with free-text
        # `blocker`. S6: reviewer merged into worker, so every child is a worker.
        raise NotRegistered(
            "this seat cannot create_item — a worker that cannot file a human wait "
            "is a mis-mint (P30 D11)"
        )
    # PRD-36 D4: what a BOUND seat handed this child. `none` on an unbound seat; a server
    # that predates PRD-36 sends no key, which reads the same as `none` here.
    assigned = me.get("assigned") if isinstance(me.get("assigned"), dict) else {}
    # GRPH-719: the project this child landed on. Named on every later call, so a credential
    # spanning several projects does not send the child's reads to its default project.
    # A server that predates the field sends none, and the client then names nothing.
    project = str(me.get("project_id") or "")
    return agent_id, role, {"item": assigned.get("item"), "state": assigned.get("state") or "none",
                            "reason": assigned.get("reason"), "held_by": assigned.get("held_by")}, project


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


def read_shared(path: Path) -> dict[str, dict]:
    """Every server stanza in the seat file, as written (GRPH-997).

    This is the whole build grant, and it is the file the supervisor wrote: nothing here reads
    `~/.claude.json`, discovers a server, or takes a name from the model. `mcpshare.select`
    already refused a pattern and refused an unknown name before spawn, so what arrives is what
    an operator typed at `--mcp-server` — plus, once GRPH-998 lands, what the project listed.

    **Policy is not applied here.** The ledger's own reserved names are filtered by
    `buildtools.build`, beside the rest of the rule about what may be called, so a reader of
    this function does not have to know which half of the seat is a credential and which is a
    grant.

    Returns `{}` rather than refusing, whatever the file says. `read_seat` has already refused
    a seat this agent cannot use, and that is the refusal worth exiting 78 for: a shared stanza
    that is malformed is one docs server the model cannot call, and killing a build over it
    would spend the run on something the work does not depend on.
    """
    try:
        servers = json.loads(Path(path).read_text(encoding="utf-8"))["mcpServers"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(servers, dict):
        return {}
    return {str(name): dict(stanza) for name, stanza in servers.items()
            if isinstance(stanza, dict)}


def _models(base_url: str) -> list[str]:
    """What the endpoint serves, one per line. Empty when there is nothing to ask."""
    if not base_url:
        return []
    session = OllamaSession(base_url, "", system="", task="", api_key=endpoint_key())
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
        base_url, api_key = read_seat(Path(args.mcp_config))
    except SeatUnreadable as exc:
        print(f"gbagent: {exc}", file=sys.stderr)
        return 78

    written = Path(args.instruction_file).read_text(encoding="utf-8") if args.instruction_file else ""
    # The registration sentence is the harness's job and names a tool the model does not have.
    task = task_from(written)

    # REGISTER BEFORE PREPARE (P30 D8 / GRPH-503). `spawn.await_registration` polls the
    # roster for 90s and kills an unregistered child, blaming the adapter. `prepare()`
    # can run `uv pip install` for 900s. Counting that against the 90s window makes a
    # cold worktree look like a broken adapter. Presence-only heartbeats (no item id)
    # keep the roster alive during setup. Do not stretch registration to 900s.
    agent_id = args.agent_id
    project = ""
    role = ""
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
            agent_id, role, assigned, project = register(
                Coordinator.connect(base_url, api_key, item_id="").client,
                code=code, model=args.model, worktree=str(root), branch=args.branch,
            )
        except NotRegistered as exc:
            print(f"gbagent: {exc}", file=sys.stderr)
            return 78
        print(f"gbagent: registered {agent_id} as {role!r}", file=sys.stderr)
        # PRD-36 D3/D4: the server's answer outranks --item. `claimed` means this child
        # already HOLDS the seat's item — no claim_cluster, and the heartbeat carries it from
        # the first beat. `taken` means somebody else holds it: exit, the normal end of a
        # run with nothing to do, and say who.
        if assigned["state"] == "claimed" and assigned["item"]:
            args.item = str(assigned["item"])
            print(f"gbagent: this seat handed me {args.item}", file=sys.stderr)
        elif assigned["state"] == "taken":
            # GRPH-931: "ghost" means the holder row exists but the agent is offline (no
            # heartbeat within presence TTL). Name the ghost so fleet_status/release_item can
            # point at it, but say "no live holder" so the child does not look like it exited
            # without knowing why.
            if assigned.get("reason") == "ghost":
                ghost = assigned.get("held_by") or "unknown"
                print(
                    f"gbagent: this seat was bound to {assigned['item']} but it is a ghost "
                    f"lease (held by {ghost}, no live holder) — not claiming, exiting",
                    file=sys.stderr,
                )
            else:
                print(
                    f"gbagent: this seat was bound to {assigned['item']} but it is {assigned['reason']}"
                    + (f" by {assigned['held_by']}" if assigned.get("held_by") else "")
                    + " — nothing to do, exiting",
                    file=sys.stderr,
                )
            return 0
    assignment = assignment_for(args.item, role=role)
    # S6 (PRD-39 D-h): merged worker gets both build and review tools.
    tools = MERGED_TOOLS
    coordinator = Coordinator.connect(base_url, api_key, item_id=args.item,
                                      agent_id=agent_id, allowed=tools, project_id=project)
    heartbeat = Heartbeat(coordinator)
    heartbeat.start()
    session = None
    build_servers = None
    try:
        try:
            # AFTER register. The executable check inside `load` is what an unbuilt
            # worktree fails; a fresh `git worktree` is what PRD-22 hands every child
            # (GRPH-502). The heartbeat above is presence-only until a claim lands.
            built = prepare(root)
            for command in built:
                print(f"gbagent: setup ran {command!r}", file=sys.stderr)
            cfg = load(root)
        except ConfigRefused as exc:
            print(f"gbagent: {exc}", file=sys.stderr)
            return 78  # EX_CONFIG. Distinct from a crash, and from giving up.

        try:
            # S6 (PRD-39 D-h): merged worker orientation covers both build and review.
            orientation = build_orientation(
                coordinator.client, extra=MERGED_COORDINATION, agent_id=agent_id,
            )
        except OrientationUnavailable as exc:
            print(f"gbagent: {exc}", file=sys.stderr)
            return 78
        # GRPH-997: the seat's OTHER stanzas, which is the whole build grant — a docs server,
        # a browser. Connected here rather than in `read_seat` or at import, because it is a
        # network call per server and must neither delay registration (P30 D8) nor refuse a
        # run the ledger can still reach: a grant that fails is a smaller tool list the model
        # is TOLD about, not an exit.
        build_servers = buildtools.build(read_shared(Path(args.mcp_config)))
        if build_servers.granted:
            print(f"gbagent: build servers: {build_servers.one_line()}", file=sys.stderr)
        toolset = Toolset(root=root, cfg=cfg, orientation=orientation,
                          build_servers=build_servers)
        # The heartbeat thread was started before the toolset existed; from here on it
        # reports what the model is doing (PRD-34 D12).
        coordinator.status_source = toolset.activity
        session = OllamaSession(
            args.base_url, args.model,
            system=system_prompt(root),
            task="\n\n".join(part for part in (task, assignment, build_servers.instruction())
                             if part).strip(),
            api_key=endpoint_key(),
        )
        try:
            outcome = loop.run(session, toolset, coordinator=coordinator,
                               window=args.window, budget=args.turns, heartbeat=heartbeat,
                               trace=_trace)
        except ModelUnreachable as exc:
            print(f"gbagent: {exc}", file=sys.stderr)
            return 69  # EX_UNAVAILABLE. The endpoint, not this agent, and not a give-up.

        print(_summary(outcome, graph_calls=orientation.calls, beats=heartbeat.beats),
              file=sys.stderr)
        # The RESULT RECORD, on stdout, one line, machine-readable (PRD-38 D3). Every other
        # vendor has one — qwen's `-o json`, claude's `--output-format json` — and the
        # supervisor's exit report reads it to say what a run cost. gbagent had none, so its
        # cells read "not comparable: 0 of N attempts reported tokens" while the endpoint was
        # reporting the numbers on every turn and the loop was dropping them.
        #
        # stdout, not stderr: stderr is the human trace and it interleaves with the model's
        # own chatter. A record a machine has to find inside that is a record that will
        # eventually be mis-parsed.
        print(json.dumps({"gbagent": _result_record(outcome)}), flush=True)
        return outcome.exit_code
    finally:
        heartbeat.stop()
        if session is not None:
            session.close()
        if build_servers is not None:
            # Sockets a granted server is holding. Closed beside the model session's own,
            # because a child that exits without dropping them leaves the operator's docs
            # server carrying a connection per dead run.
            build_servers.close()


def _result_record(outcome) -> dict:
    """What a run cost, in the terms `attempt_telemetry` records.

    `tokens_in`/`tokens_out` are null when the endpoint never reported usage — not zero. A
    zero would say "this run was free", and the ledger's whole cost story rests on telling
    "nobody said" apart from "nothing was spent" (PRD-38 D3, D11).
    """
    reported = outcome.tokens_in or outcome.tokens_out
    return {
        "status": outcome.status,
        "exit": outcome.exit_code,
        "turns": outcome.turns,
        "tokens_in": outcome.tokens_in if reported else None,
        "tokens_out": outcome.tokens_out if reported else None,
        "compactions": outcome.compactions,
    }


def _summary(outcome, *, graph_calls: int, beats: int) -> str:
    """The one line a human reads about a run.

    **`NEVER`, not 0, when the agent never wrote** — see docs/orientation-metric-prd24.md.
    `Outcome.turns_to_first_write` is `None` in that case and four tests pin it, but this
    line is what anybody actually sees, and it was pinned by nothing: rendering it as
    `{first or 0}` left `Outcome` carrying `None`, every value-layer assertion holding, and
    the reader told "first write on turn 0" (GRPH-533).

    That matters more here than the value does. The S7 walk's run 1 claimed an item, ran the
    suite, passed BECAUSE IT HAD CHANGED NOTHING, and moved the item to review with "Ran all
    tests and verified the fix". Nothing else in the stack noticed — the server does not know
    worktrees exist, and an item arriving in review with a receipt looks like finished work.
    Somebody reading THIS LINE is how it was caught, and averaged in as 0 that run scores as
    the best one ever recorded.

    Extracted from `_run` so it can be asserted at all. Inline in a function that opens a
    model session and a heartbeat thread, it was unreachable from a test — which is why the
    value grew four guards and the sentence grew none.
    """
    first = outcome.turns_to_first_write
    return (f"gbagent: {outcome.status} after {outcome.turns} turns "
            f"({outcome.compactions} compaction(s), {graph_calls} graph call(s), "
            f"{beats} heartbeat(s), "
            f"first write on turn {first if first is not None else 'NEVER'})"
            f" — {outcome.meaning}")


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

    sub.add_parser("models", help=f"list what {BASE_URL_ENV} serves ({API_KEY_ENV} is sent as a bearer when set)")
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
