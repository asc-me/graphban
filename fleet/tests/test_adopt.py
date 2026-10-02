"""P30 D7 — takeover adopts live PIDs instead of spawning beside them."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gbfleet import adopt
from gbfleet.adopt import Snapshot, UnadoptableFile, classify, load, save
from gbfleet.hostos import spawn_kwargs
from gbfleet.lock import RepoLocked, hold
from gbfleet.supervisor import Limits, up, watch_tick
from gbfleet.worktree import create, registered_worktrees

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
        **spawn_kwargs(),
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
        **spawn_kwargs(),
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
        **spawn_kwargs(),
    )
    try:
        # classify() reads adopt.*; AttachedProcess.poll / persist's running
        # check read spawn.* (imported from hostos). Mocking only adopt lets a
        # Linux hostos token ≠ "tok" mark leftover dead, so persist drops it
        # while wave.spawned still reports attach — the same hole as children=[].
        from gbfleet import spawn as spawn_mod

        def _tok(_pid: int) -> str:
            return "tok"

        def _alive(pid: int) -> bool:
            return pid == sleeper.pid

        monkeypatch.setattr(adopt, "process_start_token", _tok)
        monkeypatch.setattr(adopt, "pid_is_alive", _alive)
        monkeypatch.setattr(spawn_mod, "process_start_token", _tok)
        monkeypatch.setattr(spawn_mod, "pid_is_alive", _alive)
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


class _StopLoop(Exception):
    """until.run has no bounded-tick; tests raise this from sleep once leftover is seen."""


def test_until_ticks_the_leftover_pid(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """P30 D7 bounce. until.run() does wave.spawned.extend(leftover) then
    children=list(leftover). children=[] there left adopt+until green: persist and
    watch_tick dropped the pid while wave.spawned still reported attach.
    """
    from gbfleet import spawn as spawn_mod
    from gbfleet import until as until_mod
    from gbfleet.until import run
    from tests.test_until import KEY, _clients

    workspace = tmp_path / "ws"
    workspace.mkdir()
    sleeper = subprocess.Popen(
        [str(scripts["python"]), str(scripts["sleeper"])],
        cwd=str(git_repo),
        **spawn_kwargs(),
    )
    try:
        def _tok(_pid: int) -> str:
            return "tok"

        def _alive(pid: int) -> bool:
            return pid == sleeper.pid

        monkeypatch.setattr(adopt, "process_start_token", _tok)
        monkeypatch.setattr(adopt, "pid_is_alive", _alive)
        monkeypatch.setattr(spawn_mod, "process_start_token", _tok)
        monkeypatch.setattr(spawn_mod, "pid_is_alive", _alive)
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

        ticked: list = []
        real_watch = until_mod.watch_tick

        def capture_watch(wave, children, limits, supervisor, **kwargs):
            ticked.extend(children)
            return real_watch(wave, children, limits, supervisor, **kwargs)

        monkeypatch.setattr(until_mod, "watch_tick", capture_watch)

        def sleep_fn(_dt: float) -> None:
            if any(c.attached and c.pid == sleeper.pid for c in ticked):
                raise _StopLoop()

        planner, supervisor = _clients(workspace)
        try:
            run(
                git_repo, _factory(scripts, "works_then_exits"),
                planner, supervisor, api_key=KEY, server="http://gb.invalid",
                adapter="fake", state=state, workspace=workspace, poll=0,
                sleep=sleep_fn, empty_ticks=3, limits=Limits(max_workers=1),
            )
        except _StopLoop:
            pass
        attached = [c for c in ticked if c.attached and c.pid == sleeper.pid]
        assert attached, (
            f"until watch loop never ticked leftover pid {sleeper.pid}; ticked "
            f"{[(c.pid, c.attached, c.branch) for c in ticked]}"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        pids = [int(row["pid"]) for row in data.get("children") or []]
        assert sleeper.pid in pids, f"until persist dropped leftover pid; file has {pids}"
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)


def test_mcp_ticks_the_leftover_pid(
    git_repo: Path, tmp_path: Path, scripts, monkeypatch,
):
    """P30 D7 bounce. gbfleet mcp extends leftover onto fleet.children; that CALL
    was untested. children=[] equivalent: serve/ps/tick never see the pid.
    """
    from gbfleet import cli
    from gbfleet import spawn as spawn_mod
    from tests.test_supervisor import KEY

    workspace = tmp_path / "ws"
    workspace.mkdir()
    sleeper = subprocess.Popen(
        [str(scripts["python"]), str(scripts["sleeper"])],
        cwd=str(git_repo),
        **spawn_kwargs(),
    )
    try:
        def _tok(_pid: int) -> str:
            return "tok"

        def _alive(pid: int) -> bool:
            return pid == sleeper.pid

        monkeypatch.setattr(adopt, "process_start_token", _tok)
        monkeypatch.setattr(adopt, "pid_is_alive", _alive)
        monkeypatch.setattr(spawn_mod, "process_start_token", _tok)
        monkeypatch.setattr(spawn_mod, "pid_is_alive", _alive)
        tree = create(git_repo, workspace / "wave-1", "wave", "1")
        path = adopt.children_path(git_repo)
        save(path, [Snapshot(
            pid=sleeper.pid, start_token="tok",
            worktree=str(tree.path), branch=tree.branch, adapter="fake",
            slot="1", base=tree.base, log_dir=str(tmp_path / "logs"),
            started_wall=time.time(),
        )])
        with hold(git_repo) as first:
            lock, holder = first.path, first.holder
        lock.write_text(holder.as_json(), encoding="utf-8")

        seen: dict = {}

        def fake_serve(fleet) -> None:
            seen["children"] = list(fleet.children)
            fleet.tick()
            seen["file"] = path.read_text(encoding="utf-8")

        monkeypatch.setattr(cli, "serve", fake_serve)
        monkeypatch.setenv("GBFLEET_API_KEY", KEY)
        monkeypatch.chdir(git_repo)
        code = cli.main([
            "mcp", "--repo", str(git_repo), "--server", "http://gb.invalid",
            "--workspace", str(workspace),
        ])
        assert code == 0, seen
        attached = [c for c in seen.get("children") or [] if c.attached and c.pid == sleeper.pid]
        assert attached, (
            f"mcp serve never received leftover pid {sleeper.pid}; children "
            f"{[(c.pid, c.attached, c.branch) for c in seen.get('children') or []]}"
        )
        data = json.loads(seen["file"])
        pids = [int(row["pid"]) for row in data.get("children") or []]
        assert sleeper.pid in pids, f"mcp tick persist dropped leftover pid; file has {pids}"
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


def test_wall_clock_cap_round_trips_through_snapshot(tmp_path: Path):
    """GRPH-849 bounce. snapshot_of and _as_dict omitted wall_clock_cap, so a
    restarted supervisor dropped a 4h override back to 3600s. Sabotage: remove
    wall_clock_cap from _as_dict or _parse_row; this fails.
    """
    path = tmp_path / "children.json"
    snap = Snapshot(
        pid=os.getpid(), worktree="/wt", branch="gb/wave-1", adapter="gbagent",
        start_token="tok", seat_id="seat-1", agent_id="GRPH-A1", slot="1",
        wall_clock_cap=14400.0,
    )
    save(path, [snap])
    loaded = load(path)
    assert not isinstance(loaded, UnadoptableFile)
    assert loaded[0].wall_clock_cap == 14400.0

    snap_none = Snapshot(
        pid=os.getpid(), worktree="/wt", branch="gb/wave-2", adapter="gbagent",
        start_token="tok", seat_id="seat-2", agent_id="GRPH-A2", slot="2",
    )
    save(path, [snap_none])
    loaded2 = load(path)
    assert not isinstance(loaded2, UnadoptableFile)
    assert loaded2[0].wall_clock_cap is None


def test_wall_clock_cap_survives_an_older_supervisor(tmp_path: Path):
    """A children file written before wall_clock_cap existed has no such key.
    _parse_row must tolerate that and return None, not crash.
    """
    path = tmp_path / "children.json"
    payload = {
        "generation": 1,
        "children": [{
            "pid": os.getpid(),
            "start_token": "tok",
            "worktree": "/wt",
            "branch": "gb/wave-1",
            "adapter": "gbagent",
            "seat_id": "seat-1",
            "agent_id": "GRPH-A1",
            "slot": "1",
            "base": "",
            "seat_path": "",
            "log_dir": "",
            "started_wall": 0.0,
            "held_items": [],
        }],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load(path)
    assert not isinstance(loaded, UnadoptableFile)
    assert loaded[0].wall_clock_cap is None


# --- GRPH-1011: gbfleet gc -----------------------------------------------------------
#
# `worktree.reap` runs inside the supervisor that owns the repository, and `recover`
# runs it again only when a LATER supervisor takes the same lock. A clone nobody starts
# a supervisor on again therefore keeps every worktree it ever cut. These drive
# `adopt.sweep`, which is what `gc` is and what `up`/`until`/`mcp` run at startup.


def _dead_pid() -> int:
    """A pid that was ours and is not any more.

    The takeover tests above plant a made-up large number, which answers the same
    question. A real exited child is what `gc` actually meets, so that is what these use.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def _snap(tree, pid: int, slot: str = "1", log_dir: str = "") -> Snapshot:
    return Snapshot(
        pid=pid, worktree=str(tree.path), branch=tree.branch, adapter="fake",
        slot=slot, base=tree.base, log_dir=log_dir, started_wall=time.time(),
    )


def _lock_worktree(repo: Path, tree) -> None:
    """Make `reap` refuse. Its own comment names this as the deliberate human act that
    `--force --force` would walk straight past, so it is the honest way to reach
    `LEFT_DIRTY` without inventing a second refusal path."""
    subprocess.run(["git", "worktree", "lock", str(tree.path)], cwd=repo,
                   check=True, capture_output=True)


def _unlock_worktree(repo: Path, tree) -> None:
    subprocess.run(["git", "worktree", "unlock", str(tree.path)], cwd=repo,
                   check=False, capture_output=True)


def test_gc_reaps_a_clean_worktree_whose_supervisor_pid_is_dead(
    git_repo: Path, tmp_path: Path, state: Path
):
    """THE acceptance case. Sabotage: return False from `_reap_one` without reaping;
    the directory survives and this fails."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    roster = adopt.children_path(git_repo, state)
    save(roster, [_snap(tree, _dead_pid())])

    swept = adopt.sweep(state)

    assert not tree.path.exists(), f"gc left {tree.path} on disk: {swept.lines}"
    assert (swept.removed, swept.repos, swept.records) == (1, 1, 1), swept.lines
    assert not load(roster), "the roster still names a worktree that is gone"
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert str(tree.path) not in listed, listed
    # The clone itself is another agent's supervisor, and one lock per repository is
    # what lets two agents work at once. It is not this command's to delete.
    assert (git_repo / ".git").exists() and (git_repo / "README.md").exists()


def test_gc_leaves_a_repository_whose_lock_is_held(
    git_repo: Path, tmp_path: Path, state: Path
):
    """The other half of the acceptance: a LIVE lock is not touched. Not "its live
    children are spared" — nothing of that repository at all, because a supervisor
    mid-wave owns that roster and rewrites it several times a second.

    Sabotage: drop the `answer.free` branch in `_sweep_roster`; the dead child's tree
    goes and this fails.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    roster = adopt.children_path(git_repo, state)
    save(roster, [_snap(tree, _dead_pid())])
    # A roster this pass will not change must not be REWRITTEN either. The file belongs to
    # a supervisor that may be running on another repository right now, writing it several
    # times a second, and an `os.replace` of our stale copy clobbers whatever it added
    # since we looked. The hand-written key is what makes that visible: `load` tolerates
    # a field it does not know and `save` would drop it.
    raw = json.loads(roster.read_text(encoding="utf-8"))
    raw["children"][0]["hand_written"] = "still here"
    roster.write_text(json.dumps(raw), encoding="utf-8")

    with hold(git_repo, state) as acquired:
        swept = adopt.sweep(state)
        assert (swept.locked, swept.removed) == (1, 0), swept.lines
        assert str(acquired.holder.pid) in " ".join(swept.lines), swept.lines

    assert tree.path.exists(), f"gc reaped under a live lock: {swept.lines}"
    assert [s.branch for s in load(roster)] == [tree.branch], (
        "gc rewrote the roster of a repository a supervisor is running on")
    assert "hand_written" in roster.read_text(encoding="utf-8"), (
        "gc rewrote a roster it had no reason to touch — a live supervisor's own writes "
        "since this pass looked would have been clobbered by the stale copy")


def _sweep_in_a_thread(state: Path):
    """Run a sweep off the main thread, so the main thread can be the supervisor that
    tries to start during it.

    `sweep` does not raise by contract, but a bug in the lock handling would, and a thread
    that died quietly would leave the test hanging at the join instead of naming a cause.
    So the exception is captured and the caller asserts on it.
    """
    result: dict = {}

    def run() -> None:
        try:
            result["swept"] = adopt.sweep(state)
        except BaseException as exc:  # noqa: BLE001 - re-raised by the caller's assert
            result["raised"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, result


def test_a_sweep_holds_the_repository_until_it_has_finished(
    git_repo: Path, tmp_path: Path, state: Path, monkeypatch
):
    """THE BOUNCE. The pass used to ASK the lock and then act on the answer: `probe` took
    the flock and released it by closing its descriptor, so between that answer and the
    reap a supervisor could start on this repository, adopt the tree being removed, and
    then have its own roster replaced by the copy the pass read before it arrived.

    So the pass holds the flock, and a supervisor that starts mid-pass is refused until it
    finishes. Sabotage: close the descriptor before `sweep_hold` yields — probe-then-act —
    and the `hold` below gets in.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    roster = adopt.children_path(git_repo, state)
    save(roster, [_snap(tree, _dead_pid())])

    reached = threading.Event()
    finish = threading.Event()
    listing = registered_worktrees

    def paused(repo):
        # After the lock check and before anything is removed: as wide as the window the
        # bounced code ever opened, and where the reap of a 274 MB tree spends its seconds.
        reached.set()
        assert finish.wait(timeout=60), "the test never let the sweep finish"
        return listing(repo)

    monkeypatch.setattr(adopt, "registered_worktrees", paused)
    worker, result = _sweep_in_a_thread(state)
    try:
        assert reached.wait(timeout=60), f"the sweep never reached {git_repo}"
        with pytest.raises(RepoLocked) as exc:
            with hold(git_repo, state):
                pass
        # The sweeper writes no holder record, so the refusal says what is true of it —
        # something has this repository and has not identified itself — rather than naming
        # a supervisor that died. `sweep_hold` states why, and why there is no retry.
        assert "not yet written its record" in str(exc.value), exc.value
    finally:
        finish.set()
        worker.join(timeout=120)

    assert "raised" not in result, result["raised"]
    swept = result["swept"]
    assert not worker.is_alive(), "the sweep never finished"
    assert not tree.path.exists(), swept.lines
    assert (swept.removed, swept.locked) == (1, 0), swept.lines
    # ...and it is the same repository, not merely a refusal: once the pass ends, a
    # supervisor gets in. A lock a tidy-up leaked would refuse every later wave on this
    # checkout until that process happened to exit.
    with hold(git_repo, state) as acquired:
        assert acquired.takeover is None


def test_the_roster_is_rewritten_under_the_lock_it_was_read_under(
    git_repo: Path, tmp_path: Path, state: Path, monkeypatch
):
    """The other half of the bounce: the REWRITE. `save` replaces the roster with the copy
    this pass read at the start, so a lock released after the last reap would still let a
    supervisor start, record the children it spawned, and have them dropped by a write
    that was decided before it existed — live children named nowhere, which is the exact
    failure `gc` exists to stop.

    Sabotage: close the descriptor before `sweep_hold` yields; the `hold` below gets in
    mid-write. Releasing the lock between the reaping and the writing is what this pins.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    roster = adopt.children_path(git_repo, state)
    save(roster, [_snap(tree, _dead_pid())])

    reached = threading.Event()
    finish = threading.Event()
    writing = save

    def pausing_save(path, snapshots):
        reached.set()
        assert finish.wait(timeout=60), "the test never let the sweep write"
        return writing(path, snapshots)

    monkeypatch.setattr(adopt, "save", pausing_save)
    worker, result = _sweep_in_a_thread(state)
    try:
        assert reached.wait(timeout=60), "the sweep never reached the rewrite"
        assert not tree.path.exists(), "the pass had not finished reaping yet"
        with pytest.raises(RepoLocked):
            with hold(git_repo, state):
                pass
    finally:
        finish.set()
        worker.join(timeout=120)

    assert "raised" not in result, result["raised"]
    assert not load(roster), result["swept"].lines
    with hold(git_repo, state):
        pass


def test_a_roster_whose_trees_are_all_gone_is_still_rewritten_under_its_lock(
    git_repo: Path, tmp_path: Path, state: Path
):
    """WHICH lock guards a roster, when nothing in it resolves to a repository any more.

    The records are all a per-record lock check has to work from, and the clone this
    command exists for has none left: every tree is already gone, so all the pass is doing
    is dropping stale records. It still may not rewrite that file unless it holds the lock,
    because a supervisor starting on that repository writes the same file. So the lock is
    named by the ROSTER — `<key>.children.json` and `<key>.lock` are the same `repo_key` —
    rather than by a record.

    Sabotage: `_lock_beside` returns `<key>.sweep.lock`; the pass holds a file no
    supervisor ever opens, rewrites this roster under a live lock, and the record is gone.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    roster = adopt.children_path(git_repo, state)

    def plant() -> str:
        save(roster, [_snap(tree, _dead_pid())])
        # A key `load` tolerates and `save` drops, so "the file was rewritten" is visible
        # even though the record it names is gone either way.
        raw = json.loads(roster.read_text(encoding="utf-8"))
        raw["children"][0]["hand_written"] = "still here"
        roster.write_text(json.dumps(raw), encoding="utf-8")
        return json.dumps(raw)

    # The control half, first: with nobody holding this repository, the stale record IS
    # dropped. Without it, the assertion below could pass on a pass that never writes.
    shutil.rmtree(tree.path)
    plant()
    open_sweep = adopt.sweep(state)
    assert (open_sweep.absent, open_sweep.removed) == (1, 0), open_sweep.lines
    assert not load(roster), open_sweep.lines

    planted = plant()
    with hold(git_repo, state) as acquired:
        swept = adopt.sweep(state)
        # Not read, so nothing about it is claimed: no `absent` here, and `records` stays
        # at zero. The count that says a roster was walked past is `locked`.
        assert (swept.locked, swept.removed, swept.absent) == (1, 0, 0), swept.lines
        assert str(acquired.holder.pid) in " ".join(swept.lines), swept.lines

    assert roster.read_text(encoding="utf-8") == planted, (
        "gc rewrote the roster of a repository a supervisor is running on")


def test_a_record_reaching_another_repository_asks_that_one_too(
    git_repo: Path, other_repo: Path, tmp_path: Path, state: Path
):
    """The roster's own lock is not the only one a record can need.

    A record is a path read out of a JSON file, and one that resolves into a DIFFERENT
    checkout belongs to a different supervisor with its own lock and its own wave running
    under it. Holding only the roster's would reap that repository's trees on the strength
    of somebody else's file — which is the same act, on a smaller roster, that the bounce
    was about.

    Sabotage: drop the per-repository `sweep_hold` in `_sweep_records`; the other
    repository's tree goes and this fails.
    """
    mine = tmp_path / "ws"
    mine.mkdir()
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    own_tree = create(git_repo, mine / "wave-1", "wave", "1")
    their_tree = create(other_repo, theirs / "wave-2", "wave", "2")
    roster = adopt.children_path(git_repo, state)
    save(roster, [
        _snap(own_tree, _dead_pid(), slot="1"),
        _snap(their_tree, _dead_pid(), slot="2"),
    ])

    with hold(other_repo, state) as acquired:
        swept = adopt.sweep(state)
        assert (swept.removed, swept.locked) == (1, 1), swept.lines
        assert str(acquired.holder.pid) in " ".join(swept.lines), swept.lines

    assert not own_tree.path.exists(), swept.lines
    assert their_tree.path.exists(), (
        f"gc reaped under another repository's live lock: {swept.lines}")
    assert [s.branch for s in load(roster)] == [their_tree.branch], (
        "a tree gc would not touch must stay in the record")


def test_gc_leaves_a_recorded_child_that_is_still_running(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """An unheld lock is NOT evidence that nothing is running: children outlive their
    supervisor (`JOB_LIMIT_FLAGS = 0`), so a dead supervisor with a live child is the
    ordinary crash rather than an edge case.

    Sabotage: drop the `pid_is_alive` branch in `_reap_one`; this tree and its log
    directory both go.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    logs = tmp_path / "logs" / "wave-1"
    logs.mkdir(parents=True)
    (logs / "stdout.log").write_text("working\n", encoding="utf-8")
    sleeper = subprocess.Popen(
        [str(scripts["python"]), str(scripts["sleeper"])], **spawn_kwargs())
    try:
        tree = create(git_repo, workspace / "wave-1", "wave", "1")
        roster = adopt.children_path(git_repo, state)
        save(roster, [_snap(tree, sleeper.pid, log_dir=str(logs))])

        swept = adopt.sweep(state)

        assert (swept.live, swept.removed, swept.logs) == (1, 0, 0), swept.lines
        assert tree.path.exists(), swept.lines
        assert (logs / "stdout.log").exists(), "a live child's logs went with the tree"
        assert [s.pid for s in load(roster)] == [sleeper.pid]
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)


def test_a_tree_reap_refuses_to_remove_stays_in_the_record(
    git_repo: Path, tmp_path: Path, state: Path
):
    """`persist` writes running children only, so a reap that returns `removed=False`
    drops the path out of the roster and the directory is then named NOWHERE — the hole
    this item exists to close. Dirty is printed and left, and left IN THE RECORD.

    Sabotage: append to `removed` instead of `kept` on the `not reaped.removed` branch;
    the roster loses the path and the second assertion fails.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    _lock_worktree(git_repo, tree)
    roster = adopt.children_path(git_repo, state)
    save(roster, [_snap(tree, _dead_pid())])
    try:
        swept = adopt.sweep(state)

        assert tree.path.exists(), f"gc forced a removal: {swept.lines}"
        assert (swept.left_dirty, swept.removed) == (1, 0), swept.lines
        assert swept.needs_a_human, swept.lines
        assert [s.branch for s in load(roster)] == [tree.branch], (
            "the next gc can no longer see this tree")

        # ...and the next gc DOES still see it, rather than the first pass having been
        # the only one that ever could.
        again = adopt.sweep(state)
        assert (again.left_dirty, again.removed) == (1, 0), again.lines
        assert tree.path.exists(), again.lines
    finally:
        _unlock_worktree(git_repo, tree)


def test_the_log_directory_goes_with_its_worktree(
    git_repo: Path, tmp_path: Path, state: Path
):
    """1,584 log directories measured on one abandoned clone. A slot whose worktree went
    takes its logs with it — but not the `logs/` they all live under."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    logs = tmp_path / "logs"
    (logs / "wave-1").mkdir(parents=True)
    (logs / "wave-1" / "stdout.log").write_text("x\n", encoding="utf-8")
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    roster = adopt.children_path(git_repo, state)
    save(roster, [_snap(tree, _dead_pid(), log_dir=str(logs / "wave-1"))])

    swept = adopt.sweep(state)

    assert (swept.removed, swept.logs) == (1, 1), swept.lines
    assert not (logs / "wave-1").exists(), swept.lines
    assert logs.is_dir(), "gc removed the parent every slot's logs live under"


def test_a_log_directory_a_kept_record_names_is_not_removed(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """One directory named by a removed record AND a live one. Taking it would delete a
    running child's logs to reclaim a dead one's.

    Sabotage: drop the `protected` check in `_drop_logs`; the live child's log goes.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "stdout.log").write_text("still working\n", encoding="utf-8")
    sleeper = subprocess.Popen(
        [str(scripts["python"]), str(scripts["sleeper"])], **spawn_kwargs())
    try:
        dead_tree = create(git_repo, workspace / "wave-1", "wave", "1")
        live_tree = create(git_repo, workspace / "wave-2", "wave", "2")
        roster = adopt.children_path(git_repo, state)
        save(roster, [
            _snap(dead_tree, _dead_pid(), slot="1", log_dir=str(logs)),
            _snap(live_tree, sleeper.pid, slot="2", log_dir=str(logs)),
        ])

        swept = adopt.sweep(state)

        assert (swept.removed, swept.live, swept.logs) == (1, 1, 0), swept.lines
        assert not dead_tree.path.exists(), swept.lines
        assert live_tree.path.exists(), swept.lines
        assert (logs / "stdout.log").exists(), swept.lines
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)


def test_gc_leaves_a_roster_it_cannot_read(tmp_path: Path, state: Path):
    """An unreadable roster is a FULL roster we cannot read (P30 D7). Rewriting it would
    turn "we do not know what is out there" into "nothing is", and the trees it names
    would end up named nowhere at all.

    Sabotage: `save(roster, [])` on the UnadoptableFile branch; this fails.
    """
    roster = state / "somewhere-abc123def456.children.json"
    roster.write_text("{not json", encoding="utf-8")

    swept = adopt.sweep(state)

    assert (swept.unreadable, swept.repos, swept.removed) == (1, 1, 0), swept.lines
    assert roster.read_text(encoding="utf-8") == "{not json"
    assert swept.needs_a_human


def test_a_record_whose_directory_is_already_gone_is_absent_not_removed(
    tmp_path: Path, state: Path
):
    """The third answer. Reporting a stale record as a reap would say gc reclaimed a
    directory that reclaimed itself, and hide that the roster had gone unread."""
    roster = state / "elsewhere-abc123def456.children.json"
    save(roster, [Snapshot(
        pid=_dead_pid(), worktree=str(tmp_path / "ws" / "wave-9"),
        branch="gb/wave-9", adapter="fake", slot="9",
    )])

    swept = adopt.sweep(state)

    assert (swept.absent, swept.removed) == (1, 0), swept.lines
    assert not swept.needs_a_human, swept.lines
    assert not load(roster), "the record still names a directory that is not there"


def test_an_empty_state_directory_reports_zero_rosters_not_zero_trees(state: Path):
    """Zero of everything is also what a pass that never looked produces, so the count
    that says which one happened is the number of rosters the directory named."""
    swept = adopt.sweep(state)

    assert (swept.repos, swept.records, swept.removed) == (0, 0, 0)
    assert not swept.needs_a_human


def test_gc_leaves_a_path_git_does_not_list_as_a_worktree(
    git_repo: Path, state: Path
):
    """A record is a path in a JSON file in the temp directory, and `reap` salvages
    INSIDE that path before it ever asks git to remove it — so "not one of ours" is not a
    no-op, it is a commit in somebody else's checkout. Two cases; the first is the rule
    the item states outright, that the clone itself is never deleted.

    Sabotage: drop the `path == main or path not in ours` check in `_sweep_roster`; the
    clone's own record reaches `reap` and this fails.
    """
    (git_repo / "backend").mkdir()
    # Uncommitted work in the clone, which is what makes the guard provable rather than
    # merely stated: `reap` salvages BEFORE it asks git to remove anything, and git's own
    # refusal of `worktree remove` on a main tree comes too late — the commit is already
    # on the branch. Sabotage the guard and HEAD moves.
    (git_repo / "uncommitted.py").write_text("x = 1\n", encoding="utf-8")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True,
        check=True).stdout.strip()
    roster = adopt.children_path(git_repo, state)
    save(roster, [
        Snapshot(pid=_dead_pid(), worktree=str(git_repo), branch="gb/wave-1",
                 adapter="fake", slot="1"),
        Snapshot(pid=_dead_pid(), worktree=str(git_repo / "backend"), branch="gb/wave-2",
                 adapter="fake", slot="2"),
    ])

    swept = adopt.sweep(state)

    # HEAD first: it is the claim the guard exists for, and the counts below are wrong
    # under the same sabotage, so an assertion that came second would never be reached
    # and could not fail.
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True,
        check=True).stdout.strip()
    assert head_after == head_before, (
        f"gc salvaged into the clone it was told to leave alone: {head_before[:12]} -> "
        f"{head_after[:12]}")
    assert (swept.removed, swept.unreadable) == (0, 2), swept.lines
    assert swept.needs_a_human, swept.lines
    assert (git_repo / ".git").exists() and (git_repo / "README.md").exists()
    assert (git_repo / "backend").is_dir()
    assert len(load(roster)) == 2, "a path gc will not touch must stay in the record"
    joined = " ".join(swept.lines)
    assert "the clone itself" in joined and "does not list it" in joined, swept.lines


def test_up_sweeps_the_repositories_it_does_not_own(
    git_repo: Path, other_repo: Path, tmp_path: Path, scripts, state: Path
):
    """THE CALL, for `up`. The sweep walks the state directory rather than `--repo`,
    because the trees that need it belong to the clone this supervisor was never started
    in — and it runs inside the lock `up` holds, which is what makes `up`'s own
    repository answer "held" and be skipped by the rule protecting every other one.

    Sabotage: delete the `adopt_mod.sweep(state)` loop from `supervisor.up`; the other
    clone's tree survives and this fails.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    tree = create(other_repo, theirs / "wave-1", "wave", "1")
    save(adopt.children_path(other_repo, state), [_snap(tree, _dead_pid())])

    up(git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
       limits=Limits(max_workers=1), state=state, workspace=workspace, poll=0.05)

    assert not tree.path.exists(), (
        f"up left {other_repo.name}'s abandoned tree standing — the sweep never reached "
        "a repository other than its own --repo")


def test_until_sweeps_the_repositories_it_does_not_own(
    git_repo: Path, other_repo: Path, tmp_path: Path, scripts, state: Path
):
    """THE CALL, for `until`. Note there is no takeover here: the sweep is NOT inside the
    `acquired.takeover` branch, because the clone whose trees need it is by definition
    the one with no supervisor left to adopt anything.

    Sabotage: delete the `adopt_mod.sweep(state)` loop from `until.run`; this fails.
    """
    from gbfleet.until import run
    from tests.test_until import KEY, _clients

    workspace = tmp_path / "ws"
    workspace.mkdir()
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    tree = create(other_repo, theirs / "wave-1", "wave", "1")
    save(adopt.children_path(other_repo, state), [_snap(tree, _dead_pid())])

    def sleep_fn(_dt: float) -> None:
        raise _StopLoop()

    planner, supervisor = _clients(workspace)
    try:
        run(
            git_repo, _factory(scripts, "works_then_exits"),
            planner, supervisor, api_key=KEY, server="http://gb.invalid",
            adapter="fake", state=state, workspace=workspace, poll=0,
            sleep=sleep_fn, empty_ticks=3, limits=Limits(max_workers=1),
        )
    except _StopLoop:
        pass

    assert not tree.path.exists(), "until left the other clone's abandoned tree standing"


def test_mcp_sweeps_the_repositories_it_does_not_own(
    git_repo: Path, other_repo: Path, tmp_path: Path, monkeypatch
):
    """THE CALL, for `mcp`. `mcp` takes no `--state`, so this resolves the state
    directory the way the command does; the conftest fixture is what keeps that off the
    developer's real one.

    Sabotage: delete the `adopt_mod.sweep()` loop from `cli._serve_stdio`; this fails.
    """
    from gbfleet import cli
    from tests.test_supervisor import KEY

    workspace = tmp_path / "ws"
    workspace.mkdir()
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    tree = create(other_repo, theirs / "wave-1", "wave", "1")
    save(adopt.children_path(other_repo), [_snap(tree, _dead_pid())])

    monkeypatch.setattr(cli, "serve", lambda fleet: None)
    monkeypatch.setenv("GBFLEET_API_KEY", KEY)
    monkeypatch.chdir(git_repo)
    code = cli.main([
        "mcp", "--repo", str(git_repo), "--server", "http://gb.invalid",
        "--workspace", str(workspace),
    ])

    assert code == 0
    assert not tree.path.exists(), "mcp left the other clone's abandoned tree standing"


def test_the_gc_command_reports_what_it_reaped(git_repo: Path, tmp_path: Path, capsys):
    from gbfleet import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    save(adopt.children_path(git_repo), [_snap(tree, _dead_pid())])

    assert cli.main(["gc"]) == 0

    out = capsys.readouterr().out
    assert tree.branch in out and str(tree.path) in out, out
    assert "1 removed" in out, out


def test_the_gc_command_exits_nonzero_when_something_needs_a_person(
    git_repo: Path, tmp_path: Path, capsys
):
    """A scheduled `gc` that always exits 0 is a tidy-up nobody is ever told failed.

    Sabotage: `return 0` unconditionally in `_gc`; this fails.
    """
    from gbfleet import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    tree = create(git_repo, workspace / "wave-1", "wave", "1")
    _lock_worktree(git_repo, tree)
    save(adopt.children_path(git_repo), [_snap(tree, _dead_pid())])
    try:
        assert cli.main(["gc"]) == 1

        captured = capsys.readouterr()
        assert "could not remove" in captured.err, captured
        assert tree.path.exists(), captured.out
        assert tree.branch in captured.out, captured.out
    finally:
        _unlock_worktree(git_repo, tree)
