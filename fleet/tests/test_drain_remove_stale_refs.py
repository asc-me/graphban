"""`gbfleet drain remove` proves a push against what the server lists NOW (GRPH-1987).

A remote-tracking ref outlives the branch it tracks: delete a branch on the server, never
prune, and `refs/remotes/origin/<branch>` still points at the commit. Trusting it would let
`remove` delete the only copy. So before deleting, `remove` asks each remote it would count as
proof (`git ls-remote`, read only) and a local ref proves its tip only when the server still
lists that object id. A server that cannot be asked keeps the checkout.

Every "server" here is a temp bare repo (or a URL that cannot answer); every case runs on the
launchd and systemd fakes of `test_drain_remove_rekey`'s `world`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gbfleet import drain

from test_drain_remove_rekey import (  # noqa: F401  (fixtures)
    _cli, _git, _install, _key, _remove_keeps, kind, posix_only, world)

pytestmark = posix_only


def _push_branch(repo: Path, branch: str = "feature") -> str:
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / f"{branch}.txt").write_text("work\n", encoding="utf-8")
    _git(repo, "add", f"{branch}.txt")
    _git(repo, "commit", "-qm", f"{branch} work")
    _git(repo, "push", "-q", "-u", "origin", branch)
    _git(repo, "checkout", "-q", "main")
    return _git(repo, "rev-parse", branch)


def _delete_on_server(world, branch: str = "feature") -> None:
    subprocess.run(["git", "--git-dir", str(world["origin"]), "branch", "-q", "-D", branch],
                   check=True)


def _refs(repo: Path) -> str:
    return _git(repo, "for-each-ref", "--format=%(objectname) %(refname)")


def test_a_ref_to_a_branch_deleted_on_the_server_proves_nothing(world, capsys):
    """The unmerged branch was deleted on the server and never pruned locally: its stale
    origin/feature is the only "proof", and it is no proof. Kept, the branch named."""
    key = _key(world)
    repo = _install(world, "s1", key, marker="s1")
    sha = _push_branch(repo)
    _delete_on_server(world)
    # What the old check saw: every commit reached by a remote-tracking ref.
    assert _git(repo, "rev-list", "--all", "--not", "--remotes") == ""
    assert _git(repo, "rev-parse", "refs/remotes/origin/feature") == sha
    before, server_before = _refs(repo), _refs(world["origin"])

    out = _remove_keeps(world, capsys, "s1", repo, "unpushed commit(s)")
    assert "origin/feature" in out
    assert "the server no longer lists" in out
    assert _git(repo, "cat-file", "-t", sha) == "commit"
    # Nothing fetched, pruned or written, here or on the server.
    assert _refs(repo) == before
    assert _refs(world["origin"]) == server_before


def test_a_stale_ref_whose_only_copy_is_that_ref_is_still_kept(world, capsys):
    """The local branch is gone too: only the stale remote-tracking ref holds the commit."""
    key = _key(world)
    repo = _install(world, "s2", key, marker="s2")
    sha = _push_branch(repo)
    _git(repo, "branch", "-q", "-D", "feature")
    _delete_on_server(world)
    out = _remove_keeps(world, capsys, "s2", repo, "unpushed commit(s)")
    assert "origin/feature" in out
    assert _git(repo, "cat-file", "-t", sha) == "commit"


def test_a_stale_ref_whose_commit_the_server_still_holds_on_main_is_deleted(world, capsys):
    """Control: the branch was merged into main on the server, then deleted there. Its stale
    ref proves nothing, but main (which the server lists now) holds the commit."""
    key = _key(world)
    repo = _install(world, "s3", key, marker="s3")
    _push_branch(repo)
    _git(repo, "merge", "-q", "--ff-only", "feature")
    _git(repo, "push", "-q", "origin", "main")
    _delete_on_server(world)
    assert _git(repo, "rev-parse", "refs/remotes/origin/feature")  # still there, stale
    code, out, err = _cli(capsys, "remove", "s3")
    assert code == 0, err
    assert not repo.exists(), out
    assert f"checkout: deleted {repo}" in out


def test_an_unreachable_server_keeps_the_checkout(world, capsys):
    """The server has gone (moved away): ls-remote fails, so nothing is proved and the
    checkout, fully pushed by its local refs, is kept with the reason."""
    key = _key(world)
    repo = _install(world, "s4", key, marker="s4")
    world["origin"].rename(world["tmp"] / "origin-moved.git")
    out = _remove_keeps(world, capsys, "s4", repo, "could not be reached")
    assert "remote origin" in out


def test_a_server_that_wants_credentials_fails_fast_and_no_credential_is_printed(
        world, capsys):
    """An https remote whose URL carries a token, on a port that answers nothing: kept, the
    server named as unreachable, and the token on no surface."""
    key = _key(world)
    repo = _install(world, "s5", key, marker="s5")
    secret = "sekret-token-1987"
    _git(repo, "remote", "set-url", "origin", f"https://drain:{secret}@127.0.0.1:1/x.git")
    code, out, err = _cli(capsys, "remove", "s5")
    assert code == 0, err
    assert repo.is_dir() and f"checkout: kept {repo}" in out
    assert "could not be reached" in out
    assert secret not in out and secret not in err
    assert key not in out and key not in err


def test_a_server_that_never_answers_times_out_without_a_prompt(world, capsys, monkeypatch):
    """An ssh remote whose transport hangs (as a password or host-key prompt would): the
    whole process group is killed at LS_REMOTE_TIMEOUT and the checkout is kept. The
    transport ran with GIT_TERMINAL_PROMPT=0 and no stdin."""
    key = _key(world)
    repo = _install(world, "s6", key, marker="s6")
    seen = world["tmp"] / "ssh-env"
    ssh = world["tmp"] / "bin" / "hang-ssh"
    ssh.write_text(f'#!/bin/sh\necho "prompt=$GIT_TERMINAL_PROMPT" > "{seen}"\n'
                   "exec sleep 60\n", encoding="utf-8")
    ssh.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(ssh))
    monkeypatch.setattr(drain, "LS_REMOTE_TIMEOUT", 1.0)
    _git(repo, "remote", "set-url", "origin", "ssh://git@server.invalid/x.git")
    out = _remove_keeps(world, capsys, "s6", repo, "could not be reached")
    assert "no answer within 1s" in out
    assert seen.read_text(encoding="utf-8").strip() == "prompt=0"


def test_ls_remote_asks_only_remotes_outside_the_checkout(world, capsys, monkeypatch):
    """An inside remote (`self` = the checkout) is never asked: its refs prove nothing
    before any server is consulted. origin, outside, is asked."""
    key = _key(world)
    repo = _install(world, "s7", key, marker="s7")
    _git(repo, "remote", "add", "self", ".")
    _git(repo, "fetch", "-q", "self")
    asked: list[str] = []
    real = drain._ls_remote

    def spy(where: Path, remote: str):
        asked.append(remote)
        return real(where, remote)

    monkeypatch.setattr(drain, "_ls_remote", spy)
    code, out, err = _cli(capsys, "remove", "s7")
    assert code == 0, err
    assert asked == ["origin"], asked


@pytest.mark.parametrize("raw,clean", [
    ("fatal: unable to access 'https://u:tok@host/x.git/': refused\nmore",
     "fatal: unable to access 'https://host/x.git/': refused"),
    ("\n  fatal: nope  \n", "fatal: nope"),
])
def test_scrub_drops_url_credentials_and_keeps_one_line(raw, clean, kind):
    assert drain._scrub(raw) == clean
