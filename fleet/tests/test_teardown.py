"""GRPH-1296 — `gbfleet teardown`: remove a finished drain, refuse while anything is unpushed.

Every refusal case below asserts two things: the cause is NAMED in the output, and NOTHING was
deleted — the service unit, the key file, the clone, the workspace and the stray worktree are
all still on disk. A refusal that deleted half and then explained itself would be the hand
cleanup's failure with a nicer message.

The drain is real git: a bare remote, a clone of it, a child worktree under
`<clone>-gbfleet/`, and a reviewer's worktree OUTSIDE it — the `/private/tmp/rv-1287` shape
that the GRPH-P63 teardown found by hand. The service manager and the server are the only
fakes.
"""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet import service  # noqa: E402
from gbfleet import teardown  # noqa: E402

KEY = "gbk_live_teardown_key"
BINARY = "/opt/fake/bin/gbfleet"
NAME = "p63"
PRD = "GRPH-P63"
WAVE = "p63"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(tree: Path, name: str, text: str = "x\n") -> None:
    (tree / name).write_text(text, encoding="utf-8")
    _git(tree, "add", name)
    _git(tree, "commit", "-qm", f"add {name}")


class FakeLedger:
    """search_items by status, a roster, and a record of every retire_wave."""

    def __init__(self, clone: Path) -> None:
        self.items: dict[str, list[dict]] = {}
        self.retired: list[dict] = []
        self.roster = {"agents": [
            {"id": "A1", "active_role": "planner", "worktree": str(clone)},
            {"id": "A2", "active_role": "planner", "worktree": str(clone)},
            {"id": "A3", "active_role": "worker", "worktree": str(clone)},
            {"id": "A9", "active_role": "planner", "worktree": "/somewhere/else"},
        ]}

    def call(self, tool: str, **kw):
        if tool == "search_items":
            return {"results": self.items.get(kw["status"], [])}
        if tool == "fleet_status":
            return self.roster
        if tool == "retire_wave":
            self.retired.append(kw)
            return {"seats_revoked": 2, "agents_still_running": []}
        raise AssertionError(f"unexpected tool {tool}")


@pytest.fixture
def drain(tmp_path: Path, monkeypatch):
    """A finished drain: everything pushed, nothing open, the service idle."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "T")
    _commit(seed, "README.md")
    _commit(seed, "uv.lock", "lock v1\n")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "main")

    clone = (tmp_path / "drain").resolve()
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "T")
    workspace = clone.parent / f"{clone.name}-gbfleet"
    (workspace / "logs").mkdir(parents=True)
    (workspace / "logs" / "child.log").write_text("log\n", encoding="utf-8")

    child = workspace / "wt-a"
    _git(clone, "worktree", "add", "-q", "-b", "gb/p63-a", str(child))
    _commit(child, "a.txt")
    _git(child, "push", "-q", "origin", "gb/p63-a")

    stray = (tmp_path / "rv-1287").resolve()
    _git(clone, "worktree", "add", "-q", "-b", "gb/p63-review-1", str(stray))
    _commit(stray, "r.txt")
    _git(stray, "push", "-q", "origin", "gb/p63-review-1")

    plan = service.make_plan(
        ["--repo", str(clone), "--server", "http://gb.invalid", "--project", "p",
         "--adapter", "claude", "--prd", PRD, "--wave", WAVE],
        name=NAME, kind="systemd", binary=BINARY)
    plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    plan.unit_path.write_bytes(service.render(plan))
    service.write_env(plan, KEY)

    calls: list[list[str]] = []
    monkeypatch.setattr(service, "host", lambda: service.Host("systemd"))
    monkeypatch.setattr(service, "linger", lambda: None)
    monkeypatch.setattr(service, "_running_systemd", lambda name: (False, "inactive", 0))
    monkeypatch.setattr(service, "_run",
                        lambda cmd, timeout=20.0: (calls.append(cmd), (0, ""))[1])

    class D:
        pass

    d = D()
    d.clone, d.workspace, d.child, d.stray = clone, workspace, child, stray
    d.unit, d.env, d.calls = plan.unit_path, plan.env_path, calls
    d.ledger = FakeLedger(clone)
    return d


def _run(d, **kw) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    kw.setdefault("ledger", d.ledger)
    kw.setdefault("pr_lister", lambda clone: [])
    kw.setdefault("process_finder", lambda clone: [])
    code = teardown.run(NAME, out=out, err=err, **kw)
    return code, out.getvalue() + err.getvalue()


def _assert_untouched(d) -> None:
    for path in (d.unit, d.env, d.clone, d.workspace, d.child, d.stray,
                 d.workspace / "logs" / "child.log"):
        assert path.exists(), f"{path} was deleted by a teardown that should have refused"
    assert not [c for c in d.calls if "disable" in c], "the service was stopped"
    assert d.ledger.retired == [], "seats were retired"


# --- the happy path ---------------------------------------------------------------------


def test_a_finished_drain_is_removed_entirely(drain):
    code, text = _run(drain)
    assert code == 0, text
    assert not drain.unit.exists(), "the unit is still installed"
    assert not drain.env.exists(), "the key file is still on disk"
    assert not drain.clone.exists(), "the clone is still on disk"
    assert not drain.workspace.exists(), "<clone>-gbfleet/ is still on disk"
    assert not drain.child.exists(), "the child worktree is still on disk"
    assert not drain.stray.exists(), "the worktree outside -gbfleet/ is still on disk"
    assert any("disable" in c for c in drain.calls), "the service was never stopped"
    # Every planner this drain registered from this clone, and none from anywhere else.
    assert sorted(r["agent_id"] for r in drain.ledger.retired) == ["A1", "A2"]
    assert {r["wave"] for r in drain.ledger.retired} == {WAVE}
    assert "4 seat(s) of wave 'p63' revoked" in text


def test_keep_clone_keeps_only_the_clone(drain):
    code, text = _run(drain, keep_clone=True)
    assert code == 0, text
    assert drain.clone.exists()
    assert not drain.workspace.exists() and not drain.stray.exists()


def test_dry_run_changes_nothing(drain):
    unit_before = drain.unit.read_bytes()
    listing = sorted(p.relative_to(drain.clone.parent) for p in drain.clone.parent.rglob("*")
                     if ".git" not in p.parts)
    code, text = _run(drain, dry_run=True)
    assert code == 0, text
    assert "would delete" in text and str(drain.clone) in text
    assert f"would reap worktree {drain.stray}" in text
    _assert_untouched(drain)
    assert drain.unit.read_bytes() == unit_before
    assert sorted(p.relative_to(drain.clone.parent) for p in drain.clone.parent.rglob("*")
                  if ".git" not in p.parts) == listing


# --- refusals: each names its cause and deletes nothing -----------------------------------


def test_an_unpushed_commit_on_a_local_branch_refuses(drain):
    _git(drain.clone, "branch", "gb/p63-b")
    _git(drain.clone, "checkout", "-q", "gb/p63-b")
    _commit(drain.clone, "b.txt")
    _git(drain.clone, "checkout", "-q", "main")
    code, text = _run(drain)
    assert code == 2
    assert "branch gb/p63-b: 1 commit(s) on no remote" in text
    _assert_untouched(drain)


def test_a_dirty_worktree_outside_the_workspace_refuses(drain):
    (drain.stray / "half-done.py").write_text("print('wip')\n", encoding="utf-8")
    code, text = _run(drain)
    assert code == 2
    assert f"worktree {drain.stray} has uncommitted work: half-done.py" in text
    _assert_untouched(drain)


@pytest.mark.parametrize("status", ["review", "next"])
def test_an_unfinished_prd_refuses(drain, status):
    drain.ledger.items[status] = [{"id": "GRPH-9", "prd_id": PRD},
                                  {"id": "GRPH-10", "prd_id": "GRPH-P1"}]
    code, text = _run(drain)
    assert code == 2
    assert f"{PRD} still has {status} item(s): GRPH-9" in text
    assert "GRPH-10" not in text, "another PRD's item is not this drain's business"
    _assert_untouched(drain)


def test_an_unreadable_ledger_refuses(drain):
    from gbfleet.client import ServerUnreachable

    class Down:
        def call(self, tool, **kw):
            raise ServerUnreachable("connection refused")

    code, text = _run(drain, ledger=Down())
    assert code == 2
    assert "could not read the ledger" in text
    _assert_untouched(drain)


def test_a_process_with_the_clone_open_refuses(drain):
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              cwd=str(drain.clone))
    try:
        time.sleep(0.5)
        code, text = _run(drain, process_finder=teardown.processes_under)
    finally:
        holder.kill()
        holder.wait()
    assert code == 2
    assert "is in use by" in text
    if os.name == "nt":
        # Windows reports the clone as held, not by whom: there is no /proc and no lsof.
        assert "a process Windows does not name" in text
    else:
        assert f"pid {holder.pid}" in text
    _assert_untouched(drain)


def test_the_windows_probe_leaves_an_idle_directory_where_it_was(tmp_path):
    idle = tmp_path / "clone"
    (idle / "sub").mkdir(parents=True)
    (idle / "sub" / "f.txt").write_text("x", encoding="utf-8")
    assert teardown._windows_in_use(idle) == []
    assert (idle / "sub" / "f.txt").read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "clone.gbfleet-teardown-probe").exists()


def test_the_windows_probe_cannot_check_when_its_probe_name_is_taken(tmp_path):
    idle = tmp_path / "clone"
    idle.mkdir()
    (tmp_path / "clone.gbfleet-teardown-probe").mkdir()
    assert teardown._windows_in_use(idle) is None
    assert idle.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="a held cwd blocks a rename only on Windows")
def test_the_windows_probe_names_a_held_directory_in_use(tmp_path):
    held = tmp_path / "clone"
    held.mkdir()
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              cwd=str(held))
    try:
        time.sleep(0.5)
        found = teardown._windows_in_use(held)
    finally:
        holder.kill()
        holder.wait()
    assert found and "a process Windows does not name" in found[0]
    assert held.is_dir()


def test_read_only_files_are_deleted(tmp_path):
    """git writes its object files read-only; on Windows a plain rmtree cannot delete them."""
    tree = tmp_path / "clone"
    objects = tree / ".git" / "objects" / "08"
    objects.mkdir(parents=True)
    obj = objects / "ec7e"
    obj.write_bytes(b"blob")
    os.chmod(obj, stat.S_IREAD)
    teardown.remove_tree(tree)
    assert not tree.exists()


def test_the_read_only_handler_clears_the_bit_and_retries(tmp_path):
    target = tmp_path / "ro"
    target.write_bytes(b"x")
    os.chmod(target, stat.S_IREAD)
    calls = []

    def unlink(path):
        calls.append(os.stat(path).st_mode & stat.S_IWRITE)
        os.unlink(path)

    teardown._clear_readonly_and_retry(unlink, str(target), PermissionError())
    assert calls and calls[0] and not target.exists()


def test_a_process_table_nobody_can_read_refuses(drain):
    code, text = _run(drain, process_finder=lambda clone: None)
    assert code == 2
    assert "cannot tell whether a process has" in text
    _assert_untouched(drain)


def test_an_open_pr_refuses(drain):
    code, text = _run(drain, pr_lister=lambda clone: [
        {"headRefName": "gb/p63-a", "url": "https://example.invalid/pr/7"}])
    assert code == 2
    assert "PR still open from gb/p63-a" in text
    _assert_untouched(drain)


def test_a_running_service_refuses(drain, monkeypatch):
    monkeypatch.setattr(service, "_running_systemd", lambda name: (True, "active", None))
    code, text = _run(drain)
    assert code == 2
    assert "is mid-run" in text
    _assert_untouched(drain)


def test_no_installed_drain_refuses_before_looking_at_anything(drain):
    out, err = io.StringIO(), io.StringIO()
    assert teardown.run("nonesuch", out=out, err=err, ledger=drain.ledger) == 2
    assert "no installed drain named 'nonesuch'" in err.getvalue()
    _assert_untouched(drain)


# --- the drain-state allowlist -----------------------------------------------------------------


def test_allowlisted_drain_state_does_not_block(drain):
    (drain.clone / "uv.lock").write_text("lock v2\n", encoding="utf-8")
    (drain.clone / ".gbfleet").mkdir()
    (drain.clone / ".gbfleet" / "servers").write_text("context7\n", encoding="utf-8")
    code, text = _run(drain, dry_run=True)
    assert code == 0, text


def test_any_other_modified_file_blocks(drain):
    (drain.clone / "uv.lock").write_text("lock v2\n", encoding="utf-8")
    (drain.clone / "README.md").write_text("edited\n", encoding="utf-8")
    code, text = _run(drain)
    assert code == 2
    assert "has modified or untracked files: README.md" in text
    assert "uv.lock" not in text.split("has modified or untracked files:")[1].splitlines()[0]
    _assert_untouched(drain)


# --- reading the unit back ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["systemd", "launchd", "schtasks"])
def test_until_args_survive_the_round_trip_through_every_unit(git_repo: Path, kind):
    args = ["--repo", str(git_repo.resolve()), "--server", "http://gb.invalid",
            "--project", "p", "--adapter", "claude", "--prd", PRD, "--wave", WAVE]
    plan = service.make_plan(args, name="rt", kind=kind, binary=BINARY)
    plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    plan.unit_path.write_bytes(service.render(plan))
    if kind == "schtasks":
        plan.env_path.parent.mkdir(parents=True, exist_ok=True)
        service.runner_path_for("rt").write_text(
            service._render_schtasks_runner(plan), encoding="utf-8")
    back = service.until_args_of("rt", kind)
    assert back is not None
    for flag, value in (("--repo", str(git_repo.resolve())), ("--prd", PRD),
                        ("--wave", WAVE), ("--server", "http://gb.invalid")):
        assert service.value_of(back, flag) == value
