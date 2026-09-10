"""PRD-38 PR 1 (criteria 2, 4) — the supervisor's two posts, from this side of the wire.

The server's half is pinned in `backend/tests/test_harness_telemetry.py`. This is the half
that decides whether a post is made at all, what it carries, and — the part worth a test of
its own — that failing to make one costs nothing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from gbfleet import matrix as matrix_mod
from gbfleet.client import Graphban, ALLOWED_TOOLS
from gbfleet.mcp import Fleet, handle
from gbfleet.spawn import Child, Reason
from gbfleet.supervisor import Limits, Wave, watch_tick
from gbfleet.tiers import TierTable
from tests.test_supervisor import KEY, _factory

from conftest import telemetry_ack  # noqa: E402


def _recording_server(workspace: Path, posts: list) -> Graphban:
    """The `_server` fake plus a record of every REST post, which is the point here."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/mcp":
            posts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"id": "at_1"})
        body = json.loads(request.content)
        trees = sorted(p for p in workspace.glob("*") if p.is_dir() and p.name != "logs")
        payload = {"agents": [
            {"id": f"GRPH-A{i + 1}", "worktree": str(p), "state": "idle", "enrolled": True,
             "enrolment_id": f"seat-{i + 1}", "holdings": []}
            for i, p in enumerate(trees)]}
        if body["params"]["name"] == "propose_allocation":
            payload = {"workers": 0, "reviewers": 0, "mapping": [], "rationale": "none"}
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                       "structuredContent": payload}})

    return Graphban("http://gb.invalid", KEY, allowed=ALLOWED_TOOLS,
                    transport=httpx.MockTransport(handler))


@pytest.fixture
def recorded(git_repo: Path, tmp_path: Path, scripts, state: Path):
    workspace = tmp_path / "ws"
    posts: list = []
    fleet = Fleet(repo=git_repo, workspace=workspace,
                  client=_recording_server(workspace, posts),
                  launch_for=lambda name, model="", tuning=None: _factory(
                      scripts, "works_then_waits", adapter=name),
                  tiers=TierTable.parse(["cheap=fake:qwen-local"]))
    return fleet, posts


def _spawn(fleet: Fleet, **args) -> dict:
    reply = handle(fleet, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "spawn", "arguments": args}})
    return reply["result"]


# ---- the launch post -------------------------------------------------------------------------

def test_spawn_posts_what_it_resolved_before_the_child_starts(recorded):
    """4. Sabotage: drop the post and the server can only ever say `unknown`."""
    fleet, posts = recorded
    out = _spawn(fleet, tier="cheap", enrolment_code="WORKER-1")
    assert not out.get("isError"), out

    assert [p[0] for p in posts] == ["/api/fleet/attempts"]
    body = posts[0][1]
    assert body["enrolment_code"] == "WORKER-1"
    assert body["adapter"] == "fake"
    # The winner is spelled the way a CHILD declares itself, because that is what the server
    # compares it against: vendor first, then the model only when one was named.
    assert body["winner"] == "fake:qwen-local"
    assert body["source"] == "flag"


def test_an_explicit_adapter_is_posted_as_explicit(recorded):
    """4. `explicit` is not a resolution the matrix made, and must not be counted as one."""
    fleet, posts = recorded
    _spawn(fleet, adapter="fake", model="named", enrolment_code="WORKER-1")
    assert posts[0][1]["source"] == "explicit"
    assert posts[0][1]["winner"] == "fake:named"


def test_a_launch_post_that_cannot_land_does_not_fail_the_spawn(git_repo: Path, tmp_path: Path,
                                                               scripts, state: Path):
    """D3. A measurement that could fail a spawn would be a worse bargain than no measurement."""
    workspace = tmp_path / "ws"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/mcp":
            raise httpx.ConnectError("no route to host")
        body = json.loads(request.content)
        trees = sorted(p for p in workspace.glob("*") if p.is_dir() and p.name != "logs")
        payload = {"agents": [{"id": "GRPH-A1", "worktree": str(p), "state": "idle",
                               "enrolled": True, "enrolment_id": "seat-1", "holdings": []}
                              for p in trees]}
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                       "structuredContent": payload}})

    fleet = Fleet(repo=git_repo, workspace=workspace,
                  client=Graphban("http://gb.invalid", KEY, allowed=ALLOWED_TOOLS,
                                  transport=httpx.MockTransport(handler)),
                  launch_for=lambda name, model="", tuning=None: _factory(
                      scripts, "works_then_waits", adapter=name),
                  tiers=TierTable.parse(["cheap=fake:qwen-local"]))
    out = _spawn(fleet, tier="cheap", enrolment_code="WORKER-1")
    assert not out.get("isError"), out
    assert len(fleet.children) == 1 and fleet.children[0].running


def test_the_requested_turn_budget_is_carried_on_the_child(recorded):
    """D3. A child that stopped AT its budget reads the same as one that finished early
    unless the budget is recorded beside the turns."""
    fleet, _ = recorded
    _spawn(fleet, tier="cheap", enrolment_code="WORKER-1", turns=40)
    assert fleet.children[0].turn_budget == 40


# ---- the exit report -------------------------------------------------------------------------

class _Dead:
    """A process that has already exited, with the code the test wants."""

    def __init__(self, code: int = 0) -> None:
        self._code, self.pid = code, 4242

    def poll(self) -> int | None:
        return self._code


def _exited(adapter: str = "fake", seat_id: str | None = "seat-1", code: int = 0,
            version: str = "1.2.3") -> Child:
    return Child(adapter=adapter, worktree=Path("/tmp/wt"), branch="gb/x", base="",
                 seat_path=Path("/tmp/seat.json"), process=_Dead(code),
                 started_at=time.monotonic() - 30, log_dir=Path("/tmp/logs"),
                 binary_version=version, seat_id=seat_id, turn_budget=40)


def _tick(fleet_client, children):
    wave = Wave()
    watch_tick(wave, children, Limits(), fleet_client, debug=False)


def test_a_child_that_exited_is_reported_once(recorded):
    """2. Sabotage: report from `_reap_all` instead and a child that exits early is reported
    an hour late, or never if the supervisor dies first."""
    fleet, posts = recorded
    child = _exited()
    _tick(fleet.client, [child])
    _tick(fleet.client, [child])

    reports = [b for path, b in posts if b.get("enrolment_id")]
    assert len(reports) == 1, reports
    assert reports[0]["enrolment_id"] == "seat-1"
    assert reports[0]["binary_version"] == "1.2.3"
    assert reports[0]["turn_budget"] == 40
    assert reports[0]["wall_seconds"] >= 30
    # The ADAPTER's word for exit 0, not a word this module made up.
    assert reports[0]["exit_meaning"] == "finished"
    assert child.reported is True


def test_a_child_that_never_registered_is_not_reported(recorded):
    """D3. There is no attempt to report, and the silence is already carried elsewhere."""
    fleet, posts = recorded
    _tick(fleet.client, [_exited(seat_id=None)])
    assert [b for path, b in posts if b.get("enrolment_id")] == []


def test_a_running_child_is_not_reported(recorded):
    """2. Sabotage: drop the `running` guard and every tick posts an ending that has not
    happened."""
    class _Live(_Dead):
        def poll(self):
            return None

    fleet, posts = recorded
    child = _exited()
    child.process = _Live()
    _tick(fleet.client, [child])
    assert [b for path, b in posts if b.get("enrolment_id")] == []
    assert child.reported is False


def test_the_supervisors_own_kill_is_not_reported_as_the_vendors_verdict(recorded):
    """D3. A child stopped for running past the wall clock exits with whatever the signal
    produced, and reporting that as the harness's exit code attributes the supervisor's
    decision to the harness."""
    fleet, posts = recorded
    child = _exited(code=-15)
    child.stopped_because = Reason.WALL_CLOCK
    _tick(fleet.client, [child])
    report = [b for path, b in posts if b.get("enrolment_id")][0]
    assert report["exit_meaning"] == f"stopped: {Reason.WALL_CLOCK.value}"


# ---- the walk finding: a child that exits must leave its work ON ITS BRANCH -----------------

def _dirty_worktree(git_repo: Path, tmp_path: Path):
    """A real linked worktree with an uncommitted edit — what every walk child left behind."""
    import subprocess

    from gbfleet import worktree as wt

    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A1")
    (tree.path / "note.md").write_text("work the child did\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tree.path), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tree.path), "reset", "-q"], check=True,
                   capture_output=True)
    return tree


def test_a_child_that_exits_has_its_work_salvaged_onto_its_branch(recorded, git_repo: Path,
                                                                  tmp_path: Path):
    """The finding, as a test. `_reap_all` has always salvaged — but it runs at the END of a
    wave, which only `up` has. On the MCP surface a child could exit, be stopped, and leave
    its worktree uncommitted forever: the reviewer reads the BRANCH and saw nothing.

    Sabotage: drop `_reap_exited` from `watch_tick` and the branch stays at its base.
    """
    import subprocess

    fleet, _ = recorded
    tree = _dirty_worktree(git_repo, tmp_path)
    before = subprocess.run(["git", "-C", str(git_repo), "rev-parse", tree.branch],
                            capture_output=True, text=True).stdout.strip()

    child = _exited()
    child.worktree, child.branch, child.base = tree.path, tree.branch, tree.base
    _tick(fleet.client, [child])

    after = subprocess.run(["git", "-C", str(git_repo), "rev-parse", tree.branch],
                           capture_output=True, text=True).stdout.strip()
    assert after != before, "the child's work never reached its branch"
    files = subprocess.run(["git", "-C", str(git_repo), "show", "--pretty=format:",
                            "--name-only", after], capture_output=True, text=True).stdout
    assert "note.md" in files
    assert child.reaped is True


def test_a_running_child_is_not_reaped(recorded, git_repo: Path, tmp_path: Path):
    """Reaping removes the worktree. Doing it to a live child would delete the work it is
    still writing."""
    class _Live(_Dead):
        def poll(self):
            return None

    fleet, _ = recorded
    tree = _dirty_worktree(git_repo, tmp_path)
    child = _exited()
    child.worktree, child.branch, child.base = tree.path, tree.branch, tree.base
    child.process = _Live()
    _tick(fleet.client, [child])
    assert child.reaped is False
    assert tree.path.exists(), "a running child's worktree was removed"


def test_a_child_is_reaped_once_however_many_ticks_run(recorded, git_repo: Path,
                                                       tmp_path: Path):
    """The second tick must not report a failure for work that is safely on its branch."""
    from gbfleet.supervisor import Wave

    fleet, _ = recorded
    tree = _dirty_worktree(git_repo, tmp_path)
    child = _exited()
    child.worktree, child.branch, child.base = tree.path, tree.branch, tree.base

    wave = Wave()
    from gbfleet.supervisor import Limits, watch_tick
    for _ in range(3):
        watch_tick(wave, [child], Limits(), fleet.client, debug=False)
    assert len(wave.reaped) == 1
    assert wave.failures == [], wave.failures


# ---- GRPH-750: work reaching a branch is not the same as work reaching the reviewer --------

def _repo_with_remote(git_repo: Path, tmp_path: Path):
    """A repo whose branches can actually go somewhere, which the fleet fixtures do not have."""
    import subprocess

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(git_repo), "remote", "add", "origin", str(bare)],
                   check=True, capture_output=True)
    return bare


def test_a_reaped_branch_is_pushed_so_a_reviewer_elsewhere_can_read_it(
        recorded, git_repo: Path, tmp_path: Path):
    """GRPH-750, the finding as a test. #639 got the work onto a branch and the reviewer still
    bounced it — correctly — because the branch existed only in the supervisor's checkout.

    Sabotage: drop `_publish` and the bare remote never hears of the branch.
    """
    import subprocess

    fleet, _ = recorded
    bare = _repo_with_remote(git_repo, tmp_path)
    tree = _dirty_worktree(git_repo, tmp_path)

    child = _exited()
    child.worktree, child.branch, child.base = tree.path, tree.branch, tree.base
    _tick(fleet.client, [child])

    remote_refs = subprocess.run(["git", "-C", str(bare), "branch", "--list"],
                                 capture_output=True, text=True).stdout
    assert tree.branch in remote_refs, f"{tree.branch} never reached the remote"
    files = subprocess.run(["git", "-C", str(bare), "show", "--pretty=format:", "--name-only",
                            tree.branch], capture_output=True, text=True).stdout
    assert "note.md" in files


def test_a_branch_with_nothing_beyond_its_base_is_skipped_and_says_so(
        recorded, git_repo: Path, tmp_path: Path):
    """Skipped is not ok, and neither is a failure. A child that changed nothing has nothing
    to publish, and calling that "pushed" would make the two cases look alike."""
    from gbfleet import worktree as wt

    fleet, _ = recorded
    _repo_with_remote(git_repo, tmp_path)
    tree = wt.create(git_repo, tmp_path / "empty", wave="w", agent_id="A2")

    child = _exited()
    child.worktree, child.branch, child.base = tree.path, tree.branch, tree.base
    wave = Wave()
    from gbfleet.supervisor import Limits, watch_tick
    watch_tick(wave, [child], Limits(), fleet.client, debug=False)

    pushed = wave.published[tree.branch]
    assert pushed.skipped is True and pushed.ok is False
    assert "nothing beyond its base" in pushed.reason
    assert wave.failures == [], "a branch with nothing to say is not a failure"


def test_a_repository_with_no_remote_says_the_branch_stays_local(
        recorded, git_repo: Path, tmp_path: Path):
    """The honest answer when there is nowhere to push. Silence here would recreate exactly
    the defect this closes: a reviewer left to infer an empty diff."""
    fleet, _ = recorded
    tree = _dirty_worktree(git_repo, tmp_path)  # git_repo has no remote

    child = _exited()
    child.worktree, child.branch, child.base = tree.path, tree.branch, tree.base
    wave = Wave()
    from gbfleet.supervisor import Limits, watch_tick
    watch_tick(wave, [child], Limits(), fleet.client, debug=False)

    pushed = wave.published[tree.branch]
    assert pushed.skipped is True
    assert "no remote" in pushed.reason and "reviewer cannot read it" in pushed.reason


def test_a_refused_push_is_reported_loudly(recorded, git_repo: Path, tmp_path: Path):
    """A branch that did not reach the remote is invisible to review. Swallowing the failure
    would leave the reviewer inferring it from an empty diff."""
    import subprocess

    fleet, _ = recorded
    subprocess.run(["git", "-C", str(git_repo), "remote", "add", "origin",
                    str(tmp_path / "nowhere.git")], check=True, capture_output=True)
    tree = _dirty_worktree(git_repo, tmp_path)

    child = _exited()
    child.worktree, child.branch, child.base = tree.path, tree.branch, tree.base
    wave = Wave()
    from gbfleet.supervisor import Limits, watch_tick
    watch_tick(wave, [child], Limits(), fleet.client, debug=False)

    pushed = wave.published[tree.branch]
    assert pushed.ok is False and pushed.skipped is False
    assert "push refused" in pushed.reason
    assert any("push refused" in f for f in wave.failures), wave.failures


def test_pushing_twice_is_a_no_op_rather_than_a_duplicate(recorded, git_repo: Path,
                                                          tmp_path: Path):
    """Answering the 'vendors that can push, do' rule: if the child already pushed, ours must
    cost nothing rather than fail or duplicate."""
    from gbfleet import worktree as wt

    import subprocess

    fleet, _ = recorded
    _repo_with_remote(git_repo, tmp_path)
    tree = _dirty_worktree(git_repo, tmp_path)
    # Committed here rather than left dirty: this test is about the PUSH being idempotent,
    # and an uncommitted branch is correctly skipped before the push is ever reached.
    wt.salvage(tree.path, "committed by the child itself")
    assert wt.commits_beyond_base(tree.repo, tree.branch, tree.base) == 1

    first = wt.push_branch(tree.repo, tree.branch, tree.base)
    assert first.ok is True, first.reason

    second = wt.push_branch(tree.repo, tree.branch, tree.base)
    assert second.ok is True, second.reason


# ---- GRPH-772: a harness only ever named outright must still be promotable ------------------

def test_an_explicit_spawn_posts_the_matrix_view_of_the_row_it_ran(recorded):
    """An explicit spawn resolves nothing, so PRD-37 D8 produces no explanation — and sending
    none left the server unable to learn that harness's matrix status, so PRD-38's R1 could
    never promote a row that is only ever named outright.

    Sabotage: send `resolution=None` for an explicit spawn and the server is blind again.
    """
    fleet, posts = recorded
    # A harness the committed matrix KNOWS, so there is something truthful to say about it.
    # Naming one it does not know would make this test pass with the wiring removed, which is
    # how the first version of it passed under its own sabotage.
    _spawn(fleet, adapter="gbagent", enrolment_code="WORKER-1")
    body = posts[0][1]
    assert body["source"] == "explicit"
    res = body.get("resolution")
    assert res is not None, "an explicit spawn sent nothing, so the server cannot learn the row"
    assert res["source"] == "explicit"
    assert res["winner"]["harness"] == "gbagent"
    assert res["winner"]["status"] in ("verified", "unverified", "failed")


def test_an_explicit_spawn_of_an_unknown_adapter_sends_no_resolution(recorded):
    """The other half: `fake` is not in the matrix, and inventing a status for it is the
    failure the whole module exists to avoid."""
    fleet, posts = recorded
    _spawn(fleet, adapter="fake", model="named", enrolment_code="WORKER-1")
    body = posts[0][1]
    assert body["source"] == "explicit"
    assert "resolution" not in body, "a row the matrix does not know must not get a status"


def test_the_matrix_view_names_one_candidate_because_there_was_one(recorded):
    """The shortlist has a single entry, and that is the honest shape: nothing was ranked and
    nothing was dropped, so a replay over it correctly reports that no reordering changes it."""
    from gbfleet import matrix as m

    res = m.explicit_resolution("gbagent", "qwen3.6:35b-a3b-coding-mtp-det")
    assert res["source"] == "explicit"
    assert len(res["shortlist"]) == 1
    assert res["runner_up"] is None and res["dropped_rows"] == []
    assert res["winner"]["status"] in ("verified", "unverified", "failed", "unregistered")
    # No score: nothing was scored. A zero here would read as "scored badly".
    assert res["winner"]["score"] is None


def test_an_adapter_the_matrix_does_not_know_gets_no_invented_status(recorded):
    """The refusal that keeps this honest. Sabotage: fall back to a default row and an
    unregistered adapter acquires a status nobody committed."""
    from gbfleet import matrix as m

    assert m.explicit_resolution("nosuch-harness", "") is None

# ---- PRD-38 D3: what a run cost, from the vendor's own record -------------------------------

def test_the_exit_report_carries_what_the_vendors_record_says(recorded, git_repo: Path,
                                                              tmp_path: Path):
    """The endpoint reports tokens on every turn and the loop was dropping all but the last,
    so every cell read "not comparable: 0 of N attempts reported tokens" while the numbers
    were being computed and thrown away.

    Sabotage: drop `**facts` from the post and the ledger is blind again.
    """
    fleet, posts = recorded
    child = _exited(adapter="gbagent")
    child.log_dir = tmp_path / "logs"
    child.log_dir.mkdir(parents=True, exist_ok=True)
    (child.log_dir / "stdout.log").write_text(
        'gbagent: some human line\n'
        '{"gbagent": {"status": "finished", "exit": 0, "turns": 7, '
        '"tokens_in": 91000, "tokens_out": 4100, "compactions": 1}}\n',
        encoding="utf-8")
    _tick(fleet.client, [child])

    report = [b for path, b in posts if b.get("enrolment_id")][0]
    assert report["turns_used"] == 7
    assert report["tokens_in"] == 91000 and report["tokens_out"] == 4100


def test_a_vendor_that_prints_no_record_reports_no_tokens(recorded, tmp_path: Path):
    """"Not reported" and "zero" are different claims, and only one of them is true here."""
    fleet, posts = recorded
    child = _exited(adapter="fake")
    child.log_dir = tmp_path / "quiet"
    child.log_dir.mkdir(parents=True, exist_ok=True)
    (child.log_dir / "stdout.log").write_text("nothing machine-readable\n", encoding="utf-8")
    _tick(fleet.client, [child])

    report = [b for path, b in posts if b.get("enrolment_id")][0]
    assert "tokens_in" not in report and "tokens_out" not in report
    assert "turns_used" not in report


def test_a_run_whose_endpoint_reported_no_usage_sends_null_not_zero(recorded, tmp_path: Path):
    """Sabotage: emit 0 for an unreported total and a run nobody measured looks free."""
    fleet, posts = recorded
    child = _exited(adapter="gbagent")
    child.log_dir = tmp_path / "nousage"
    child.log_dir.mkdir(parents=True, exist_ok=True)
    (child.log_dir / "stdout.log").write_text(
        '{"gbagent": {"status": "finished", "exit": 0, "turns": 3, '
        '"tokens_in": null, "tokens_out": null}}\n', encoding="utf-8")
    _tick(fleet.client, [child])

    report = [b for path, b in posts if b.get("enrolment_id")][0]
    assert report["turns_used"] == 3
    assert "tokens_in" not in report and "tokens_out" not in report


def test_the_last_record_wins(recorded, tmp_path: Path):
    """A run that printed a record, was resumed and printed another is describing the same
    attempt twice; the later one is the one that finished."""
    fleet, posts = recorded
    child = _exited(adapter="gbagent")
    child.log_dir = tmp_path / "twice"
    child.log_dir.mkdir(parents=True, exist_ok=True)
    (child.log_dir / "stdout.log").write_text(
        '{"gbagent": {"turns": 2, "tokens_in": 10, "tokens_out": 1}}\n'
        '{"gbagent": {"turns": 9, "tokens_in": 900, "tokens_out": 90}}\n', encoding="utf-8")
    _tick(fleet.client, [child])

    report = [b for path, b in posts if b.get("enrolment_id")][0]
    assert report["turns_used"] == 9 and report["tokens_in"] == 900


def test_a_malformed_record_does_not_take_the_supervisor_down(recorded, tmp_path: Path):
    """A vendor's broken output is not this process's crash."""
    fleet, posts = recorded
    child = _exited(adapter="gbagent")
    child.log_dir = tmp_path / "broken"
    child.log_dir.mkdir(parents=True, exist_ok=True)
    (child.log_dir / "stdout.log").write_text('{"gbagent": {"turns": ]]]\n', encoding="utf-8")
    _tick(fleet.client, [child])
    assert [b for path, b in posts if b.get("enrolment_id")], "no report was sent at all"


def test_the_exit_post_carries_the_shape_of_what_the_child_changed(recorded, git_repo: Path,
                                                                   tmp_path: Path):
    """The reap runs BEFORE the exit post, and until this test nothing said so.

    `worktree.reap` computes the diff shape against the base once salvage has committed, and
    `_report_exits` reads it off the child. Reverse the two calls in `watch_tick` and every
    exit post carries a null shape: the capability rollups lose the `diff_shape` axis this
    slice exists to add (PRD-41 S1), the page renders "not reported", and nothing anywhere
    fails. That is how the two came to be ordered differently on two branches at once —
    six tests covered the reaping and six covered the reporting, and none covered the seam.

    Sabotage: swap `_reap_exited` and `_report_exits` in `watch_tick`. This fails; measured,
    nothing else in the fleet suite does.
    """
    fleet, posts = recorded
    tree = _dirty_worktree(git_repo, tmp_path)
    child = _exited()
    child.worktree, child.branch, child.base = tree.path, tree.branch, tree.base

    _tick(fleet.client, [child])

    exits = [body for path, body in posts
             if path == "/api/fleet/attempts" and "exit_meaning" in body]
    assert exits, f"no exit post at all: {posts}"
    shape = exits[-1].get("diff_shape")
    assert shape, "the exit post carried no diff shape — the reap must run before the report"
    # The salvage committed one new file, so the shape describes it rather than an empty diff.
    assert shape["files_added"] == 1, shape
    assert "note.md" in shape["added"], shape
