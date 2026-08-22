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
    GBFLEET_WALK_DB="postgresql://..." \\
        .venv/bin/python -m pytest tests/test_acceptance_walk.py -v -s

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

pytestmark = pytest.mark.skipif(
    not (SERVER and KEY),
    reason="set GBFLEET_WALK_SERVER and GBFLEET_WALK_KEY to run the acceptance walk",
)

#: Steps that cannot run until GRPH-460 puts `retire_wave`, `list_enrolments` and
#: `reissue_enrolment` on the MCP surface. Recorded by number rather than skipped
#: silently: a walk that reports 14 of 17 without saying which three is a walk that
#: passed by omission.
BLOCKED = {
    14: "retire_wave is not an MCP tool yet (GRPH-460, blocked on the manifest budget)",
    15: "reissue_enrolment is not an MCP tool yet (GRPH-460)",
    16: "needs 14 and 15 — the planner cannot retire what it cannot reach",
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

    # 1 ─ a planner registers, and cannot claim work.
    planner_code = _seat_via_rest("planner")
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
        "create_item", title="acceptance walk: something to build", project_id=PROJECT,
        status="next", touchpoints=["walk/predicted.py"],
    )["id"]

    # 3 ─ two spawns: two processes, two worktrees, two agents, distinct seats.
    workspace = tmp_path / "ws"
    client = Graphban(base_url=SERVER, api_key=KEY)
    fleet = Fleet(
        repo=git_repo, workspace=workspace, client=client,
        launch_for=lambda name: (lambda s, t, i: _standin_launch(s, t, i)),
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

    for step, why in sorted(BLOCKED.items()):
        report.skip(step, why)

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


def _seat_via_rest(role: str) -> str:
    """Mint the FIRST seat the way a human does — over REST, behind user auth.

    The walk starts with nobody registered, so there is no planner to mint from yet.
    That bootstrap is the human's, by design: PRD-17 §D-e says issuing a credential and
    admitting an agent should never be automatic, and `mint_enrolment_as` took only the
    second half of that deliberately.
    """
    import urllib.parse

    if not JWT:
        pytest.skip(
            "set GBFLEET_WALK_JWT: minting the FIRST seat is behind user auth, which is "
            "PRD-22 §6's complaint stated as a precondition of its own acceptance walk"
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
