"""P30 D7 — takeover adopts live PIDs instead of spawning beside them."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gbfleet import adopt
from gbfleet.adopt import Snapshot, UnadoptableFile, classify, load, save
from gbfleet.lock import hold
from gbfleet.supervisor import Limits, up, watch_tick
from gbfleet.worktree import create

from tests.test_supervisor import _factory, _seats, _server


def test_a_missing_file_is_empty_and_a_corrupt_file_is_not(tmp_path: Path):
    """Partial file ≠ empty roster. An empty reading is how takeover starts blind."""
    path = tmp_path / "children.json"
    assert load(path) == []

    path.write_text("{not json", encoding="utf-8")
    got = load(path)
    assert isinstance(got, UnadoptableFile)

    path.write_text(json.dumps({"generation": 99, "children": []}), encoding="utf-8")
    got = load(path)
    assert isinstance(got, UnadoptableFile)
    assert "generation" in str(got)

    # Missing required fields is unadoptable, not a skipped row (P30 D7 bounce).
    path.write_text(
        json.dumps({"generation": 1, "children": [{"pid": 1, "worktree": "/wt"}]}),
        encoding="utf-8",
    )
    got = load(path)
    assert isinstance(got, UnadoptableFile)
    assert "required" in str(got)


def test_save_is_atomic_and_round_trips(tmp_path: Path):
    path = tmp_path / "children.json"
    snap = Snapshot(
        pid=os.getpid(), worktree="/wt", branch="gb/wave-1", adapter="gbagent",
        start_token="tok", seat_id="seat-1", agent_id="GRPH-A1", slot="1",
    )
    save(path, [snap])
    loaded = load(path)
    assert not isinstance(loaded, UnadoptableFile)
    assert loaded[0].pid == os.getpid()
    assert loaded[0].seat_id == "seat-1"
    assert "WORKER" not in path.read_text()  # never the enrolment code


def test_a_live_pid_with_matching_token_is_attached(tmp_path: Path, monkeypatch):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        monkeypatch.setattr(adopt, "process_start_token", lambda pid: "tok")
        snap = Snapshot(
            pid=proc.pid, start_token="tok", worktree=str(tmp_path),
            branch="gb/w-1", adapter="fake",
        )
        verdict = classify(snap)
        assert verdict.fate == "attached", verdict.why
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_dead_pid_is_gone():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    snap = Snapshot(
        pid=proc.pid, start_token="whatever", worktree="/wt",
        branch="gb/w-1", adapter="fake",
    )
    assert classify(snap).fate == "gone"


def test_a_reused_or_unknown_token_is_unadoptable(monkeypatch):
    """Cannot tell live from reused → do not attach (P30 D7)."""
    monkeypatch.setattr(adopt, "process_start_token", lambda pid: "real-token")
    monkeypatch.setattr(adopt, "pid_is_alive", lambda pid: True)
    snap = Snapshot(
        pid=os.getpid(), start_token="not-this-process",
        worktree="/wt", branch="gb/w-1", adapter="fake",
    )
    assert classify(snap).fate == "unadoptable"

    snap2 = Snapshot(
        pid=os.getpid(), start_token=None,
        worktree="/wt", branch="gb/w-1", adapter="fake",
    )
    assert classify(snap2).fate == "unadoptable"


def test_recover_attaches_a_live_pid(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sleeper = subprocess.Popen(
        [str(scripts["python"]), str(scripts["sleeper"])],
        cwd=str(git_repo),
        start_new_session=True,
    )
    try:
        monkeypatch.setattr(adopt, "process_start_token", lambda pid: "tok")
        monkeypatch.setattr(adopt, "pid_is_alive", lambda pid: pid == sleeper.pid)
        tree = create(git_repo, workspace / "wave-1", "wave", "1")
        path = adopt.children_path(git_repo, state)
        save(path, [Snapshot(
            pid=sleeper.pid, start_token="tok",
            worktree=str(tree.path), branch=tree.branch, adapter="fake",
            slot="1", base=tree.base, log_dir=str(tmp_path / "logs"),
            started_wall=time.time(),
        )])
        leftover, occupied, _notes = adopt.recover(git_repo, workspace, state)
        assert leftover and leftover[0].attached and leftover[0].pid == sleeper.pid
        assert tree.branch in occupied
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)


def test_recover_treats_a_corrupt_file_as_unadoptable_not_empty(
    git_repo: Path, tmp_path: Path, state: Path
):
    """P30 D7 bounce. load() already rejects corrupt JSON; recover() used to be
    untested, so treating UnadoptableFile as [] stayed green — `_start` still skips
    existing gb/ branches on its own. Partial file ≠ empty roster.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    path = adopt.children_path(git_repo, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    leftover, occupied, notes = adopt.recover(git_repo, workspace, state)
    assert leftover == []
    assert tree.branch in occupied, (
        f"corrupt file read as an empty roster; occupied={occupied!r}"
    )
    assert notes, "unadoptable recover must say why"


def test_recover_treats_missing_required_fields_as_unadoptable(
    git_repo: Path, tmp_path: Path, state: Path
):
    """A child row missing branch/adapter is the whole file, not a skipped row."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    path = adopt.children_path(git_repo, state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "generation": 1,
            "children": [{"pid": 1, "worktree": str(tree.path)}],
        }),
        encoding="utf-8",
    )
    leftover, occupied, notes = adopt.recover(git_repo, workspace, state)
    assert leftover == []
    assert tree.branch in occupied, (
        f"partial row read as empty roster; occupied={occupied!r}"
    )
    assert notes


def test_recover_does_not_attach_a_reused_token(
    git_repo: Path, tmp_path: Path, state: Path, monkeypatch
):
    """classify() already says unadoptable; recover() used to be untested, so
    attaching a reused token still left 9/9 green.
    """
    monkeypatch.setattr(adopt, "process_start_token", lambda pid: "real-token")
    monkeypatch.setattr(adopt, "pid_is_alive", lambda pid: True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    path = adopt.children_path(git_repo, state)
    save(path, [Snapshot(
        pid=os.getpid(), start_token="not-this-process",
        worktree=str(tree.path), branch=tree.branch, adapter="fake",
        slot="1", base=tree.base,
    )])
    leftover, occupied, notes = adopt.recover(git_repo, workspace, state)
    assert leftover == [], (
        f"reused token was attached: {[(c.pid, c.attached) for c in leftover]}"
    )
    assert tree.branch in occupied
    assert notes


def test_takeover_does_not_spawn_onto_the_previous_slot(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """THE LOAD-BEARING TEST. Logging takeover and starting a new wave refuses
    `gb/<wave>-<slot>` (never force). recover() marks that branch occupied; `_start`
    skips it. Sabotage: skip recover(); this raises BranchExists.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    (tree.path / "wip.py").write_text("x = 1\n", encoding="utf-8")
    path = adopt.children_path(git_repo, state)
    save(path, [Snapshot(
        pid=999_999_999, start_token="gone",
        worktree=str(tree.path), branch=tree.branch, adapter="fake",
        slot="1", base=tree.base,
    )])
    with hold(git_repo, state) as first:
        lock, holder = first.path, first.holder
    lock.write_text(holder.as_json(), encoding="utf-8")

    second = up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"),
        _server(workspace), limits=Limits(max_workers=2),
        state=state, workspace=workspace, poll=0.05,
    )
    assert second.spawned, "expected a new child on a free slot"
    assert all(c.branch != tree.branch for c in second.spawned), (
        f"spawned onto the leftover branch: {[c.branch for c in second.spawned]}"
    )


def test_takeover_attaches_the_leftover_pid(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """P30 D7 bounce. Skipping recover used to pass: `_start` skipped the existing
    branch and spawned an unattached sibling. The CALL is attach of the leftover pid.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sleeper = subprocess.Popen(
        [str(scripts["python"]), str(scripts["sleeper"])],
        cwd=str(git_repo),
        start_new_session=True,
    )
    try:
        monkeypatch.setattr(adopt, "process_start_token", lambda pid: "tok")
        monkeypatch.setattr(adopt, "pid_is_alive", lambda pid: pid == sleeper.pid)
        tree = create(git_repo, workspace / "wave-1", "wave", "1")
        path = adopt.children_path(git_repo, state)
        save(path, [Snapshot(
            pid=sleeper.pid, start_token="tok",
            worktree=str(tree.path), branch=tree.branch, adapter="fake",
            slot="1", base=tree.base, log_dir=str(tmp_path / "logs"),
            started_wall=time.time(),
        )])
        with hold(git_repo, state) as first:
            lock, holder = first.path, first.holder
        lock.write_text(holder.as_json(), encoding="utf-8")

        import gbfleet.supervisor as sup
        monkeypatch.setattr(sup, "_wait_out", lambda *a, **k: None)
        monkeypatch.setattr(sup, "_reap_all", lambda *a, **k: None)

        second = up(
            git_repo, _seats(1), _factory(scripts, "works_then_exits"),
            _server(workspace), limits=Limits(max_workers=2),
            state=state, workspace=workspace, poll=0.05,
        )
        attached = [c for c in second.spawned if c.attached and c.pid == sleeper.pid]
        assert attached, (
            f"takeover did not attach pid {sleeper.pid}; spawned "
            f"{[(c.pid, c.attached, c.branch) for c in second.spawned]}"
        )
        assert all(c.branch != tree.branch or c.attached for c in second.spawned)
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)


def test_takeover_ticks_the_leftover_pid(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """P30 D7 second bounce. leftover on wave.spawned is a report, not consumption.
    Setting `children = []` instead of `list(leftover)` left the attach test green:
    the leftover pid sat on wave.spawned while persist-before-start and `_wait_out`
    never saw it. The CALL is leftover in the list the watch loop actually ticks,
    still in the children file after persist.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sleeper = subprocess.Popen(
        [str(scripts["python"]), str(scripts["sleeper"])],
        cwd=str(git_repo),
        start_new_session=True,
    )
    try:
        monkeypatch.setattr(adopt, "process_start_token", lambda pid: "tok")
        monkeypatch.setattr(adopt, "pid_is_alive", lambda pid: pid == sleeper.pid)
        tree = create(git_repo, workspace / "wave-1", "wave", "1")
        path = adopt.children_path(git_repo, state)
        save(path, [Snapshot(
            pid=sleeper.pid, start_token="tok",
            worktree=str(tree.path), branch=tree.branch, adapter="fake",
            slot="1", base=tree.base, log_dir=str(tmp_path / "logs"),
            started_wall=time.time(),
        )])
        with hold(git_repo, state) as first:
            lock, holder = first.path, first.holder
        lock.write_text(holder.as_json(), encoding="utf-8")

        import gbfleet.supervisor as sup
        ticked: list = []

        def capture_wait(wave, children, limits, client, **kwargs):
            ticked.extend(children)
            watch_tick(
                wave, children, limits, client,
                debug=kwargs.get("debug", False),
                persist=kwargs.get("persist"),
            )

        monkeypatch.setattr(sup, "_wait_out", capture_wait)
        monkeypatch.setattr(sup, "_reap_all", lambda *a, **k: None)

        second = up(
            git_repo, _seats(1), _factory(scripts, "works_then_exits"),
            _server(workspace), limits=Limits(max_workers=2),
            state=state, workspace=workspace, poll=0.05,
        )
        attached = [c for c in ticked if c.attached and c.pid == sleeper.pid]
        assert attached, (
            f"watch loop never ticked leftover pid {sleeper.pid}; ticked "
            f"{[(c.pid, c.attached, c.branch) for c in ticked]}; spawned "
            f"{[(c.pid, c.attached, c.branch) for c in second.spawned]}"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        pids = [int(row["pid"]) for row in data.get("children") or []]
        assert sleeper.pid in pids, f"persist dropped leftover pid; file has {pids}"
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)


def test_a_child_is_in_the_children_file_before_it_registers(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """Crash during await_registration must not leave a live pid with no JSON record."""
    from gbfleet import spawn as spawn_mod
    import gbfleet.supervisor as sup

    seen: list[list[int]] = []
    real_await = spawn_mod.await_registration

    def await_and_check(child, *args, **kwargs):
        path = adopt.children_path(git_repo, state)
        assert path.exists(), "children file was not written before registration"
        data = json.loads(path.read_text(encoding="utf-8"))
        pids = [int(row["pid"]) for row in data.get("children") or []]
        seen.append(pids)
        assert child.pid in pids, f"pid {child.pid} missing from {pids}"
        return real_await(child, *args, **kwargs)

    monkeypatch.setattr(sup, "await_registration", await_and_check)
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"),
        _server(workspace), limits=Limits(max_workers=1),
        state=state, workspace=workspace, poll=0.05,
    )
    assert seen, "await_registration was never reached"
