"""GRPH-1396: no second PR for a commit that has one; superseded drafts get closed."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gbfleet import propose as P

REPO = Path("/tmp/x")
SHA = "a" * 40


class _Gh:
    def __init__(self, open_rows, create_url="https://github.com/o/r/pull/20"):
        self.rows, self.create_url = open_rows, create_url
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        verb = " ".join(argv[1:3])
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        if verb == "pr view":
            return subprocess.CompletedProcess(argv, 1, "", "none")
        if verb == "pr list" and "--state" in argv and argv[argv.index("--state") + 1] == "open":
            return ok(json.dumps(self.rows))
        if verb == "pr list":
            return ok("[]")
        if verb == "pr create":
            return ok(self.create_url + "\n")
        if verb == "pr close":
            return ok()
        raise AssertionError(argv)

    def closed(self):
        return [(c[3], c[c.index("--comment") + 1]) for c in self.calls if c[1:3] == ["pr", "close"]]


@pytest.fixture()
def gh(monkeypatch):
    def wire(fake):
        monkeypatch.setattr(P, "find", lambda: "/usr/bin/gh")
        monkeypatch.setattr(P.subprocess, "run", fake)
        return fake
    return wire


def _pr(n, branch, *, sha="b" * 40, draft=True, title="GRPH-1: thing", body=""):
    return {"number": n, "url": f"https://github.com/o/r/pull/{n}", "headRefName": branch,
            "headRefOid": sha, "isDraft": draft, "title": title, "body": body}


def test_same_head_commit_on_another_branch_opens_no_second_pr(gh):
    fake = gh(_Gh([_pr(5, "gb/w-1", sha=SHA)]))
    got = P.propose(REPO, "gb/w-2", "origin/main", title="t", body="b", head=SHA, items=["GRPH-1"])
    assert got.skipped and not got.ok and "#5" in got.reason
    assert not any(c[1:3] == ["pr", "create"] for c in fake.calls)


def test_a_different_head_still_opens(gh):
    fake = gh(_Gh([_pr(5, "gb/w-1", sha="c" * 40)]))
    got = P.propose(REPO, "gb/w-2", "origin/main", title="t", body="b", head=SHA)
    assert got.ok
    assert any(c[1:3] == ["pr", "create"] for c in fake.calls)


def test_newer_attempt_closes_the_older_draft_of_the_same_item(gh):
    fake = gh(_Gh([_pr(5, "gb/w-1"), _pr(6, "gb/w-9", title="GRPH-2: other"),
                   _pr(7, "gb/w-3", draft=False)]))
    got = P.propose(REPO, "gb/w-2", "origin/main", title="t", body="b", head=SHA,
                    items=["GRPH-1"])
    assert got.ok
    # the other item's draft and the non-draft are left alone
    assert fake.closed() == [("5", "Superseded by #20")]


def test_a_newer_draft_is_not_closed_by_an_older_attempt(gh):
    fake = gh(_Gh([_pr(30, "gb/w-1")], create_url="https://github.com/o/r/pull/20"))
    P.propose(REPO, "gb/w-2", "origin/main", title="t", body="b", head=SHA, items=["GRPH-1"])
    assert fake.closed() == []


def test_merge_closes_other_open_gb_prs_of_the_item_naming_the_merged_one(gh):
    fake = gh(_Gh([_pr(5, "gb/w-1"), _pr(6, "gb/w-2", draft=False),
                   _pr(7, "feature/x"), _pr(8, "gb/w-4", title="x", body="Items: GRPH-1\n"),
                   _pr(9, "gb/w-5", title="x", body="mentions GRPH-1 in passing"),
                   _pr(10, "gb/w-6", title="GRPH-11: other")]))
    closed = P.close_item_prs(REPO, "GRPH-1", "https://github.com/o/r/pull/20",
                              comment="Superseded by #20")
    assert closed == ("#5", "#6", "#8")
    assert {c for c in fake.closed()} == {("5", "Superseded by #20"), ("6", "Superseded by #20"),
                                          ("8", "Superseded by #20")}


def test_close_after_merge_extends_closed(gh):
    fake = gh(_Gh([_pr(5, "gb/w-1")]))
    merged = P.Merged(item="GRPH-1", selector="s", url="https://github.com/o/r/pull/20", ok=True)
    got = P.close_after_merge(REPO, merged)
    assert got.closed == ("#5",)
    assert fake.closed() == [("5", "Superseded by #20")]
