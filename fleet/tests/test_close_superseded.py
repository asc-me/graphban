"""GRPH-1452: a title mention is not ownership when the body declares items.

A merge titled "Merge origin/main (A) into B" names A. B's PR says `Items: B`. Closing
it when A merges drops B's only open PR. The `Items:` / `Attests:` line is ownership;
the title counts only when the body has no such line.

A branch that still has commits the merged PR does not is not superseded either. A
compare that cannot be read does not invent that fact, so an older draft whose
`Items:` line names the merged item is still closed.

The knock-on: a PR the supersede path closed, whose branch has since moved, is
reopened by `propose` rather than left closed with "not opening a sibling".
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gbfleet import propose as P

REPO = Path("/tmp/x")
KEEPER = "a" * 40
OTHER = "b" * 40
MOVED = "c" * 40


class _Gh:
    def __init__(self, open_rows, *, keeper_head="", ahead=None, reopen_ok=True,
                 any_state=None):
        self.rows = open_rows
        self.keeper_head = keeper_head
        # None: the compare cannot be read. An int is `ahead_by` of the other head.
        self.ahead = ahead
        self.reopen_ok = reopen_ok
        self.any_state = any_state
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        verb = " ".join(argv[1:3])
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        if verb == "pr view":
            number = argv[3] if len(argv) > 3 else ""
            if self.keeper_head and number.isdigit():
                return ok(json.dumps({"headRefOid": self.keeper_head}))
            return subprocess.CompletedProcess(argv, 1, "", "none")
        if verb == "pr list":
            state = argv[argv.index("--state") + 1] if "--state" in argv else "open"
            if state == "all" and self.any_state is not None:
                return ok(json.dumps(self.any_state))
            if state == "open":
                return ok(json.dumps(self.rows))
            return ok("[]")
        if verb == "pr close":
            return ok()
        if verb == "pr reopen":
            if not self.reopen_ok:
                return subprocess.CompletedProcess(argv, 1, "", "failed to reopen")
            return ok("reopened\n")
        if argv[1:2] == ["api"]:
            # `gh api <path>` is one verb plus a path, not "api <path>" as two words.
            if self.ahead is None:
                return subprocess.CompletedProcess(argv, 1, "", "compare unread")
            return ok(json.dumps({"ahead_by": self.ahead}))
        if verb == "pr create":
            return ok("https://github.com/o/r/pull/20\n")
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


def _pr(n, branch, *, sha=OTHER, draft=True, title="untitled", body=""):
    return {"number": n, "url": f"https://github.com/o/r/pull/{n}", "headRefName": branch,
            "headRefOid": sha, "isDraft": draft, "title": title, "body": body}


def _close(item="A"):
    return P.close_item_prs(REPO, item, "https://github.com/o/r/pull/1457",
                            comment="Superseded by #1457")


@pytest.mark.parametrize("line", ["Items: B", "Attests: B"])
def test_a_merge_title_naming_a_does_not_close_the_pr_that_declares_b(gh, line):
    """Acceptance: titled "Merge origin/main (A) into B", body declares B, A's merge
    does not close it."""
    fake = gh(_Gh([_pr(1461, "gb/p68-37",
                       title="Merge origin/main (A) into B", body=line + "\n")]))
    assert _close() == ()
    assert fake.closed() == []


def test_an_older_draft_whose_items_line_names_the_merged_item_is_still_closed(gh):
    """Acceptance: the Items: line still means ownership. The title does not name A,
    so a title-only match would leave this open."""
    fake = gh(_Gh([_pr(8, "gb/w-4", title="older attempt", body="Items: A\n")]))
    assert _close() == ("#8",)
    assert fake.closed() == [("8", "Superseded by #1457")]


def test_title_still_counts_when_the_body_declares_no_items(gh):
    fake = gh(_Gh([_pr(5, "gb/w-1", title="A: the work", body="mentions A in passing\n")]))
    assert _close() == ("#5",)


def test_a_branch_with_commits_the_merged_pr_lacks_stays_open(gh):
    """What: never close a PR whose branch has commits the keeper does not."""
    fake = gh(_Gh(
        [_pr(8, "gb/w-4", sha=OTHER, title="A: earlier", body="Items: A\n")],
        keeper_head=KEEPER, ahead=3,
    ))
    assert _close() == ()
    assert fake.closed() == []
    assert any(c[1:2] == ["api"] and f"{KEEPER}...{OTHER}" in c[2] for c in fake.calls)


def test_a_branch_contained_in_the_merged_pr_is_still_closed(gh):
    fake = gh(_Gh(
        [_pr(8, "gb/w-4", sha=OTHER, title="older attempt", body="Items: A\n")],
        keeper_head=KEEPER, ahead=0,
    ))
    assert _close() == ("#8",)


def test_an_unreadable_compare_does_not_spare_a_declared_draft(gh):
    """Could not tell whether the branch has extra commits. That is not proof it does,
    and the older draft whose Items: line names A is still closed."""
    fake = gh(_Gh(
        [_pr(8, "gb/w-4", title="older attempt", body="Items: A\n")],
        keeper_head=KEEPER, ahead=None,
    ))
    assert _close() == ("#8",)
    assert any(c[1:2] == ["api"] for c in fake.calls)


def test_a_superseded_pr_whose_branch_moved_is_reopened(gh):
    """Knock-on: #1461 was closed as superseded, then the branch moved. propose reopens
    it. It does not stop at "already has closed PR; not opening a sibling"."""
    closed = {"url": "https://github.com/o/r/pull/1461", "state": "CLOSED", "number": 1461,
              "headRefOid": OTHER}
    fake = gh(_Gh([], any_state=[closed]))
    got = P.propose(REPO, "gb/p68-37", "origin/main", title="t", body="Items: B\n",
                    head=MOVED, items=["B"])
    assert got.ok and got.url.endswith("/pull/1461")
    assert "reopened existing PR #1461" in got.reason
    assert any(c[1:3] == ["pr", "reopen"] and c[3] == "1461" for c in fake.calls)
    assert not any(c[1:3] == ["pr", "create"] for c in fake.calls)
    assert "not opening a sibling" not in got.reason
