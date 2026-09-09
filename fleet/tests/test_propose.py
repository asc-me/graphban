"""Reaped work is proposed for merge (GRPH-804).

The other half of "done does not mean merged". GRPH-798 made the supervisor HOLD an item whose
dependency's commit is not in the base; nothing made that hold clear, because nothing in the
loop ever proposed the merge.

Reported from super-arc: SA-417 reached `done` with its commit only on `origin/gb/p11-m1-4`,
while SA-416 — which only reached `review` — did have a PR. The more complete item is the one
that went missing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gbfleet import propose as propose_mod


class _Gh:
    """A recorded `gh`. Answers `pr view` and `pr create` the way the real one does."""

    def __init__(self, *, has_pr="", create_ok=True, url="https://github.com/o/r/pull/7"):
        self.has_pr, self.create_ok, self.url = has_pr, create_ok, url
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd=None, capture_output=True, text=True, timeout=None):
        self.calls.append(argv)
        verb = " ".join(argv[1:3])
        if verb == "pr view":
            if not self.has_pr:
                return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")
            return subprocess.CompletedProcess(argv, 0, f'{{"url": "{self.has_pr}"}}', "")
        if verb == "pr create":
            if not self.create_ok:
                return subprocess.CompletedProcess(argv, 1, "", "pull request create failed")
            return subprocess.CompletedProcess(argv, 0, f"{self.url}\n", "")
        raise AssertionError(f"unexpected gh call: {argv}")


@pytest.fixture()
def gh(monkeypatch):
    def _wire(fake, installed=True):
        monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh" if installed else "")
        monkeypatch.setattr(propose_mod.subprocess, "run", fake)
        return fake
    return _wire


REPO = Path("/tmp/does-not-matter")


# ---- the happy path -----------------------------------------------------------------------

def test_a_pushed_branch_is_proposed(gh):
    fake = gh(_Gh())

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert got.ok and got.url.endswith("/pull/7")
    assert ["gh", "pr", "create"] == fake.calls[-1][:3]


def test_it_opens_a_DRAFT(gh):
    """The supervisor knows the work exists and is pushed. It does not know whether anybody
    wants it reviewed, and a review request nobody asked for is a claim on attention."""
    fake = gh(_Gh())

    propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert "--draft" in fake.calls[-1]


def test_the_base_is_the_branch_name_not_the_remote_ref(gh):
    """`gh` wants `main`, not `origin/main`. Passing the ref opens a PR against a branch that
    does not exist — or against one that does and is not the trunk."""
    fake = gh(_Gh())

    propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    argv = fake.calls[-1]
    assert argv[argv.index("--base") + 1] == "main"


# ---- the three ways it declines ---------------------------------------------------------------

def test_an_existing_pr_is_reused_rather_than_re_created(gh):
    """`gh pr create` FAILS on a branch that already has one, and that failure would read as
    "could not propose" for work proposed perfectly well an hour ago."""
    fake = gh(_Gh(has_pr="https://github.com/o/r/pull/3"))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert got.skipped and got.url.endswith("/pull/3")
    assert not any(c[1:3] == ["pr", "create"] for c in fake.calls), "created a second PR"


def test_no_gh_is_reported_not_installed(gh):
    fake = gh(_Gh(), installed=False)

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert got.skipped and not got.ok
    assert "gh is not installed" in got.reason
    assert fake.calls == [], "ran gh after finding it absent"


def test_no_base_proposes_nothing(gh):
    """A repository with no remote default has nothing to propose against, and guessing
    `main` would open a PR at the wrong target on a repo whose trunk is `develop`."""
    fake = gh(_Gh())

    got = propose_mod.propose(REPO, "gb/w-1", "", title="t", body="b")

    assert got.skipped and fake.calls == []


def test_a_failed_create_is_a_reason_not_an_exception(gh):
    """The work is committed and pushed by the time this runs. A PR that could not be opened
    is a thing for a person to finish, never a reason to call the wave broken."""
    gh(_Gh(create_ok=False))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert not got.ok and not got.skipped
    assert "gh pr create failed" in got.reason


def test_success_without_a_url_is_not_success(gh):
    """`ok` means "there is a PR to point at". A green exit code with nothing to link is not
    that, and reporting it as ok would record no evidence while claiming to have."""
    gh(_Gh(url=""))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert not got.ok
    assert "without a URL" in got.reason


# ---- what it says ---------------------------------------------------------------------------

def test_the_description_names_the_items_and_claims_nothing_else():
    """The supervisor did not do the work and cannot summarise it. Generated filler is what a
    reviewer learns to skip — and then skips on the PR that needed reading."""
    title, body = propose_mod.describe("gb/w-1", ["SA-417", "SA-420"])

    assert "SA-417" in title and "gb/w-1" in title
    assert "SA-417" in body and "SA-420" in body
    assert "draft" in body.lower()
    assert "knows nothing" in body, "did not warn that the description is generated"


def test_a_branch_with_no_items_still_gets_a_usable_title():
    title, _ = propose_mod.describe("gb/w-9", [])

    assert "gb/w-9" in title
