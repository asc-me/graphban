"""A PR create that hits a GitHub limit is retried, and no reviewer is spawned for it (GRPH-1730).

The REST fallback for a GraphQL rate limit lives in `propose` (GRPH-1752). These tests cover
that path and the two clauses it does not: a failed create is tried again on a later poll,
and `until` does not spawn a reviewer whose only refusal so far is `pr_missing`.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import propose as propose_mod
from gbfleet import until


REPO = Path("/tmp/does-not-matter")
RATE = "GraphQL: API rate limit already exceeded for user ID 1"
URL = "https://github.com/o/r/pull/9"
PR_MISSING = ("refused at `done` (pr_missing): work is on branch gb/x "
              "and no PR is linked")


def _cp(code, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class _Gh:
    """A recorded `gh`. The first creates can die; a later call, or REST, can live."""

    def __init__(self, *, create_error: str = "", rest_code: int = 0):
        self.create_error = create_error
        self.rest_code = rest_code
        self.calls: list[tuple] = []

    def __call__(self, repo, *args, timeout=60.0):
        self.calls.append(args)
        if args[:1] == ("api",):
            if self.rest_code != 0:
                return _cp(self.rest_code, err="REST: rate limit exceeded")
            return _cp(0, json.dumps({"html_url": URL, "draft": True}))
        if args[:2] == ("pr", "create"):
            if self.create_error:
                return _cp(1, err=self.create_error)
            return _cp(0, out=f"{URL}\n")
        if args[:2] == ("pr", "list"):
            return _cp(0, "[]")
        return _cp(1, err="no pull requests found")


class _Planner:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.posts: list[dict] = []

    def post_observation(self, **payload):
        if self.fail:
            raise RuntimeError("observations route refused")
        self.posts.append(payload)
        return {"recorded": True}


def _wire(monkeypatch, gh: _Gh):
    monkeypatch.setattr(propose_mod, "_gh", gh)
    monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh")
    monkeypatch.setattr(propose_mod, "title_subject", lambda *a, **k: "subj")
    return gh


def _row(**extra) -> dict:
    row = {"id": "GRPH-1", "branch": "gb/x"}
    row.update(extra)
    return row


def _open(row, planner, logged=None):
    return until._pr_opened([row], REPO, "origin/main", set() if logged is None else logged,
                            planner=planner)


# ---- REST fallback (GRPH-1752's path, called from this item's test) --------------------------
# Sabotage: drop the REST branch in propose(); this goes red.


def test_a_graphql_rate_limit_opens_the_draft_through_rest(monkeypatch):
    """`gh pr create` dies on the GraphQL limit. The same draft opens over REST."""
    gh = _wire(monkeypatch, _Gh(create_error=RATE))

    got = propose_mod.propose(REPO, "gb/x", "origin/main", title="t", body="b")

    assert got.ok and got.url == URL
    posted = [c for c in gh.calls if c[:1] == ("api",)]
    assert len(posted) == 1
    assert posted[0][:4] == ("api", "-X", "POST", "repos/{owner}/{repo}/pulls")
    assert gh.calls.index(posted[0]) > next(
        i for i, c in enumerate(gh.calls) if c[:2] == ("pr", "create"))


def test_a_non_graphql_failure_does_not_fall_back_to_rest(monkeypatch):
    """A 502 is not the GraphQL sentence. REST stays unused; the next poll retries `gh`."""
    gh = _wire(monkeypatch, _Gh(create_error="HTTP 502 Bad Gateway"))

    got = propose_mod.propose(REPO, "gb/x", "origin/main", title="t", body="b")

    assert not got.ok and not got.skipped
    assert "502" in got.reason
    assert not any(c[:1] == ("api",) for c in gh.calls)


# ---- a later cycle retries --------------------------------------------------------------------
# Sabotage: mark a failed open ready inside `_pr_opened`; the waiting assertions go red.


def test_a_transient_failure_is_retried_on_a_later_cycle(monkeypatch):
    """The first poll reports the 502 and holds the row. The next poll opens the PR."""
    gh = _wire(monkeypatch, _Gh(create_error="HTTP 502 Bad Gateway"))
    planner = _Planner()
    row = _row()

    ready, waiting = _open(row, planner)
    assert waiting == [row] and not ready and planner.posts == []

    gh.create_error = ""
    ready, waiting = _open(row, planner)
    assert ready == [row] and not waiting
    assert planner.posts == [{
        "item_id": "GRPH-1", "kind": "pr_opened", "branch": "gb/x", "url": URL,
    }]
    assert until._linked_pr(row) == URL


def test_a_rate_limit_whose_rest_fallback_also_fails_is_retried(monkeypatch):
    """GraphQL and REST both spent: the row waits. A later poll's REST open is the PR."""
    gh = _wire(monkeypatch, _Gh(create_error=RATE, rest_code=1))
    planner = _Planner()
    row = _row(evidence=[{"kind": "note", "detail": PR_MISSING}])

    ready, waiting = _open(row, planner)
    assert waiting == [row] and not ready

    gh.rest_code = 0
    ready, waiting = _open(row, planner)
    assert ready == [row] and not waiting
    assert planner.posts[-1]["url"] == URL
    assert any(c[:4] == ("api", "-X", "POST", "repos/{owner}/{repo}/pulls") for c in gh.calls)


def test_a_row_whose_only_refusal_is_pr_missing_waits_when_the_open_fails(monkeypatch):
    """The refusal that sent nine reviewers around GRPH-1727 does not spawn another."""
    _wire(monkeypatch, _Gh(create_error=RATE, rest_code=1))
    row = _row(evidence=[{"kind": "note", "detail": PR_MISSING}])

    ready, waiting = _open(row, _Planner())

    assert waiting == [row] and not ready


def test_a_row_with_some_other_refusal_keeps_its_reviewer(monkeypatch):
    """`pr_missing` is the only refusal this gate holds. A different one still spawns."""
    gh = _wire(monkeypatch, _Gh())
    row = _row(evidence=[{"kind": "note", "detail": "refused at `done` (adversarial): nope"}])

    ready, waiting = _open(row, _Planner())

    assert ready == [row] and not waiting
    assert gh.calls == []


def test_a_row_that_already_has_a_pr_is_not_opened_again(monkeypatch):
    gh = _wire(monkeypatch, _Gh())
    row = _row(evidence=[{"kind": "url", "url": URL, "detail": "draft PR opened by gbfleet for gb/x"}])

    ready, waiting = _open(row, _Planner())

    assert ready == [row] and not waiting
    assert not [c for c in gh.calls if c[:2] != ("pr", "view")]


def test_a_pr_that_cannot_be_linked_is_not_treated_as_ready(monkeypatch):
    """The PR existing on the forge is not a link. `sign_off` would still say `pr_missing`."""
    _wire(monkeypatch, _Gh())
    row = _row()

    ready, waiting = _open(row, _Planner(fail=True))

    assert waiting == [row] and not ready
    assert until._linked_pr(row) == ""


def test_a_repo_that_cannot_propose_does_not_wedge_the_row(monkeypatch):
    """No `gh` is a fact about this machine. Holding every review row would never drain."""
    monkeypatch.setattr(propose_mod, "find", lambda: "")
    monkeypatch.setattr(propose_mod, "title_subject", lambda *a, **k: "subj")
    row = _row()

    ready, waiting = _open(row, _Planner())

    assert ready == [row] and not waiting


# ---- the loop, not the helper -----------------------------------------------------------------
# The nine tests above call `_pr_opened` directly. Dropping the waiting row from that
# helper's return value used to leave `claim_review` free to hand it out, and gating the
# helper on `not review_busy` then stopped the retry for the whole lease. This one goes
# through `until.run`.
# Sabotage: put `and not review_busy` back on the `_pr_opened` call. The second `gh pr
# create` then lands only after the reviewer has exited, and the assertion below fails.


def test_a_second_ready_row_does_not_let_the_reviewer_take_the_one_waiting_on_its_pr(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """Two review rows. One already has a PR; one does not, and `gh` is rate-limited.

    The loop still mints a reviewer for the ready row, tells it to skip the waiting
    one, and calls `gh pr create` again while that reviewer is still alive.
    """
    from gbfleet.supervisor import Limits, WaveBase
    from tests.test_supervisor import KEY, _factory
    from tests.test_until import _clients
    from gbfleet.until import run

    def _base(repo, client=None, *, base_branch=""):
        return WaveBase(ref="main", source="head", note="test base")

    monkeypatch.setattr(until, "resolve_wave_base", _base)
    gh = _wire(monkeypatch, _Gh(create_error=RATE, rest_code=1))
    workspace = tmp_path / "ws"
    here = scripts["works_until_released"].parent
    minted: list[dict] = []
    seen: dict = {}

    def _sleep(_seconds: float) -> None:
        creates = sum(1 for call in gh.calls if call[:2] == ("pr", "create"))
        if creates >= 2 and "child_gone" not in seen:
            seen["creates"] = creates
            seen["child_gone"] = (here / "builder-exited").exists()
            seen["instructions"] = [
                path.read_text(encoding="utf-8")
                for path in workspace.rglob(".gbfleet-instruction")]
        if creates >= 2:
            (here / "release").write_text("", encoding="utf-8")

    ready_row = {
        "id": "GRPH-READY", "status": "review", "branch": "gb/ready",
        "claimed_by": "GRPH-A1", "review_claimed_by": "",
        "evidence": [{"kind": "url", "url": URL, "detail": "draft PR opened by gbfleet for gb/ready"}],
    }
    wait_row = {
        "id": "GRPH-WAIT", "status": "review", "branch": "gb/wait",
        "claimed_by": "GRPH-A1", "review_claimed_by": "",
        "evidence": [{"kind": "note", "detail": PR_MISSING}],
    }
    planner, supervisor = _clients(
        workspace, review=[wait_row, ready_row], minted_args=minted)
    result = run(
        git_repo, _factory(scripts, "works_until_released"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        state=state, workspace=workspace, poll=0, sleep=_sleep, empty_ticks=3,
        limits=Limits(max_workers=1, max_children=6),
    )

    assert seen.get("child_gone") is False, seen
    assert seen["creates"] >= 2
    assert any(args.get("review_only") for args in minted), minted
    texts = seen["instructions"]
    assert texts, "the review seat wrote no instruction"
    skips = []
    for text in texts:
        marked = text.split("skip=", 1)
        assert len(marked) == 2, text
        skips.append(json.loads(marked[1].split(". Those rows", 1)[0]))
    assert any("GRPH-WAIT" in ids and "GRPH-READY" not in ids for ids in skips), skips
    waits = result.as_json().get("review_waits") or []
    assert any(w == "GRPH-WAIT: review waits on its PR being opened" for w in waits), waits
    assert result.reason == "review-unsigned"


# ---- a rebuilt item's PR (GRPH-1881) ----------------------------------------------------------
# Sabotage: drop `superseded` from the `_pr_opened` guard; this goes red.


def test_a_rebuilt_item_gets_a_pr_for_its_new_branch(monkeypatch):
    """The item's only PR heads the superseded gb/old. A PR opens for item.branch gb/new."""
    old = "https://github.com/o/r/pull/5"
    _wire(monkeypatch, _Gh())
    closed = []
    monkeypatch.setattr(propose_mod, "view", lambda repo, sel: (
        {"headRefName": "gb/old", "headRefOid": "aaa"}, ""))
    monkeypatch.setattr(propose_mod, "close_item_prs",
                        lambda repo, item, keeper, **kw: closed.append((item, keeper)) or ())
    monkeypatch.setattr(until, "_tip", lambda repo, b: "bbb")
    monkeypatch.setattr(until.wt_mod, "is_ancestor", lambda *a: False)
    planner = _Planner()
    row = _row(branch="gb/new", evidence=[{"kind": "url", "url": old}])

    ready, waiting = _open(row, planner)

    assert ready == [row] and not waiting
    assert planner.posts == [{"item_id": "GRPH-1", "kind": "pr_opened",
                              "branch": "gb/new", "url": URL}]
    assert ("GRPH-1", URL) in closed
    assert until._linked_pr(row) == URL


def test_a_rebuilt_item_stops_logging_review_branch_mismatch(monkeypatch):
    """After the new PR is stamped, the review gate reads gb/new and names no mismatch.

    The row holds the old PR (gb/old, which does not descend from gb/new) and, once
    `_pr_opened` has run, the newly stamped pr_opened link for gb/new. `review_ci_branch`
    resolves to gb/new and emits no `review_branch_mismatch` — the loop ends on the
    first cycle instead of repeating every poll.

    Sabotage: drop the `reversed(...)` in `_linked_pr`; the gate reads the old PR again
    and emits `review_branch_mismatch item.branch gb/new pr gb/old`.
    """
    old = "https://github.com/o/r/pull/5"
    heads = {old: {"headRefName": "gb/old", "headRefOid": "aaa"},
             URL: {"headRefName": "gb/new", "headRefOid": "bbb"}}
    _wire(monkeypatch, _Gh())
    monkeypatch.setattr(propose_mod, "view", lambda repo, sel: (heads[sel], ""))
    monkeypatch.setattr(propose_mod, "close_item_prs", lambda *a, **kw: ())
    monkeypatch.setattr(until, "_tip", lambda repo, b: "bbb")
    monkeypatch.setattr(until.wt_mod, "is_ancestor", lambda *a: False)
    events: list[tuple] = []
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: events.append((ev, kw)))
    row = _row(branch="gb/new", evidence=[{"kind": "url", "url": old}])
    logged: set = set()

    ready, waiting = _open(row, _Planner(), logged)
    assert ready == [row] and not waiting

    for _poll in range(2):
        assert until.review_ci_branch(REPO, row, logged) == ("gb/new", "")
    assert not [ev for ev, _ in events if ev == "review_branch_mismatch"], events


# ---- a branch that was never pushed (GRPH-1891) -----------------------------------------------
# Sabotage: drop `_publish_if_local_only` from `_pr_opened`; the create fails and these go red.


class _LocalOnly(_Gh):
    """`gh pr create` refuses until the branch has been pushed."""

    def __init__(self):
        super().__init__()
        self.pushed = False

    def __call__(self, repo, *args, timeout=60.0):
        if args[:2] == ("pr", "create") and not self.pushed:
            self.calls.append(args)
            return _cp(1, err="Head sha can't be blank, Head ref must be a branch")
        return super().__call__(repo, *args, timeout=timeout)


def _local_only(monkeypatch, *, push_ok=True):
    from gbfleet import worktree as wt
    gh = _wire(monkeypatch, _LocalOnly())
    monkeypatch.setattr(wt, "branch_exists", lambda r, b: True)
    monkeypatch.setattr(wt, "remote_name",
                        lambda r, b: wt.RemoteName(known=True, exists=gh.pushed))
    pushes = []

    def push(repo, branch, base):
        pushes.append(branch)
        if push_ok:
            gh.pushed = True
            return wt.Pushed(branch=branch, ok=True)
        return wt.Pushed(branch=branch, reason="push refused: denied")

    monkeypatch.setattr(wt, "push_branch", push)
    return gh, pushes


def test_a_local_only_branch_is_pushed_then_its_pr_is_opened(monkeypatch):
    gh, pushes = _local_only(monkeypatch)
    planner = _Planner()
    row = _row()

    ready, waiting = _open(row, planner)

    assert pushes == ["gb/x"]
    assert ready == [row] and not waiting
    assert planner.posts == [{
        "item_id": "GRPH-1", "kind": "pr_opened", "branch": "gb/x", "url": URL,
    }]


def test_a_failed_publish_is_withheld_on_the_item_not_retried_as_a_create(monkeypatch):
    gh, pushes = _local_only(monkeypatch, push_ok=False)
    planner = _Planner()
    row = _row()

    ready, waiting = _open(row, planner)

    assert waiting == [row] and not ready
    assert not [c for c in gh.calls if c[:2] == ("pr", "create")]
    assert planner.posts == [{
        "item_id": "GRPH-1", "kind": "unpushed", "branch": "gb/x",
        "reason": "push refused: denied",
    }]


def test_a_push_that_keeps_failing_is_said_on_the_item_once(monkeypatch):
    """`_pr_opened` runs every poll and the server appends every observation."""
    gh, pushes = _local_only(monkeypatch, push_ok=False)
    planner = _Planner()
    events = []
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: events.append((ev, kw)))
    row = _row()
    logged: set = set()

    for _poll in range(3):
        ready, waiting = _open(row, planner, logged)
        assert waiting == [row] and not ready

    assert pushes == ["gb/x"] * 3
    assert [p["kind"] for p in planner.posts] == ["unpushed"]
    assert len([ev for ev, _ in events if ev == "review_waits_on_pr"]) == 1


def test_a_lost_unpushed_note_is_logged_and_the_wait_still_names_it(monkeypatch):
    gh, pushes = _local_only(monkeypatch, push_ok=False)
    events = []
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: events.append((ev, kw)))
    row = _row()

    ready, waiting = _open(row, _Planner(fail=True))

    assert waiting == [row] and not ready
    assert [ev for ev, _ in events] == ["unpushed_note_lost", "review_waits_on_pr"]
    assert "never pushed" in events[1][1]["detail"]
