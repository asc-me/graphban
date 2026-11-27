"""`gbfleet drain remove` proves a push against the tip the server lists NOW, even when that
tip moved past the checkout's (GRPH-2001).

GRPH-1987 counted a local remote-tracking ref only when the server listed that exact object
id. Anyone else pushing to the server's main moved it, and a clean, fully pushed drain
checkout was then kept: the normal state of a drain. Now a branch the server lists at a tip
this checkout lacks has that tip fetched as OBJECTS only (no ref, no reflog, no FETCH_HEAD, no
packed-refs change, here or on the server), and a commit is pushed exactly when that tip
contains it. A tip that cannot be read keeps the checkout.

Every server is a temp bare repo; every case runs on the launchd and systemd fakes.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from gbfleet import drain
from test_drain_remove_rekey import (  # noqa: F401  (fixtures)
    _cli,
    _git,
    _install,
    _key,
    _remove_keeps,
    _unpushed,
    kind,
    posix_only,
    world,
)

pytestmark = posix_only


def _other_pushes(world, *, force: bool = False) -> str:
    """Someone else pushes to the server's main (on top of it, or force-pushed over it)."""
    other = world["tmp"] / f"other-{time.monotonic_ns()}"
    subprocess.run(["git", "clone", "-q", str(world["origin"]), str(other)], check=True)
    _git(other, "config", "user.email", "o@example.invalid")
    _git(other, "config", "user.name", "O")
    if force:
        _git(other, "reset", "-q", "--hard", "HEAD~1")
    (other / f"other-{time.monotonic_ns()}.txt").write_text("theirs\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "someone else's work")
    _git(other, "push", "-q", *(["--force"] if force else []), "origin", "main")
    return _git(other, "rev-parse", "HEAD")


def _push_mine(repo: Path) -> str:
    _unpushed(repo)
    _git(repo, "push", "-q", "origin", "main")
    return _git(repo, "rev-parse", "HEAD")


def _snapshot(git_dir: Path) -> dict[str, object]:
    """Every ref, reflog, packed-refs and FETCH_HEAD: what an object fetch must not touch."""
    logs = git_dir / "logs"
    return {
        "refs": subprocess.run(["git", "--git-dir", str(git_dir), "for-each-ref",
                                "--format=%(objectname) %(refname)"], check=True,
                               capture_output=True, text=True).stdout,
        "packed": (git_dir / "packed-refs").read_bytes()
        if (git_dir / "packed-refs").exists() else None,
        "fetch_head": (git_dir / "FETCH_HEAD").exists(),
        "logs": sorted((str(f.relative_to(logs)), f.read_bytes())
                       for f in logs.rglob("*") if f.is_file()) if logs.exists() else [],
    }


def test_a_pushed_checkout_whose_server_main_moved_ahead_is_deleted(world, capsys):
    key = _key(world)
    repo = _install(world, "m1", key, marker="m1")
    _push_mine(repo)
    tip = _other_pushes(world)
    assert subprocess.run(["git", "cat-file", "-e", tip], cwd=repo, check=False).returncode != 0
    code, out, err = _cli(capsys, "remove", "m1")
    assert code == 0, err
    assert not repo.exists(), out
    assert f"checkout: deleted {repo}" in out


def test_moved_ahead_main_that_still_holds_the_commit_proves_it_and_writes_only_objects(
        world, capsys):
    """The verdict is "" (deletable), and the only change is the fetched objects: no ref,
    reflog, packed-refs or FETCH_HEAD, here or on the server."""
    key = _key(world)
    repo = _install(world, "m2", key, marker="m2")
    mine = _push_mine(repo)
    _git(repo, "pack-refs", "--all")
    tip = _other_pushes(world)
    before, server_before = _snapshot(repo / ".git"), _snapshot(world["origin"])
    assert drain.checkout_verdict(repo, "m2") == ""
    assert _snapshot(repo / ".git") == before
    assert _snapshot(world["origin"]) == server_before
    # The tip arrived as an object, and it does contain the local commit.
    assert _git(repo, "merge-base", "--is-ancestor", mine, tip) == ""
    assert _git(repo, "rev-parse", "refs/remotes/origin/main") == mine


def test_a_force_pushed_main_that_dropped_the_commit_keeps_the_checkout(world, capsys):
    """The server lists main, at a tip that no longer contains the local commit: one
    unpushed commit, and the server is NOT said to no longer list main."""
    key = _key(world)
    repo = _install(world, "m3", key, marker="m3")
    mine = _push_mine(repo)
    _other_pushes(world, force=True)
    out = _remove_keeps(world, capsys, "m3", repo, "it has 1 unpushed commit(s)")
    assert "no longer lists" not in out
    assert "could not be read" not in out
    assert _git(repo, "cat-file", "-t", mine) == "commit"


def test_only_commits_the_moved_tip_lacks_are_counted_unpushed(world, capsys):
    """Pushed, then one more local commit: the server holds the first (under a moved main),
    so exactly one commit is unpushed."""
    key = _key(world)
    repo = _install(world, "m4", key, marker="m4")
    _push_mine(repo)
    _other_pushes(world)
    (repo / "more.txt").write_text("more\n", encoding="utf-8")
    _git(repo, "add", "more.txt")
    _git(repo, "commit", "-qm", "second, never pushed")
    out = _remove_keeps(world, capsys, "m4", repo, "it has 1 unpushed commit(s)")
    assert "no longer lists" not in out


# ---- a moved tip that cannot be read ------------------------------------------------------

def _ssh_server(world, monkeypatch, second: str) -> None:
    """origin over a fake ssh that serves the real bare repo for the first call (ls-remote)
    and runs `second` (shell) for the next (the object fetch)."""
    count = world["tmp"] / "ssh-calls"
    ssh = world["tmp"] / "bin" / "fake-ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{count}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{count}"\n'
        'for last; do :; done\n'
        f'if [ "$n" -gt 1 ]; then {second}; fi\n'
        'eval "$last"\n', encoding="utf-8")
    ssh.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(ssh))
    monkeypatch.setenv("GIT_SSH_VARIANT", "simple")


def _over_ssh(world, repo: Path) -> None:
    _git(repo, "remote", "set-url", "origin", f"ssh://git@server.invalid{world['origin']}")


def test_a_moved_tip_whose_fetch_fails_keeps_the_checkout(world, capsys, monkeypatch):
    key = _key(world)
    repo = _install(world, "m5", key, marker="m5")
    _push_mine(repo)
    tip = _other_pushes(world)
    _over_ssh(world, repo)
    _ssh_server(world, monkeypatch, "echo 'fatal: refused' >&2; exit 1")
    out = _remove_keeps(world, capsys, "m5", repo, "could not be reached")
    assert "server tip could not be read" in out
    assert f" at {tip[:12]}: git fetch failed: fatal: refused" in out
    assert "unpushed" not in out and "no longer lists" not in out


def _gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_a_moved_tip_whose_fetch_hangs_times_out_killing_its_whole_process_group(
        world, capsys, monkeypatch):
    """The fetch's transport hangs and leaves a child of its own: at FETCH_TIP_TIMEOUT the
    whole group goes (killing only git would leave both), and the checkout is kept."""
    key = _key(world)
    repo = _install(world, "m6", key, marker="m6")
    _push_mine(repo)
    _other_pushes(world)
    _over_ssh(world, repo)
    child = world["tmp"] / "child-pid"
    _ssh_server(world, monkeypatch,
                f'sleep 60 & echo $! > "{child}"; exec sleep 60')
    monkeypatch.setattr(drain, "FETCH_TIP_TIMEOUT", 1.0)
    out = _remove_keeps(world, capsys, "m6", repo, "could not be reached")
    assert "server tip could not be read" in out
    assert "git fetch gave no answer within 1s" in out
    pid = int(child.read_text(encoding="utf-8"))
    alive = not _gone(pid)
    if alive:
        os.kill(pid, 9)
    assert not alive, "the transport's child outlived the timeout"


def test_a_hung_ls_remote_kills_its_whole_process_group(world, capsys, monkeypatch):
    """GRPH-1987's ls-remote timeout too: a child the transport left behind is killed."""
    key = _key(world)
    repo = _install(world, "m7", key, marker="m7")
    child = world["tmp"] / "child-pid"
    ssh = world["tmp"] / "bin" / "hang-ssh"
    ssh.write_text(f'#!/bin/sh\nsleep 60 & echo $! > "{child}"\nexec sleep 60\n',
                   encoding="utf-8")
    ssh.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(ssh))
    monkeypatch.setattr(drain, "LS_REMOTE_TIMEOUT", 1.0)
    _git(repo, "remote", "set-url", "origin", "ssh://git@server.invalid/x.git")
    _remove_keeps(world, capsys, "m7", repo, "could not be reached")
    pid = int(child.read_text(encoding="utf-8"))
    alive = not _gone(pid)
    if alive:
        os.kill(pid, 9)
    assert not alive, "the transport's child outlived the timeout"


@pytest.mark.parametrize("spec,ref,want", [
    ("+refs/heads/*:refs/remotes/origin/*", "refs/remotes/origin/main", "refs/heads/main"),
    ("+refs/heads/main:refs/remotes/origin/main", "refs/remotes/origin/main", "refs/heads/main"),
    ("+refs/heads/gb/*:refs/remotes/origin/gb/*", "refs/remotes/origin/gb/x", "refs/heads/gb/x"),
])
def test_server_name_reverses_the_fetch_refspec(world, spec, ref, want):
    key = _key(world)
    repo = _install(world, "m8", key, marker="m8")
    _git(repo, "config", "--replace-all", "remote.origin.fetch", spec)
    assert drain._server_name(repo, "origin", ref) == want
