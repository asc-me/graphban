"""A GraphQL rate limit is not "no PR", and the draft still opens over REST (GRPH-1752).

`gh pr create` and the lookups before it speak GraphQL. On the wave that filed this, that
side was already exceeded while the REST core quota was not, and the supervisor reported
the create as failed. A lookup that dies the same way used to return empty, which is also
what "there is no PR" looks like, and the next step opened a sibling.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gbfleet import propose as propose_mod


RATE = "GraphQL: API rate limit already exceeded for user ID 1"
REST_URL = "https://github.com/o/r/pull/1904"
REPO = Path("/tmp/does-not-matter")


class _Gh:
    """A recorded `gh`. One lookup can die on the GraphQL rate limit; the REST create can live."""

    def __init__(self, *, limited: str = "", create_error: str = "",
                 rest_code: int = 0, rest_stdout: str | None = None):
        self.limited = limited
        self.create_error = create_error
        self.rest_code = rest_code
        self.rest_stdout = rest_stdout
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd=None, capture_output=True, text=True, timeout=None):
        self.calls.append(list(argv))
        if argv[1] == "api":
            body = self.rest_stdout
            if body is None:
                body = json.dumps({"html_url": REST_URL, "draft": True})
            return subprocess.CompletedProcess(argv, self.rest_code, body, "" if self.rest_code == 0
                                               else "REST: Validation Failed")
        verb = " ".join(argv[1:3])
        if verb == "pr view":
            if self.limited == "existing":
                return subprocess.CompletedProcess(argv, 1, "", RATE)
            return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")
        if verb == "pr list":
            if "--head" in argv and self.limited == "existing_any":
                return subprocess.CompletedProcess(argv, 1, "", RATE)
            state = argv[argv.index("--state") + 1] if "--state" in argv else ""
            if state == "open" and self.limited == "open_prs":
                return subprocess.CompletedProcess(argv, 1, "", RATE)
            if state == "merged" and self.limited == "merged_prs":
                return subprocess.CompletedProcess(argv, 1, "", RATE)
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if verb == "pr create":
            if self.create_error:
                return subprocess.CompletedProcess(argv, 1, "", self.create_error)
            return subprocess.CompletedProcess(argv, 0, f"{REST_URL}\n", "")
        raise AssertionError(f"unexpected gh call: {argv}")


@pytest.fixture()
def gh(monkeypatch):
    def _wire(fake):
        monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh")
        monkeypatch.setattr(propose_mod.subprocess, "run", fake)
        return fake
    return _wire


def _fields(argv: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """(`-f` raw fields, `-F` typed fields) from a `gh api` argv."""
    raw: dict[str, str] = {}
    typed: dict[str, str] = {}
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("-f", "-F") and i + 1 < len(args):
            key, _, value = args[i + 1].partition("=")
            (raw if args[i] == "-f" else typed)[key] = value
            i += 2
            continue
        i += 1
    return raw, typed


def _created(calls: list[list[str]]) -> bool:
    return any(c[1:3] == ["pr", "create"] for c in calls)


def _posted(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if len(c) > 1 and c[1] == "api"]


# ---- the create falls back to REST -----------------------------------------------------------
# This is the FIRST test. Sabotage: remove the REST fallback; this goes red.


def test_a_graphql_rate_limit_opens_the_draft_through_rest(gh):
    """`gh pr create` dies on the GraphQL limit. The draft still opens, and `ok` points at it."""
    fake = gh(_Gh(create_error=RATE))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="the body")

    assert got.ok and got.url == REST_URL
    assert _created(fake.calls)
    posted = _posted(fake.calls)
    assert len(posted) == 1
    argv = posted[0]
    assert argv[:5] == ["gh", "api", "-X", "POST", "repos/{owner}/{repo}/pulls"]
    raw, typed = _fields(argv)
    assert raw["head"] == "gb/w-1"
    assert raw["base"] == "main"
    assert raw["title"] == "t"
    assert raw["body"] == "the body"
    assert typed["draft"] == "true"
    assert fake.calls.index(argv) > next(
        i for i, c in enumerate(fake.calls) if c[1:3] == ["pr", "create"])


def test_a_validation_error_does_not_fall_back(gh):
    """Anything that is not the GraphQL rate limit is still the failure it was."""
    fake = gh(_Gh(create_error="pull request create failed: Validation Failed"))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="the body")

    assert not got.ok and not got.skipped
    assert got.reason.startswith("gh pr create failed:")
    assert "Validation Failed" in got.reason
    assert _posted(fake.calls) == []


def test_a_rate_limit_that_is_not_graphql_does_not_fall_back(gh):
    """The fallback is the GraphQL sentence from the incident, not every 403."""
    fake = gh(_Gh(create_error="HTTP 403: rate limit exceeded"))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert not got.ok
    assert "gh pr create failed:" in got.reason
    assert _posted(fake.calls) == []


# ---- a rate-limited lookup is not "no PR" -----------------------------------------------------


@pytest.mark.parametrize("which", ["existing", "existing_any", "open_prs", "merged_prs"])
def test_a_rate_limited_lookup_does_not_open_a_sibling(gh, which):
    """Each lookup the create waits on. Nonzero plus the rate-limit text stops the create.

    `head` is set so the open and merged listings run. The fake's create would succeed,
    so an ok result is the sibling this must not open.
    """
    fake = gh(_Gh(limited=which))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b", head="abc")

    assert not got.ok and not got.skipped
    assert RATE in got.reason
    assert not _created(fake.calls)
    assert _posted(fake.calls) == []


def test_a_failed_lookup_that_is_not_a_rate_limit_still_means_no_pr(gh):
    """The ordinary miss (`no pull requests found`) is still "nothing here" and may create."""
    fake = gh(_Gh())

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert got.ok and got.url == REST_URL
    assert _created(fake.calls)
    assert _posted(fake.calls) == []
