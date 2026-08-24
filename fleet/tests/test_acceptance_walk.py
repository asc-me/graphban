"""PRD-22 §9 — the acceptance walk, run against a real server.

**Not a test-suite rerun.** AGENTS.md is explicit that running against real data is the
highest-yield check available and the one a green suite cannot substitute for. Every
other test in this package talks to a `MockTransport`; this one talks to a Graphban
instance, redeems real single-use seats, and watches real processes register.

Skipped unless `GBFLEET_WALK_SERVER` and `GBFLEET_WALK_KEY` are set, the same way
`test_a_real_installed_binary_resolves` skips where a vendor is absent — because a walk
that quietly passed by not running would be the worst possible version of this file.

    DATABASE_URL=... uvicorn app.main:app --port 8099        # a real instance
    GBFLEET_WALK_SERVER=http://127.0.0.1:8099 \\
    GBFLEET_WALK_KEY=gb_sk_... \\
    GBFLEET_WALK_PROJECT=<a scratch project, NOT a real one — see below> \\
    GBFLEET_WALK_SEATS="PLANNER-AAAAAA PLANNER-BBBBBB" \\
    GBFLEET_WALK_DB="postgresql://..." \\
        .venv/bin/python -m pytest tests/test_acceptance_walk.py -v -s

**Point it at a scratch project.** The walk claims from the backlog and signs work off
as done; `_refuse_a_real_project` stops it before the first write if the project holds
anything the walk did not itself create.

The child is `child_standin.py`: a genuine MCP client with the model removed. It redeems
a real seat, reports a real worktree, claims real work and exits. What it stands in for —
argv construction, config placement, version pinning — is already verified against real
`claude`, `cursor-agent` and `grok` binaries in `test_adapters.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gbfleet.adapters import Support, VersionUnsupported
from gbfleet.client import Graphban
from gbfleet.lock import RepoLocked, hold
from gbfleet.mcp import Fleet, handle
from gbfleet.seat import Seat
from gbfleet.spawn import Launch, LaunchFailed, Reason
from gbfleet.worktree import SEAT_FILES, Disposition, create, orphans, reap

SERVER = os.environ.get("GBFLEET_WALK_SERVER")
KEY = os.environ.get("GBFLEET_WALK_KEY")
DB = os.environ.get("GBFLEET_WALK_DB")
JWT = os.environ.get("GBFLEET_WALK_JWT")
PSQL = os.environ.get("GBFLEET_WALK_PSQL", "")
PROJECT = os.environ.get("GBFLEET_WALK_PROJECT", "")
STANDIN = Path(__file__).parent / "child_standin.py"

#: Planner seats an operator issued by hand and pasted in, consumed in order. The walk
#: needs two: one to register with, and one for the second minter step 14 measures the
#: retire scope against.
SEATS = [c for c in os.environ.get("GBFLEET_WALK_SEATS", "").replace(",", " ").split()]
SEATS_GIVEN = bool(SEATS)

#: Every item this walk creates is titled with this. It is also the ONLY thing that
#: tells a scratch project apart from a real one — see `_refuse_a_real_project`.
WALK_ITEM = "acceptance walk:"

pytestmark = pytest.mark.skipif(
    not (SERVER and KEY),
    reason="set GBFLEET_WALK_SERVER and GBFLEET_WALK_KEY to run the acceptance walk",
)

#: What still cannot run, recorded by number rather than skipped silently: a walk that
#: reports "16 of 17" without saying which one is a walk that passed by omission.
#:
#: GRPH-460 put `retire_wave` on the MCP surface and folded seat listing into
#: `fleet_status`, which unblocked 14 and 16. `reissue_enrolment` stayed off deliberately
#: — replacing a dead seat is a different capability from retiring your own wave, and it
#: had no caller asking for it. So 15 remains blocked on a decision, not on a budget.
BLOCKED = {
    15: "reissue_enrolment is not an MCP tool (GRPH-460 shipped retire_wave and the "
        "fleet_status seat listing; reissue was deliberately left off the surface)",
}


def rpc(tool: str, **args) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode()
    request = urllib.request.Request(
        f"{SERVER}/api/mcp",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": KEY},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    result = payload.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"{tool}: {result['content'][0]['text']}")
    return result.get("structuredContent") or {}


def refused(tool: str, **args) -> str:
    """Call a tool expecting it to be refused, and return why."""
    try:
        rpc(tool, **args)
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError(f"{tool} was permitted and should not have been")


def sql(query: str) -> list[tuple]:
    """Read stored state directly, for the one assertion the wire cannot make.

    Step 4 asks that neither spawned agent has a `parent_agent_id` **key at all**, and no
    API surface exposes it — `fleet_status` never did and should not start. Asserting an
    absence therefore means reading the row. That is legitimate for an acceptance walk,
    which is run by an operator who has the database, and it is why this step needs
    `GBFLEET_WALK_DB` rather than quietly passing without it.

    `GBFLEET_WALK_PSQL` overrides the client command, because a containerised Postgres
    is the normal case and `psql` is often not on the host at all.
    """
    if not DB:
        pytest.skip("set GBFLEET_WALK_DB to assert on stored state (step 4)")
    command = (PSQL.split() if PSQL else ["psql", DB]) + ["-tAF", "\x1f", "-c", query]
    out = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    return [tuple(line.split("\x1f")) for line in out.splitlines() if line.strip()]


@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    def ok(self, step: int, what: str) -> None:
        self.passed.append(f"{step:2}. {what}")
        print(f"  ✓ {step:2}. {what}", flush=True)

    def skip(self, step: int, why: str) -> None:
        self.blocked.append(f"{step:2}. {why}")
        print(f"  ⊘ {step:2}. BLOCKED — {why}", flush=True)

    def finding(self, step: int, what: str) -> None:
        self.findings.append(f"{step:2}. {what}")
        print(f"  ! {step:2}. FINDING — {what}", flush=True)


def _refuse_a_real_project() -> None:
    """Refuse to walk anything but a scratch project. FIRST, before a single write.

    Step 6 has a worker call `claim_next`, and `claim_next` takes the highest-priority
    ready item in the project — not the one the walk just created. Pointed at a real
    project this walk claims somebody's work, attaches a fabricated evidence note,
    moves it to `review`, and has a sibling sign it off as `done`. Step 6's assertion
    catches the wrong item afterwards, which is far too late: the damage is four writes
    old by then, and two of them are irreversible in any practical sense.

    That was not hypothetical. On 2026-08-22 two planner seats arrived minted against
    `agentledger` — 348 items, 53 of them ready to claim, `GRPH-466` at the head of the
    queue. Nothing in the walk would have stopped it.

    The test is "does this project hold anything the walk did not write", because a
    scratch project re-run against itself is the normal case and must keep working.
    """
    found = rpc("search_items", project_id=PROJECT, limit=100)
    strays = [f"{r['id']} {r.get('title', '')[:40]}" for r in found.get("results", [])
              if not r.get("title", "").startswith(WALK_ITEM)]
    total = found.get("total", 0)
    if strays or total > 100:
        raise AssertionError(
            f"refusing to walk {PROJECT!r}: {len(strays)} of its {total} items were not "
            f"written by this walk ({'; '.join(strays[:3])}). The walk CLAIMS from the "
            "backlog and signs work off as done — give it a scratch project of its own."
        )


def _standin_launch(seat: Seat, tree, instruction_file: Path, *, extra=("--claim",)) -> Launch:
    return Launch(
        adapter="standin",
        argv=[sys.executable, str(STANDIN), str(tree.path / SEAT_FILES[0]), *extra],
        seat_path=tree.path / SEAT_FILES[0],
        config=seat.mcp_config(),
        instruction="",
        binary_version="standin-1.0",
        stdin_file=instruction_file,
    )


def test_the_acceptance_walk(git_repo: Path, tmp_path: Path, state: Path):
    report = Report()
    print(f"\nPRD-22 acceptance walk against {SERVER}\n", flush=True)
    _refuse_a_real_project()

    # 1 ─ a planner registers, and cannot claim work.
    planner_code = _first_seat("planner")
    planner = rpc("register_agent", label="walk planner", enrolment_code=planner_code)
    why = refused("claim_next", agent_id=planner["agent_id"], wait_seconds=0)
    assert "planner" in why or "role" in why.lower(), why
    report.ok(1, f"planner {planner['agent_id']} registered; claim_next refused ({why[:60]})")

    # 2 ─ the planner mints its own seats.
    worker_seat = rpc("mint_enrolment", agent_id=planner["agent_id"], role="worker")
    reviewer_seat = rpc("mint_enrolment", agent_id=planner["agent_id"], role="reviewer")
    assert worker_seat["enrolment_code"] != reviewer_seat["enrolment_code"]
    report.ok(2, "planner minted a worker seat and a reviewer seat")

    # ── one real item, so steps 6 and 7 have work to do rather than a permission to
    #    inspect. An empty project would let both pass without a review ever happening.
    work_item = rpc(
        "create_item", title=f"{WALK_ITEM} something to build", project_id=PROJECT,
        status="next", touchpoints=["walk/predicted.py"],
    )["id"]

    # 3 ─ two spawns: two processes, two worktrees, two agents, distinct seats.
    workspace = tmp_path / "ws"
    client = Graphban(base_url=SERVER, api_key=KEY)
    fleet = Fleet(
        repo=git_repo, workspace=workspace, client=client,
        launch_for=lambda name, model="", tuning=None: (lambda s, t, i: _standin_launch(s, t, i)),
    )
    spawned = []
    for code in (worker_seat["enrolment_code"], reviewer_seat["enrolment_code"]):
        reply = handle(fleet, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "spawn", "arguments": {"adapter": "standin", "enrolment_code": code}},
        })
        assert not reply["result"].get("isError"), reply["result"]["content"][0]["text"]
        spawned.append(reply["result"]["structuredContent"])

    roster = {a["id"]: a for a in rpc("fleet_status")["agents"]}
    ids = [s["agent_id"] for s in spawned]
    assert len(set(ids)) == 2, ids
    seats_seen = {roster[i].get("enrolment_id") for i in ids}
    assert len(seats_seen) == 2 and None not in seats_seen, seats_seen
    assert len({s["worktree"] for s in spawned}) == 2
    report.ok(3, f"two agents {ids} with distinct enrolment_ids {sorted(seats_seen)}")

    # 4 ─ neither declares parentage, and review across the pair is permitted.
    rows = sql(
        "select id, coalesce(parent_agent_id, '<null>') from agents where id in "
        f"""('{ids[0]}','{ids[1]}')"""
    )
    assert rows, "agents not found in the database"
    for agent_id, parent in rows:
        assert parent == "<null>", f"{agent_id} declared parent {parent!r}"
    report.ok(4, f"neither spawned agent declared parentage ({len(rows)} rows checked)")

    # 5 ─ a second supervisor on the same repository refuses.
    with hold(git_repo, state):
        try:
            with hold(git_repo, state):
                raise AssertionError("a second supervisor started")
        except RepoLocked as exc:
            assert str(os.getpid()) in str(exc), str(exc)
            report.ok(5, f"second supervisor refused, naming the holder ({os.getpid()})")

    # 6 ─ the worker exits on empty, and the roster notices without being told.
    worker = next(s for s in spawned if s["agent_id"] == ids[0])
    deadline = time.monotonic() + 60
    while any(c.running for c in fleet.children) and time.monotonic() < deadline:
        time.sleep(0.2)
    listing = handle(fleet, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "ps", "arguments": {}},
    })["result"]["structuredContent"]
    assert listing["running"] == 0, listing
    state_now = rpc("get_item_details", id=work_item)
    assert state_now["status"] == "review", f"the worker did not move its item: {state_now}"
    report.ok(6, f"worker claimed {work_item}, moved it to review and exited; ps shows none running")

    # 7 ─ the reviewer claims the WORKER'S OWN item and signs it off.
    #     This is the whole of D-b, observed rather than argued: two children of one
    #     supervisor, on one credential, holding two seats — and review between them
    #     still means something. If parentage had been declared anywhere in the spawn
    #     path, `independent` would refuse here and the fleet would be unable to review
    #     a single thing it built.
    review = rpc("claim_review", agent_id=ids[1])
    assert review.get("claimed") is True, f"the reviewer was refused its sibling's work: {review}"
    reviewed = review["item"]["id"]
    assert reviewed == work_item, f"reviewed {reviewed}, expected {work_item}"

    signed = rpc("sign_off", id=reviewed, agent_id=ids[1])
    assert signed.get("status") == "done", signed
    author, reviewer_of = sql(
        f"select built_by, reviewed_by from items where id = '{reviewed}'"
    )[0]
    assert author == ids[0] and reviewer_of == ids[1], (author, reviewer_of)
    report.ok(
        7, f"{ids[1]} reviewed and signed off {reviewed}, built by its sibling {ids[0]}"
    )

    # 8 ─ after reap, no seat file survives.
    for child in fleet.children:
        tree_reaped = reap(
            __import__("gbfleet.worktree", fromlist=["Worktree"]).Worktree(
                path=child.worktree, branch=child.branch, repo=git_repo, base=child.base
            )
        )
        assert tree_reaped.removed, tree_reaped.reason
        for seat_file in SEAT_FILES:
            assert not (child.worktree / seat_file).exists()
        assert not child.seat_path.exists(), f"{child.seat_path} survived the reap"
    report.ok(8, "every seat file gone after reap, inside the worktree and out")

    # 9 ─ a worker killed mid-build is salvaged, and the commit carries no credential.
    tree = create(git_repo, workspace / "killed", "wave-kill", "9")
    (tree.path / "half-done.py").write_text("half a thought\n", encoding="utf-8")
    seat_path = tree.path / SEAT_FILES[0]
    seat_path.parent.mkdir(parents=True, exist_ok=True)
    seat_path.write_text(json.dumps({"apiKey": KEY}), encoding="utf-8")

    killed = reap(tree)
    assert killed.disposition is Disposition.SALVAGED, killed
    history = subprocess.run(
        ["git", "log", "-p", killed.branch], cwd=git_repo, capture_output=True, text=True
    ).stdout
    assert KEY not in history, "the salvage commit carries a live credential"
    assert "half a thought" in history
    report.ok(9, f"killed worker salvaged to {killed.branch}; no credential in the commit")

    # 10 ─ orphans lists it, and offers no opinion about resuming.
    found = {o.branch: o for o in orphans(git_repo)}
    assert killed.branch in found and found[killed.branch].salvaged
    report.ok(10, f"orphans lists {killed.branch} as salvaged, and nothing else")

    # 11 ─ a version outside the adapter's range refuses at spawn.
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho '1.0.0 (Claude Code)'\n", encoding="utf-8")
    fake.chmod(0o755)
    from gbfleet.adapters import resolve

    with pytest.raises(VersionUnsupported) as exc:
        resolve("claude", binary=fake)
    assert "1.0.0" in str(exc.value) and "2.0" in str(exc.value)
    report.ok(11, "version mismatch refused at spawn, naming binary and range")

    # 12 ─ a child that never registers is killed inside the window, adapter named.
    from gbfleet.supervisor import Limits, start_one, _tree_for
    from gbfleet.supervisor import Partition

    silent = _tree_for(git_repo, workspace, "wave-silent", "12")
    with pytest.raises(LaunchFailed) as exc:
        start_one(
            silent,
            Seat(code="NOPE-0000", server_url=SERVER, api_key=KEY),
            lambda s, t, i: Launch(
                adapter="silent", argv=[sys.executable, "-c", "import time; time.sleep(60)"],
                seat_path=t.path / SEAT_FILES[0], config=s.mcp_config(), instruction="",
            ),
            client, Limits(registration_window=3.0), Partition(),
            workspace=workspace, wave_name="wave-silent", slot="12",
        )
    assert "silent" in str(exc.value)
    report.ok(12, "silent child killed inside the registration window, adapter named")

    # 13 ─ unreachable server: no new spawns.
    from gbfleet.client import ServerUnreachable
    from gbfleet.supervisor import up

    offline_client = Graphban(base_url="http://127.0.0.1:1", api_key=KEY)
    wave = up(
        git_repo, [Seat(code="X", server_url=SERVER, api_key=KEY)],
        lambda s, t, i: _standin_launch(s, t, i), offline_client,
        state=state, workspace=tmp_path / "ws-offline",
    )
    assert wave.offline and wave.spawned == [] and wave.unused_seats == 1
    report.ok(13, "server unreachable: nothing spawned, the seat left unredeemed")

    # 14 ─ a planner retires the seats IT minted, and cannot reach another planner's.
    #      The scope claim is only worth something if a second minter exists to be
    #      spared, so the walk makes one. An isolation check with nothing on the other
    #      side passes for the wrong reason — it is the empty-set version of "absence
    #      reads as clean", and this repository has shipped that defect a dozen times.
    other_code = _first_seat("planner")
    other = rpc("register_agent", label="walk planner B", enrolment_code=other_code)
    other_seat = rpc("mint_enrolment", agent_id=other["agent_id"], role="worker")
    assert other_seat["enrolment_code"]

    mine = rpc("fleet_status", agent_id=planner["agent_id"])["seats"]
    theirs = rpc("fleet_status", agent_id=other["agent_id"])["seats"]
    assert {s["minted_by"] for s in mine} == {planner["agent_id"]}, mine
    assert {s["minted_by"] for s in theirs} == {other["agent_id"]}, theirs
    # The planner's OWN seat came from a human over REST, so it is not the planner's to
    # retire and must not appear in its list at all.
    assert planner["agent_id"] not in {s["consumed_by"] for s in mine}, mine

    retired = rpc("retire_wave", agent_id=planner["agent_id"])
    assert retired["seats_revoked"] == len(mine), (retired, mine)

    after_mine = rpc("fleet_status", agent_id=planner["agent_id"])["seats"]
    after_theirs = rpc("fleet_status", agent_id=other["agent_id"])["seats"]
    assert {s["state"] for s in after_mine} == {"revoked"}, after_mine
    assert "revoked" not in {s["state"] for s in after_theirs}, after_theirs
    still = rpc("get_item_details", id=work_item)
    assert still["status"] == "done", f"retiring a wave disturbed finished work: {still}"
    report.ok(
        14,
        f"{planner['agent_id']} retired its own {retired['seats_revoked']} seats; "
        f"planner B's {len(after_theirs)} untouched",
    )

    for step, why in sorted(BLOCKED.items()):
        report.skip(step, why)

    # 16 ─ the fleet grows and shrinks to zero through TOOLS ONLY — no Fleet view, no
    #      REST, nothing a human clicks. Every server call below is an MCP tool and every
    #      local one is a supervisor tool, which is the whole of the §6 complaint: spin-up
    #      was agent-callable and spin-down was not.
    shrink = Fleet(
        repo=git_repo, workspace=tmp_path / "ws-shrink", client=client,
        launch_for=lambda name, model="", tuning=None: (
            lambda s, t, i: _standin_launch(s, t, i, extra=("--linger",))
        ),
    )
    reply = handle(shrink, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        # A wave name of its own. The default is `wave`, and the branch guard refuses to
        # reuse `gb/wave-1` — step 9's salvage branch still holds it, which is the point of
        # salvage. The guard firing here is the correct behaviour, observed for free.
        "params": {"name": "spawn", "arguments": {
            "adapter": "standin", "wave": "shrink",
            "enrolment_code": other_seat["enrolment_code"]}},
    })
    assert not reply["result"].get("isError"), reply["result"]["content"][0]["text"]
    lingering = reply["result"]["structuredContent"]["agent_id"]

    grown = rpc("fleet_status", agent_id=other["agent_id"])
    assert lingering in {a["id"] for a in grown["agents"]}, grown["agents"]
    assert "consumed" in {s["state"] for s in grown["seats"]}, grown["seats"]

    shrunk = rpc("retire_wave", agent_id=other["agent_id"])
    # The field that stops `{"seats_revoked": 1}` reading as "the wave is over". The child
    # is still executing at this instant, against a seat that no longer authenticates.
    assert lingering in shrunk["agents_still_running"], shrunk
    assert {s["state"] for s in
            rpc("fleet_status", agent_id=other["agent_id"])["seats"]} == {"revoked"}

    stopped = handle(shrink, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "stop", "arguments": {
            "agent_id": lingering, "reason": "scaled_down"}},
    })["result"]["structuredContent"]
    assert stopped["stopped"] is True, stopped
    remaining = handle(shrink, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "ps", "arguments": {}},
    })["result"]["structuredContent"]
    assert remaining["running"] == 0, remaining
    report.ok(
        16,
        f"grew to 1 and shrank to 0 through tools alone: {shrunk['seats_revoked']} seat "
        f"revoked, {lingering} named as still running, then stopped",
    )

    # 17 ─ what each worker actually touched.
    from gbfleet import touchpoints as tp
    from gbfleet.worktree import Worktree

    measured = tp.measure(Worktree(path=tree.path, branch=killed.branch, repo=git_repo, base=tree.base))
    assert measured == ["half-done.py"], measured
    report.ok(17, f"measured touchpoints off the branch: {measured}")

    print(
        f"\n  {len(report.passed)} passed · {len(report.blocked)} blocked · "
        f"{len(report.findings)} findings\n", flush=True
    )
    client.close()
    offline_client.close()
    assert not report.findings, report.findings


def _first_seat(role: str) -> str:
    """A seat NO agent could have minted. The bootstrap is a human's, by design.

    The walk starts with nobody registered, so there is no planner to mint from yet.
    PRD-17 §D-e says issuing a credential and admitting an agent should never be
    automatic, and `mint_enrolment_as` took only the second half of that deliberately.

    Two ways in, both of them a person's:

    - `GBFLEET_WALK_SEATS` — codes an operator issued from the Fleet view and pasted in.
      This is the path that works against a DEPLOYED instance, where nobody has the
      operator's password on a command line and minting one by reaching into the box is
      the authority gate this very step exists to honour.
    - `GBFLEET_WALK_JWT` — a session, minting over REST as the walk goes. Convenient on
      an instance you just provisioned and hold the password for.
    """
    import urllib.parse

    if SEATS_GIVEN:
        assert role == "planner", (
            f"pre-minted seats are planner seats; the walk asked for {role!r}, which "
            "means a step changed and this bootstrap no longer matches it"
        )
        if not SEATS:
            pytest.fail(
                "GBFLEET_WALK_SEATS ran out. The walk needs TWO planner seats: one to "
                "register with (step 1), one for the second minter step 14 measures the "
                "retire scope against."
            )
        return SEATS.pop(0)

    if not JWT:
        pytest.skip(
            "set GBFLEET_WALK_SEATS to two planner codes an operator issued, or "
            "GBFLEET_WALK_JWT for a session that can mint them: the FIRST seat is behind "
            "user auth, which is PRD-22 §6's complaint stated as a precondition of its "
            "own acceptance walk"
        )
    body = json.dumps({"project_id": PROJECT, "roles": [role]}).encode()
    request = urllib.request.Request(
        f"{SERVER}/api/fleet/seats",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {JWT}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)["seats"][0]["code"]
    except urllib.error.HTTPError as exc:
        pytest.skip(
            f"cannot mint the first seat over REST ({exc.code}): the walk needs a "
            "user-auth session or a pre-minted planner seat"
        )
