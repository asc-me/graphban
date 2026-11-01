"""A salvage belongs to the item that produced it, and a push must not move a landed fix.

GRPH-1053. Two field failures, both data-loss:

* The next child of a bound seat started on whatever salvage was nearest, so one item's
  work was resumed under another item.
* A salvage commit, or a child that kept editing after the fix had landed, was pushed
  onto the branch the fix already used. The tip moved. The fix was gone.

The calls under test are `up` (bound resume), `publish_salvaged`, `watch_tick`,
`adopt.recover`, and MCP `spawn`. A helper that is right and never called is the bug.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from gbfleet import adopt
from gbfleet.adopt import Snapshot, save
from gbfleet.client import NotPermitted
from gbfleet.mcp import Fleet
from gbfleet.seat import Seat
from gbfleet.spawn import Child, Reason
from gbfleet.supervisor import Limits, Wave, up, watch_tick
from gbfleet.worktree import create, reap, salvage_message
from gbfleet import supervisor as sup

from tests.test_mcp import _call, _value
from tests.test_supervisor import KEY, _factory, _server


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _bare(git_repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(git_repo, "remote", "add", "origin", str(bare))
    _git(git_repo, "push", "-q", "origin", "main")
    _git(git_repo, "remote", "set-head", "origin", "main")
    return bare


def _commit(path: Path, name: str, body: str, message: str) -> str:
    (path / name).write_text(body, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", message)
    return _git(path, "rev-parse", "HEAD")


class _Ledger:
    """Enough of a ledger for one watch tick or one salvage publish."""

    def __init__(self, *, done: str = "", review: str = "", landed: str = ""):
        self.allowed = frozenset({
            "search_items", "get_item_details", "fleet_status", "update_item",
            "propose_allocation",
        })
        self.done = done
        self.review = review
        self.landed = landed
        self.calls: list[tuple] = []

    def fleet_status(self):
        return {"agents": [], "presence_ttl_seconds": 150}

    def post_attempt(self, **payload):
        self.calls.append(("post_attempt", payload))

    def post_observation(self, **payload):
        self.calls.append(("post_observation", payload))
        return {"recorded": True}

    def call(self, tool, **kwargs):
        self.calls.append((tool, kwargs))
        if tool == "search_items":
            status = kwargs.get("status")
            if status == "done" and self.done:
                return {"results": [{"id": self.done, "status": "done"}]}
            if status == "review" and self.review:
                return {"results": [{"id": self.review, "status": "review"}]}
            return {"results": []}
        if tool == "get_item_details" and self.landed:
            return {"id": kwargs.get("id"), "evidence": [{"kind": "attestation", "commit": self.landed}]}
        return {}


def _sleeping_child(tree, tmp_path: Path, item: str) -> tuple[Child, subprocess.Popen]:
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    child = Child(
        adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
        seat_path=tmp_path / "seat.json", process=proc, started_at=time.monotonic(),
        log_dir=tmp_path / "logs", held_items=[item],
    )
    child.log_dir.mkdir(parents=True, exist_ok=True)
    return child, proc


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_a_bound_seat_does_not_resume_another_items_salvage(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """THE CALL. `up` used to pop the first salvage whatever the seat was bound to.

    Sabotage: in `_start`, resume `resumes.pop(0)` and ignore `seat.item`. This child
    starts on GRPH-1's branch and `wip.py` is in the tree it was cut from.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dead = create(git_repo, workspace / "dead", "wave", "1")
    (dead.path / "wip.py").write_text("keep-me\n", encoding="utf-8")
    reap(dead, message=salvage_message("fake", ["GRPH-1"]))

    bound = Seat(code="WORKER-7F3K-0", server_url="http://gb.invalid", api_key=KEY, item="GRPH-2")
    wave = up(
        git_repo, [bound], _factory(scripts, "works_then_exits"),
        _server(workspace, items=[
            {"id": "GRPH-1", "status": "next", "claimed_by": ""},
            {"id": "GRPH-2", "status": "next", "claimed_by": ""},
        ]),
        limits=Limits(max_workers=1), state=state, workspace=workspace,
        items={
            "GRPH-1": {"status": "next", "claimed_by": ""},
            "GRPH-2": {"status": "next", "claimed_by": ""},
        },
    )

    assert wave.resumed == [], wave.resume_misses
    assert wave.spawned, wave.failures
    assert wave.spawned[0].branch != dead.branch
    assert wave.spawned[0].branch.startswith("gb/wave-")


def test_a_bound_seat_resumes_the_salvage_that_names_its_item(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The same seat, the matching item. Binding must not refuse every salvage."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dead = create(git_repo, workspace / "dead", "wave", "1")
    (dead.path / "wip.py").write_text("keep-me\n", encoding="utf-8")
    reap(dead, message=salvage_message("fake", ["GRPH-1"]))

    bound = Seat(code="WORKER-7F3K-0", server_url="http://gb.invalid", api_key=KEY, item="GRPH-1")
    wave = up(
        git_repo, [bound], _factory(scripts, "works_then_exits"),
        _server(workspace, items=[{"id": "GRPH-1", "status": "next", "claimed_by": ""}]),
        limits=Limits(max_workers=1), state=state, workspace=workspace,
        items={"GRPH-1": {"status": "next", "claimed_by": ""}},
    )

    assert wave.resumed == [dead.branch], wave.resume_misses
    assert wave.spawned[0].branch == dead.branch


def test_publish_diverts_when_the_trunk_already_holds_the_fix(git_repo: Path, tmp_path: Path):
    """A salvage whose base does not contain the landed commit must not move that branch.

    The original ref stays at the fix. The rework is pushed under another name, and the
    receipt names that name. It is not a pull request.
    """
    bare = _bare(git_repo, tmp_path)
    tree = create(git_repo, tmp_path / "kid", "wave", "1")
    landed = _commit(tree.path, "fix.py", "the fix\n", "GRPH-1: the fix")
    _git(git_repo, "merge", "--ff-only", tree.branch)
    _git(git_repo, "push", "-q", "origin", "main")
    _git(git_repo, "push", "-q", "origin", f"{tree.branch}:{tree.branch}")
    tip = _commit(tree.path, "again.py", "rework\n",
                  salvage_message("fake", ["GRPH-1"]))

    client = _Ledger(landed=landed)
    wave = Wave()
    sup.publish_salvaged(
        wave, git_repo,
        [adopt.Salvaged(tree.branch, tree.base, ["GRPH-1"])],
        client=client,
    )

    side = f"{tree.branch}--GRPH-1--late"
    assert _git(bare, "rev-parse", tree.branch) == landed, "the landed branch moved"
    assert _git(bare, "rev-parse", side) == tip
    assert "propose_branch" not in __import__("inspect").getsource(sup.publish_salvaged)
    updates = [c for c in client.calls if c[0] == "post_observation"]
    assert updates and updates[0][1]["item_id"] == "GRPH-1"
    assert updates[0][1]["kind"] == "salvage"
    assert updates[0][1]["branch"] == side
    assert "url" not in updates[0][1]
    assert any("diverted" in line for line in wave.failures), wave.failures


def test_publish_keeps_the_branch_when_nothing_has_landed(git_repo: Path, tmp_path: Path):
    """The ordinary salvage — one commit past its base, nothing in the trunk — still
    publishes as itself. Diverting it would hide the only copy of the work."""
    bare = _bare(git_repo, tmp_path)
    tree = create(git_repo, tmp_path / "kid", "wave", "1")
    tip = _commit(tree.path, "wip.py", "only the salvage\n", salvage_message("fake", ["GRPH-1"]))

    client = _Ledger()
    wave = Wave()
    sup.publish_salvaged(
        wave, git_repo, [adopt.Salvaged(tree.branch, tree.base, ["GRPH-1"])], client=client,
    )

    assert _git(bare, "rev-parse", tree.branch) == tip
    assert not any("diverted" in line for line in wave.failures), wave.failures
    updates = [c for c in client.calls if c[0] == "post_observation"]
    assert updates[0][1]["kind"] == "salvage"
    assert updates[0][1]["branch"] == tree.branch


def test_a_child_whose_item_is_done_is_stopped_before_it_can_move_the_branch(
    git_repo: Path, tmp_path: Path
):
    """THE CALL for the stop. `watch_tick` must stop the process, and the reap that
    follows in the same tick must not push the new tip onto the landed branch.

    Sabotage: delete `_stop_landed_children` from `watch_tick` and the process is still
    running. Sabotage: push `child.branch` anyway and the remote tip is the rework.
    """
    bare = _bare(git_repo, tmp_path)
    tree = create(git_repo, tmp_path / "kid", "wave", "1")
    landed = _commit(tree.path, "fix.py", "the fix\n", "GRPH-1: the fix")
    _git(git_repo, "merge", "--ff-only", tree.branch)
    _git(git_repo, "push", "-q", "origin", "main")
    _git(git_repo, "push", "-q", "origin", f"{tree.branch}:{tree.branch}")
    _commit(tree.path, "again.py", "rework\n", "GRPH-1: still editing")

    child, proc = _sleeping_child(tree, tmp_path, "GRPH-1")
    try:
        watch_tick(Wave(), [child], Limits(), _Ledger(done="GRPH-1", landed=landed))
    finally:
        _stop_proc(proc)

    assert child.stopped_because is Reason.ITEM_LANDED
    assert not child.running
    assert _git(bare, "rev-parse", tree.branch) == landed


def test_a_child_in_review_with_nothing_landed_stays_up_and_is_pushed(
    git_repo: Path, tmp_path: Path
):
    """`review` is not `done`. Stopping here would fail the early publish (GRPH-987)
    and the same process reviewing other work (PRD-39)."""
    bare = _bare(git_repo, tmp_path)
    tree = create(git_repo, tmp_path / "kid", "wave", "1")
    tip = _commit(tree.path, "fix.py", "the work\n", "GRPH-1: the work a reviewer needs")

    child, proc = _sleeping_child(tree, tmp_path, "GRPH-1")
    try:
        wave = Wave()
        watch_tick(wave, [child], Limits(), _Ledger(review="GRPH-1"))
        assert child.running
        assert child.stopped_because is None
        assert _git(bare, "rev-parse", tree.branch) == tip
        assert wave.published_in_review.get(tree.branch) == tip
    finally:
        _stop_proc(proc)


def test_recover_names_each_held_item_on_its_own_branch(
    git_repo: Path, tmp_path: Path, state: Path
):
    """Two held items are two salvages. Both receipts used to name the first branch."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    (tree.path / "feature.py").write_text("print('both')\n", encoding="utf-8")
    save(adopt.children_path(git_repo, state), [Snapshot(
        pid=2 ** 22, start_token="tok", worktree=str(tree.path), branch=tree.branch,
        adapter="fake", slot="1", base=tree.base, started_wall=time.time(),
        held_items=["GRPH-1", "GRPH-2"],
    )])

    got = adopt.recover(git_repo, workspace, state)

    assert [s.items for s in got.salvaged] == [["GRPH-1"], ["GRPH-2"]]
    assert got.salvaged[0].branch == tree.branch
    assert got.salvaged[1].branch != tree.branch
    assert got.salvaged[1].branch.endswith("GRPH-2")


def test_spawn_with_an_item_does_not_resume_a_different_salvage(
    git_repo: Path, tmp_path: Path, scripts
):
    """MCP spawn passed `item` and still took `resumes[0]`. Same defect as `up`."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dead = create(git_repo, workspace / "dead", "wave", "1")
    (dead.path / "wip.py").write_text("keep-me\n", encoding="utf-8")
    reap(dead, message=salvage_message("fake", ["GRPH-1"]))

    fleet = Fleet(
        repo=git_repo, workspace=workspace,
        client=_server(workspace, items=[{"id": "GRPH-1", "status": "next", "claimed_by": ""}]),
        launch_for=lambda name, model="", tuning=None: _factory(
            scripts, "works_then_waits", adapter=name
        ),
    )
    value = _value(_call(
        fleet, "spawn", adapter="fake", enrolment_code="WORKER-1", item="GRPH-2",
    ))
    try:
        assert value["branch"] != dead.branch, value
        assert not (Path(value["worktree"]) / "wip.py").exists()
    finally:
        _call(fleet, "stop", agent_id=value["agent_id"])


def _landed_then_rework(git_repo: Path, tmp_path: Path, *, message: str) -> tuple[Path, object, str, str]:
    """Fix merged to the trunk and pushed on its branch, then one more commit on the branch."""
    bare = _bare(git_repo, tmp_path)
    tree = create(git_repo, tmp_path / "kid", "wave", "1")
    landed = _commit(tree.path, "fix.py", "the fix\n", "GRPH-1: the fix")
    _git(git_repo, "merge", "--ff-only", tree.branch)
    _git(git_repo, "push", "-q", "origin", "main")
    _git(git_repo, "push", "-q", "origin", f"{tree.branch}:{tree.branch}")
    tip = _commit(tree.path, "again.py", "rework\n", message)
    return bare, tree, landed, tip


class _Refused(_Ledger):
    """`get_item_details` is on the allowlist and the call is still refused."""

    def call(self, tool, **kwargs):
        if tool == "get_item_details":
            raise NotPermitted("this credential may not call 'get_item_details'")
        return super().call(tool, **kwargs)


class _Unfetched(_Ledger):
    """The item was read. Its evidence names a sha this clone has never fetched."""

    def call(self, tool, **kwargs):
        if tool == "get_item_details":
            return {"id": kwargs.get("id"), "evidence": [
                {"kind": "attestation", "commit": "a" * 40},
            ]}
        return super().call(tool, **kwargs)


def test_up_reads_the_item_before_the_reap_pushes(git_repo: Path, tmp_path: Path):
    """THE UP CALL. `gbfleet up` watches with `SPAWN_READS`. That set has to include
    `get_item_details`, and this tick has to call it — a ledger that happens to allow
    the tool is not the client `up` builds.

    Sabotage: drop `get_item_details` from `SPAWN_READS`. The call is absent and this
    fails, even if a client built by hand still diverts.
    """
    from gbfleet.cli import SPAWN_READS

    assert "get_item_details" in SPAWN_READS
    bare, tree, landed, _tip = _landed_then_rework(
        git_repo, tmp_path, message="GRPH-1: still editing")
    client = _Ledger(done="GRPH-1", landed=landed)
    client.allowed = SPAWN_READS
    child, proc = _sleeping_child(tree, tmp_path, "GRPH-1")
    try:
        wave = Wave()
        watch_tick(wave, [child], Limits(), client)
    finally:
        _stop_proc(proc)

    assert any(name == "get_item_details" for name, _ in client.calls), client.calls
    assert _git(bare, "rev-parse", tree.branch) == landed
    assert wave.landed_unmeasured == "", wave.landed_unmeasured


def test_a_client_that_may_not_read_items_does_not_push_the_rework(
    git_repo: Path, tmp_path: Path
):
    """`search_items` is enough to stop the child. It is not enough to see the landed
    commit. The reap that follows must not push the new tip onto that branch.

    Sabotage: in `_push_target`, pass `landed=[]` when the read did not run. The
    remote tip becomes the rework.
    """
    bare, tree, landed, _tip = _landed_then_rework(
        git_repo, tmp_path, message="GRPH-1: still editing")
    client = _Ledger(done="GRPH-1", landed=landed)
    client.allowed = frozenset({"search_items", "fleet_status", "update_item", "release_item"})
    child, proc = _sleeping_child(tree, tmp_path, "GRPH-1")
    try:
        wave = Wave()
        watch_tick(wave, [child], Limits(), client)
    finally:
        _stop_proc(proc)

    assert child.stopped_because is Reason.ITEM_LANDED
    assert _git(bare, "rev-parse", tree.branch) == landed
    assert wave.landed_unmeasured
    assert any(wave.landed_unmeasured in line for line in wave.failures), wave.failures
    assert any("diverted" in line for line in wave.failures), wave.failures
    assert not any(name == "get_item_details" for name, _ in client.calls)


def test_not_permitted_does_not_take_the_unfetched_sha_path(git_repo: Path, tmp_path: Path):
    """Same tree, two reads. A sha this clone has never fetched is left out and the
    rework is published on the branch. `NotPermitted` is not that read: the branch
    the fix uses stays where it was, and the wave says the check did not run.

    Sabotage: catch `NotPermitted` and return `[], ""`. This pushes the rework and
    `landed_unmeasured` stays empty.
    """
    import io

    from gbfleet.cli import report

    bare, tree, landed, tip = _landed_then_rework(
        git_repo, tmp_path, message="GRPH-1: still editing")

    unread = _Refused(done="GRPH-1", landed=landed)
    child, proc = _sleeping_child(tree, tmp_path, "GRPH-1")
    try:
        refused = Wave()
        watch_tick(refused, [child], Limits(), unread)
    finally:
        _stop_proc(proc)

    assert _git(bare, "rev-parse", tree.branch) == landed
    assert refused.landed_unmeasured
    assert "not permitted" in refused.landed_unmeasured
    assert any(refused.landed_unmeasured in line for line in refused.failures), refused.failures
    printed = io.StringIO()
    report(refused, out=printed)
    assert "LANDED unmeasured:" in printed.getvalue()
    assert "not permitted" in printed.getvalue()

    # The other path, on the same repo: the remote is still the landed fix and the
    # local tip is the rework. The sha cannot be resolved, the read itself succeeded,
    # and the branch moves. Diverting here would be the fresh-clone behaviour the
    # omission exists to avoid.
    client = _Unfetched()
    wave = Wave()
    sup.publish_salvaged(
        wave, git_repo, [adopt.Salvaged(tree.branch, tree.base, ["GRPH-1"])], client=client,
    )
    assert _git(bare, "rev-parse", tree.branch) == tip
    assert wave.landed_unmeasured == ""
    assert not any("did not run" in line for line in wave.failures), wave.failures


def test_a_skipped_landed_check_is_in_the_wave_report():
    """Stored and never shown is the same as not stored. Both surfaces an operator
    or a machine reads have to carry it, including when nothing happened.

    Sabotage: drop the key from `Report.as_json`, or the print from `cli.report`.
    """
    import io

    from gbfleet.cli import report
    from gbfleet.until import Report

    text = "GRPH-1: landed-commit check did not run (not permitted to read the item)"
    wave = Wave()
    wave.landed_unmeasured = text
    buf = io.StringIO()
    report(wave, out=buf)
    assert f"LANDED unmeasured: {text}" in buf.getvalue()

    payload = Report(ok=False, reason="failed", exit=1, wave=wave).as_json()
    assert payload["landed_unmeasured"] == text
    assert Report(ok=True, reason="idle", exit=0, wave=Wave()).as_json()["landed_unmeasured"] == ""
    assert Report(ok=True, reason="idle", exit=0).as_json()["landed_unmeasured"] == ""
