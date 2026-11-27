"""`gbfleet drain remove` and two low-severity ways deleting a checkout went wrong (GRPH-1986).

F3: a `git clone --shared` / `--reference` clone of the drain's checkout borrows its objects
through objects/info/alternates; deleting the checkout broke it silently. A borrower beside
the checkout keeps it, named; any other gets a warning naming the checkout when it goes.

F2: `git fetch . HEAD:refs/remotes/origin/x` writes a local commit into origin's namespace.
A remote-tracking ref whose newest reflog entry is a fetch from inside the checkout proves
nothing, and the keep reason says so.

Every server is a temp bare repo; every case runs on the launchd and systemd fakes.
"""
from __future__ import annotations

import subprocess

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


# ---- F3: --shared / --reference clones ----------------------------------------------------

def test_a_shared_clone_beside_the_checkout_keeps_it_and_is_named(world, capsys):
    key = _key(world)
    repo = _install(world, "s1", key, marker="s1")
    borrower = repo.parent / "s1-shared"
    subprocess.run(["git", "clone", "-q", "--shared", str(repo), str(borrower)], check=True)
    assert (borrower / ".git" / "objects" / "info" / "alternates").exists()
    out = _remove_keeps(world, capsys, "s1", repo, "--shared or --reference")
    assert str(borrower) in out
    assert "git repack -a" in out
    # The borrower still works: its objects are still there.
    assert _git(borrower, "log", "--oneline", "-1")


def test_a_reference_clone_with_a_relative_alternates_path_keeps_it(world, capsys):
    key = _key(world)
    repo = _install(world, "s2", key, marker="s2")
    borrower = repo.parent / "s2-ref"
    subprocess.run(["git", "clone", "-q", "--bare", "--reference", str(repo),
                    str(world["origin"]), str(borrower)], check=True)
    alt = borrower / "objects" / "info" / "alternates"
    # Rewrite as a relative path (git allows it; relative to the borrower's objects dir).
    alt.write_text("../../s2/.git/objects\n", encoding="utf-8")
    out = _remove_keeps(world, capsys, "s2", repo, "borrow its objects")
    assert str(borrower) in out


def test_a_clean_checkout_with_no_borrower_is_deleted_with_a_warning_naming_it(world, capsys):
    key = _key(world)
    repo = _install(world, "s3", key, marker="s3")
    # A plain sibling clone (no alternates) does not keep it.
    subprocess.run(["git", "clone", "-q", str(world["origin"]), str(repo.parent / "plain")],
                   check=True)
    code, out, err = _cli(capsys, "remove", "s3")
    assert code == 0, err
    assert not repo.exists(), out
    assert f"checkout: deleted {repo}" in out
    warning = next(line for line in out.splitlines() if "warning" in line)
    assert str(repo) in warning and "--shared" in warning and "--reference" in warning
    assert "re-clone" in warning and "git repack -a" in warning


def test_borrowers_ignores_an_alternates_pointing_elsewhere(world):
    key = _key(world)
    repo = _install(world, "s4", key, marker="s4")
    other = world["tmp"] / "elsewhere"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    sib = repo.parent / "s4-other"
    subprocess.run(["git", "clone", "-q", "--shared", str(other), str(sib)], check=True)
    assert drain._borrowers(repo) == ([], "")


# ---- F2: a fetch from `.` into origin's namespace ------------------------------------------

def _fetch_self(repo, dst: str) -> None:
    _git(repo, "fetch", "-q", ".", f"HEAD:{dst}")


def test_a_fetch_from_dot_into_origins_namespace_does_not_count_as_pushed(world, capsys):
    key = _key(world)
    repo = _install(world, "f1", key, marker="f1")
    _unpushed(repo)
    _fetch_self(repo, "refs/remotes/origin/x")
    assert _git(repo, "rev-parse", "refs/remotes/origin/x") == _git(repo, "rev-parse", "HEAD")
    out = _remove_keeps(world, capsys, "f1", repo, "unpushed commit(s)")
    assert "origin/x were last written by a fetch from inside this checkout" in out


def test_a_fetch_from_an_absolute_path_inside_the_checkout_does_not_count(world):
    key = _key(world)
    repo = _install(world, "f2", key, marker="f2")
    _unpushed(repo)
    _git(repo, "fetch", "-q", str(repo / ".git"), "HEAD:refs/remotes/origin/y")
    assert drain._fetched_from_inside(repo, ["refs/remotes/origin/y", "refs/remotes/origin/main"]
                                      ) == ["refs/remotes/origin/y"]


def test_a_real_fetch_from_origin_still_proves_a_push(world, capsys):
    key = _key(world)
    repo = _install(world, "f3", key, marker="f3")
    _unpushed(repo)
    _git(repo, "push", "-q", "origin", "HEAD:main")
    _git(repo, "fetch", "-q", "origin")
    assert drain._fetched_from_inside(repo, ["refs/remotes/origin/main"]) == []
    code, out, err = _cli(capsys, "remove", "f3")
    assert code == 0, err
    assert not repo.exists(), out
