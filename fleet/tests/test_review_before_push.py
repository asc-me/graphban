"""An item must be readable before it is reviewable (GRPH-987).

**What went wrong.** A child says it is done by moving its item to `review`. That makes the item
visible to every reviewer, everywhere, immediately. Its branch was pushed by the supervisor at
REAP — when the child exited — and a bound seat does not exit when it finishes building: PRD-39
D-h tells it to review other work in the same process. So the branch it committed stayed local
for the rest of that run while the item advertised it.

Wave p47f lost that race. A cursor-agent reviewer took GRPH-961 and bounced it:

    Branch gb/p47f-1 and commit 63b46358 are not on origin; reviewer cannot fetch or verify
    the diff.

The refusal was correct — reviewing a diff you cannot read is what GRPH-973 exists to prevent.
Minutes later `origin/gb/p47f-1` was `63b46358`, identical to local, and the reviewer's own clone
fetched it without trouble. Nothing was wrong except the ordering. The bounce still sent the item
back to `next` with a reason that reads like a verdict on the work, and the preference matrix
still counted it as a failed attempt for the builder's vendor and model.

And the arrangement that loses the race is the one PRD-39 argues for: a same-checkout reviewer
never notices, and a same-checkout reviewer is usually a same-vendor one, because `until` spawns
reviewers with its own `--adapter`.

**What these pin.** The supervisor publishes on observing the transition rather than at reap; the
publish report follows a real push and never precedes it; an early push proposes no PR; and a
revision that could not be made readable is distinguishable on the attempt record from one that
was.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from gbfleet import supervisor as sup
from gbfleet import worktree as wt
from gbfleet.client import ALLOWED_TOOLS, Graphban
from gbfleet.spawn import Child
from gbfleet.supervisor import Limits, Wave, watch_tick

KEY = "gbk_test"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _server(posts: list, review: list[str], *, reads_items: bool = True,
            reachable: bool = True) -> Graphban:
    """A fake ledger that answers `fleet_status`, `search_items` and the attempts POST.

    `reads_items=False` builds the client a pure supervisor gets: `ALLOWED_TOOLS` is two reads
    and `search_items` is not one of them, which is the "could not ask" case rather than the
    "asked and found nothing" one.
    """
    allowed = ALLOWED_TOOLS | ({"search_items"} if reads_items else frozenset())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/mcp":
            posts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"id": "at_1"})
        body = json.loads(request.content)
        name = body["params"]["name"]
        if name == "search_items":
            payload = {"results": [{"id": i, "title": "t", "status": "review"} for i in review],
                       "fields": ["id", "title", "status"]}
        elif name == "propose_allocation":
            payload = {"workers": 0, "reviewers": 0, "mapping": [], "rationale": "none"}
        else:
            payload = {"agents": [], "presence_ttl_seconds": 150}
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                       "structuredContent": payload}})

    transport = httpx.MockTransport(handler) if reachable else httpx.MockTransport(_down)
    return Graphban("http://gb.invalid", KEY, allowed=allowed, transport=transport)


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no route to host")


class _Spy:
    """Records which TOOLS were asked for. `Graphban` is a dataclass, so a test cannot simply
    rebind `call` on an instance."""

    def __init__(self, inner: Graphban) -> None:
        self.inner = inner
        self.tools: list[str] = []

    @property
    def allowed(self):
        return self.inner.allowed

    def call(self, tool, /, **kwargs):
        self.tools.append(tool)
        return self.inner.call(tool, **kwargs)

    def post_attempt(self, **payload):
        return self.inner.post_attempt(**payload)


def _exit_posts(posts: list) -> list[dict]:
    """The attempt posts that describe an ENDING. `_publish_in_review` posts to the same path
    with only `branch_published` on it, and picking the first post by `enrolment_id` alone
    reads that one and finds no `exit_meaning`."""
    return [b for _, b in posts if b.get("enrolment_id") and "exit_meaning" in b]


class _Live:
    """A process that has not exited, which is the whole point of the ordering being tested."""

    pid = 4242
    returncode = None

    def poll(self):
        return None


@pytest.fixture
def builder(git_repo: Path, tmp_path: Path):
    """A running child on a branch that carries one commit, with a remote to push to.

    Returns `(child, bare, head)`. The bare repo is `origin`; nothing else stands in for the
    reviewer's own checkout, because "reachable from a different clone" is the claim.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(git_repo, "remote", "add", "origin", str(bare))

    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A1")
    (tree.path / "feature.py").write_text("print('the work')\n", encoding="utf-8")
    _git(tree.path, "add", "-A")
    _git(tree.path, "commit", "-qm", "GRPH-987: the work a reviewer needs to read")
    head = _git(tree.path, "rev-parse", "HEAD")

    child = Child(adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
                  seat_path=tmp_path / "seat.json", process=_Live(),
                  started_at=time.monotonic(), log_dir=tmp_path / "logs",
                  seat_id="seat-1", held_items=["GRPH-961"])
    child.log_dir.mkdir(parents=True, exist_ok=True)
    return child, bare, head


def _remote_branches(bare: Path) -> str:
    return subprocess.run(["git", "-C", str(bare), "branch", "--list"],
                          capture_output=True, text=True).stdout


# ---- criterion 1: the ordering ----------------------------------------------------------------

def test_an_item_in_review_is_pushed_while_its_child_is_still_running(builder, tmp_path: Path):
    """THE ORDERING, through `watch_tick` so the wiring is what is under test.

    The child is alive and its worktree still exists when the branch reaches the remote, and a
    SECOND CLONE — a reviewer anywhere but this checkout — can fetch and read the work. Before
    this, both happened at reap, which a bound seat does not reach until it has finished
    reviewing everybody else's work too.

    Sabotage: drop `_publish_in_review` from `watch_tick` and this fails, with the branch still
    local and the second clone still unable to see it. That is the mutation the item asks for,
    and it is the old ordering restored exactly.
    """
    child, bare, head = builder
    posts: list = []
    client = _server(posts, ["GRPH-961"])

    wave = Wave()
    watch_tick(wave, [child], Limits(), client, debug=False)

    assert child.running, "the child must still be alive — that is the case reap never reached"
    assert child.worktree.exists(), "a running child's worktree was removed"
    assert child.branch in _remote_branches(bare), f"{child.branch} never reached the remote"
    assert _git(bare, "rev-parse", child.branch) == head

    # The reviewer's own checkout, which is where the p47f bounce came from.
    reviewer = tmp_path / "reviewer-clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(reviewer)], check=True)
    _git(reviewer, "fetch", "-q", "origin", child.branch)
    files = _git(reviewer, "show", "--pretty=format:", "--name-only", f"origin/{child.branch}")
    assert "feature.py" in files, "a different checkout could not read the work"

    assert wave.published_in_review[child.branch] == head
    assert wave.review_unreadable == {}
    assert [b for _, b in posts if b.get("branch_published")]


def test_the_publish_report_follows_a_real_push_and_never_precedes_it(git_repo: Path,
                                                                     tmp_path: Path):
    """The release, and only on a real push.

    `publish_pending` withholds an item from `claim_review` until the supervisor reports the
    branch published (GRPH-754). Reporting it for a branch that did not reach the remote would
    hand a reviewer the exact 404 that guard exists to prevent — so a refused push must leave
    the item withheld, and must say why.

    Sabotage: move the `post_attempt` above the `pushed.ok` test and this fails.
    """
    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A2")
    (tree.path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(tree.path, "add", "-A")
    _git(tree.path, "commit", "-qm", "work")
    subprocess.run(["git", "-C", str(git_repo), "remote", "add", "origin",
                    str(tmp_path / "nowhere.git")], check=True, capture_output=True)

    child = Child(adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
                  seat_path=tmp_path / "seat.json", process=_Live(),
                  started_at=time.monotonic(), log_dir=tmp_path / "logs",
                  seat_id="seat-1", held_items=["GRPH-961"])
    posts: list = []
    wave = Wave()

    sup._publish_in_review(wave, [child], _server(posts, ["GRPH-961"]))

    assert [b for _, b in posts if b.get("branch_published")] == [], \
        "a push that did not land reported the branch as published"
    assert wave.published_in_review == {}
    assert wave.review_unreadable[tree.branch] == ["GRPH-961"]
    assert any("push refused" in f for f in wave.failures), wave.failures


def test_an_early_push_proposes_no_pr(builder, monkeypatch):
    """A push of an unproposed branch must not imply a proposal — the item says so explicitly.

    The refusals that decide whether a branch may be proposed at all are measured at REAP:
    UNDECLARED files (GRPH-949) and a base the trunk has moved past (GRPH-786). Neither
    measurement exists yet, so proposing here would open a PR the wave has not earned a
    judgement about.

    Sabotage: call `_propose` from `_publish_in_review` and this fails.
    """
    import inspect

    child, bare, _ = builder
    calls: list[str] = []
    monkeypatch.setattr(sup, "propose_branch",
                        lambda *a, **k: calls.append("propose"))
    posts: list = []

    sup._publish_in_review(Wave(), [child], _server(posts, ["GRPH-961"]))

    assert calls == [], "an early push opened a PR"
    assert "propose_branch" not in inspect.getsource(sup._publish_in_review)
    assert child.branch in _remote_branches(bare), "the push itself must still happen"


def test_an_item_still_in_progress_is_not_published_early(builder):
    """The STATUS is the signal, not the existence of commits.

    A child commits many times while it builds. Pushing on the first one would publish
    half-work to the remote where a reviewer could be handed it — the same defect with the
    window moved earlier instead of closed.

    The review queue is deliberately NOT empty here: something else is awaiting review, so the
    thing being tested is the intersection with this child's own holdings and not the early
    return on an empty board. Sabotage caught the first version of this test — with an empty
    queue, dropping `i in in_review` from the intersection left it green, because the early
    return fired first and the assertion proved nothing.
    """
    child, bare, _ = builder
    posts: list = []

    wave = Wave()
    sup._publish_in_review(wave, [child], _server(posts, ["GRPH-999"]))

    assert _remote_branches(bare).strip() == "", "published a branch nobody had handed over"
    assert wave.published_in_review == {}
    assert wave.review_unreadable == {}
    assert [b for _, b in posts if b.get("branch_published")] == []


def test_an_item_held_by_somebody_else_is_not_this_childs_to_publish(git_repo: Path,
                                                                    tmp_path: Path):
    """Matched on the child's OWN holdings, not on the review queue as a whole.

    Two children in one wave, one item in review: publishing the other child's branch would
    push work that has not been handed over, and would post `branch_published` against the
    wrong seat — releasing an item from a branch that does not carry it.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(git_repo, "remote", "add", "origin", str(bare))

    mine = wt.create(git_repo, tmp_path / "a", wave="w", agent_id="A1")
    theirs = wt.create(git_repo, tmp_path / "b", wave="w", agent_id="A2")
    for tree in (mine, theirs):
        (tree.path / "f.py").write_text("x = 1\n", encoding="utf-8")
        _git(tree.path, "add", "-A")
        _git(tree.path, "commit", "-qm", "work")

    def _child(tree, items, seat):
        return Child(adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
                     seat_path=tmp_path / f"{seat}.json", process=_Live(),
                     started_at=time.monotonic(), log_dir=tmp_path / "logs",
                     seat_id=seat, held_items=items)

    posts: list = []
    wave = Wave()
    sup._publish_in_review(wave, [_child(mine, [], "seat-1"), _child(theirs, ["GRPH-961"], "seat-2")],
                           _server(posts, ["GRPH-961"]))

    assert wave.published_in_review == {theirs.branch: _git(theirs.path, "rev-parse", "HEAD")}
    assert mine.branch not in wave.published_in_review
    assert [b["enrolment_id"] for _, b in posts if b.get("branch_published")] == ["seat-2"]


def test_one_push_per_revision_and_another_when_the_head_moves(builder):
    """A watch tick runs every poll interval; the ledger read and the push must not.

    Guarded on the head, not on a boolean: a bound seat that commits again after its item
    reached review has to be published again, or the reviewer reads the older revision.
    """
    child, bare, head = builder
    posts: list = []
    client = _server(posts, ["GRPH-961"])
    wave = Wave()

    sup._publish_in_review(wave, [child], client)
    sup._publish_in_review(wave, [child], client)
    assert len([b for _, b in posts if b.get("branch_published")]) == 1, posts

    (child.worktree / "more.py").write_text("y = 2\n", encoding="utf-8")
    _git(child.worktree, "add", "-A")
    _git(child.worktree, "commit", "-qm", "one more")
    later = _git(child.worktree, "rev-parse", "HEAD")

    sup._publish_in_review(wave, [child], client)

    assert wave.published_in_review[child.branch] == later != head
    assert _git(bare, "rev-parse", child.branch) == later
    assert len([b for _, b in posts if b.get("branch_published")]) == 2, posts


def test_a_revision_is_put_to_the_remote_once_not_every_tick(builder, monkeypatch):
    """`gbfleet mcp` ticks every second. An unguarded publish is a `git push` subprocess per
    tick inside the loop every other check waits on — and against a remote that is refusing, it
    is a per-second network round trip that cannot change its answer.

    Sabotage: gate on `published_in_review` alone and the refused case below retries forever.
    """
    child, bare, _ = builder
    calls: list[str] = []
    real = sup.wt_mod.push_branch

    def counting(repo, branch, base):
        calls.append(branch)
        return real(repo, branch, base)

    monkeypatch.setattr(sup.wt_mod, "push_branch", counting)
    client = _server([], ["GRPH-961"])
    wave = Wave()

    for _ in range(5):
        sup._publish_in_review(wave, [child], client)

    assert calls == [child.branch], calls


def test_a_refused_push_is_reported_once(git_repo: Path, tmp_path: Path, monkeypatch):
    """The other half of the same tick rate: one refusal must not become a `git push` per tick
    against a remote that already said no, nor a page of identical FAILED lines that buries the
    real failure beside them.

    Both counts, because they are guarded by different things: the retry guard stops the push,
    `_note_once` stops the line. Sabotage either one alone and only one of these assertions
    moves.
    """
    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A4")
    (tree.path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(tree.path, "add", "-A")
    _git(tree.path, "commit", "-qm", "work")
    subprocess.run(["git", "-C", str(git_repo), "remote", "add", "origin",
                    str(tmp_path / "nowhere.git")], check=True, capture_output=True)
    child = Child(adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
                  seat_path=tmp_path / "seat.json", process=_Live(),
                  started_at=time.monotonic(), log_dir=tmp_path / "logs",
                  seat_id="seat-1", held_items=["GRPH-961"])
    client = _server([], ["GRPH-961"])
    calls: list[str] = []
    real = sup.wt_mod.push_branch

    def counting(repo, branch, base):
        calls.append(branch)
        return real(repo, branch, base)

    monkeypatch.setattr(sup.wt_mod, "push_branch", counting)
    wave = Wave()

    for _ in range(5):
        sup._publish_in_review(wave, [child], client)

    assert calls == [tree.branch], calls
    assert len([f for f in wave.failures if f.startswith(f"{tree.branch}:")]) == 1, wave.failures
    assert wave.review_unreadable[tree.branch] == ["GRPH-961"]


def test_a_child_whose_tree_cannot_be_resolved_is_reported_once(tmp_path: Path):
    """The case `_note_once` actually exists for, and the one the retry guard does NOT cover:
    the tree is resolved before there is a head to guard on, so this path is reached again on
    every tick. A worktree deleted out from under a live child is a real state — `_reap_exited`
    handles the same one — and without the dedupe it is a page of identical FAILED lines at one
    per poll interval, which is how the real failure beside them gets buried.

    Sabotage: drop the per-branch guard in `_note_once` and this fails with five lines.
    """
    child = Child(adapter="fake", worktree=tmp_path / "gone", branch="gb/gone", base="HEAD",
                  seat_path=tmp_path / "seat.json", process=_Live(),
                  started_at=time.monotonic(), log_dir=tmp_path / "logs",
                  seat_id="seat-1", held_items=["GRPH-961"])
    client = _server([], ["GRPH-961"])
    wave = Wave()

    for _ in range(5):
        sup._publish_in_review(wave, [child], client)

    assert len(wave.failures) == 1, wave.failures
    assert "could not resolve its tree" in wave.failures[0]


# ---- the absence must not read as a clean result ---------------------------------------------

def test_a_client_that_cannot_read_items_says_so_rather_than_reporting_a_quiet_board(builder):
    """`ALLOWED_TOOLS` is two reads and `search_items` is not one of them, so `up` and `mcp`
    supervisors cannot make this observation at all. That is not "nothing was awaiting review",
    and an empty `review_unreadable` reads as exactly that unless the third answer is named.

    Sabotage: return an empty set from `_ids_in_review` instead of None and this fails.
    """
    child, bare, _ = builder
    posts: list = []

    wave = Wave()
    sup._publish_in_review(wave, [child], _server(posts, [], reads_items=False))

    assert wave.review_unmeasured, "an unreadable board reported as an empty one"
    assert wave.review_unreadable == {}
    assert _remote_branches(bare).strip() == ""


def test_an_unreachable_server_is_unmeasured_too(builder):
    """Same third answer from the other side: the tool is allowed and the server did not pick
    up. A partition is not a quiet board."""
    child, bare, _ = builder

    wave = Wave()
    sup._publish_in_review(wave, [child], _server([], [], reachable=False))

    assert wave.review_unmeasured
    assert wave.review_unreadable == {}
    assert _remote_branches(bare).strip() == ""


def test_nothing_is_read_from_the_ledger_while_no_child_has_unpublished_work(git_repo: Path,
                                                                            tmp_path: Path):
    """The tick is not free and this must not make it expensive. A branch still at its base
    has nothing to publish, so a wave of freshly spawned children must not ask the ledger what
    is in review on every poll.
    """
    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A3")
    child = Child(adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
                  seat_path=tmp_path / "seat.json", process=_Live(),
                  started_at=time.monotonic(), log_dir=tmp_path / "logs",
                  seat_id="seat-1", held_items=["GRPH-961"])
    spy = _Spy(_server([], ["GRPH-961"]))

    sup._publish_in_review(Wave(), [child], spy)

    assert spy.tools == [], spy.tools


# ---- criterion 2: the attempt record distinguishes them --------------------------------------

def test_the_attempt_record_distinguishes_an_unreadable_revision_from_a_plain_exit():
    """An unreadable revision is not a verdict, but the bounce it provokes is recorded as one
    against the builder's vendor and model. The outcome is the server's and cannot be un-recorded
    from here; `exit_meaning` is this process's to describe, and it is where a later reader can
    tell the two apart.

    Sabotage: drop the `unreadable` clause and both records read the same, which is the defect.
    """
    child = Child(adapter="fake", worktree=Path("/tmp/wt"), branch="gb/p47f-1", base="",
                  seat_path=Path("/tmp/seat.json"), process=_Live(), started_at=0.0,
                  log_dir=Path("/tmp/logs"), seat_id="seat-1")

    plain = sup._exit_meaning(child, 0)
    marked = sup._exit_meaning(child, 0, unreadable=["GRPH-961"])

    assert marked != plain
    assert sup.REVIEW_UNREADABLE in marked and "GRPH-961" in marked
    assert sup.REVIEW_UNREADABLE not in plain


def test_the_marker_reaches_the_posted_attempt_row(builder, monkeypatch):
    """THE CALL. A field computed and never posted is the shape this repository keeps finding
    (GRPH-534, GRPH-247): the callee is right and nothing consumes it.
    """
    child, _, _ = builder
    posts: list = []
    client = _server(posts, ["GRPH-961"])
    wave = Wave()
    wave.review_unreadable[child.branch] = ["GRPH-961"]

    child.process = type("_Dead", (), {"poll": staticmethod(lambda: 0), "pid": 4242,
                                       "returncode": 0})()
    monkeypatch.setattr(sup.adapters, "result_facts", lambda adapter, text: {})
    sup._report_exits([child], client, wave)

    row = _exit_posts(posts)[0]
    assert sup.REVIEW_UNREADABLE in row["exit_meaning"], row
    assert "GRPH-961" in row["exit_meaning"]


def test_a_clean_publish_leaves_no_marker_on_the_record(builder, monkeypatch):
    """The control, and the other direction of the distinction: an ordinary exit must not carry
    the marker, or the field says nothing at all."""
    child, _, _ = builder
    posts: list = []
    client = _server(posts, ["GRPH-961"])

    sup._publish_in_review(Wave(), [child], client)
    child.process = type("_Dead", (), {"poll": staticmethod(lambda: 0), "pid": 4242,
                                       "returncode": 0})()
    monkeypatch.setattr(sup.adapters, "result_facts", lambda adapter, text: {})
    sup._report_exits([child], client, Wave())

    row = _exit_posts(posts)[0]
    assert sup.REVIEW_UNREADABLE not in row["exit_meaning"], row


def test_the_marker_is_truncated_to_the_column_width():
    """`exit_meaning` is VARCHAR(256) on Postgres, and `post_attempt` never raises: an over-long
    value would not fail loudly, it would silently lose the whole attempt measurement — tokens,
    wall seconds and diff shape with it. Ids are free text, so the slice has to be there.

    Sabotage: drop `[:256]` and the length is 617, not 256 — and the failure would surface in
    production as a missing row rather than as an error, because the post is fire-and-forget.
    """
    child = Child(adapter="fake", worktree=Path("/tmp/wt"), branch="gb/x", base="",
                  seat_path=Path("/tmp/seat.json"), process=_Live(), started_at=0.0,
                  log_dir=Path("/tmp/logs"), seat_id="seat-1")

    got = sup._exit_meaning(child, 0, unreadable=["GRPH-" + "x" * 200 for _ in range(3)])

    assert len(got) == 256
    assert sup.REVIEW_UNREADABLE in got, got[:80]


# ---- the ordering inside the tick -------------------------------------------------------------

def test_an_exited_child_is_left_to_the_reap(builder):
    """A child that has EXITED is `_reap_exited`'s, and the reap does the fuller job: salvage,
    touchpoint measurement, the salvage receipt, and then `_publish`. This must not race it.

    Sabotage: drop `c.running` from the `pending` filter and this fails — the branch is
    published twice and the early push is recorded as the reason the reviewer could read it,
    which is not what happened.
    """
    child, bare, head = builder
    child.process = type("_Dead", (), {"poll": staticmethod(lambda: 0), "pid": 4242,
                                       "returncode": 0})()
    posts: list = []

    wave = Wave()
    sup._publish_in_review(wave, [child], _server(posts, ["GRPH-961"]))

    assert wave.published_in_review == {}
    assert _remote_branches(bare).strip() == "", "published a child the reap owns"
    assert [b for _, b in posts if b.get("branch_published")] == []


def test_the_reap_runs_before_the_early_publish_in_a_tick():
    """THE ORDER, which is load-bearing and was found the hard way.

    `_reap_all` at the end of a wave writes the give-up line and the observe record only for
    children it reaped itself, and it skips any `_reap_exited` already took. Giving
    `_publish_in_review` the first turn in the tick put three local git calls ahead of the reap
    — enough wall time for a fast child to die mid-tick, be reaped by the wrong one of the two,
    and lose both records. Fifteen tests failed. A child that exits during a tick belongs to the
    reap, exactly as it did before this existed.

    Sabotage: move the call above `_reap_exited` in `watch_tick` and this fails.
    """
    import inspect

    source = inspect.getsource(sup.watch_tick)
    assert source.index("_reap_exited(") < source.index("_publish_in_review(")
    assert source.index("_publish_in_review(") < source.index("_report_exits(")


# ---- the operator-facing surfaces (GRPH-987, bounce 1) -----------------------------------
#
# The reviewer of PR #898 re-ran the CALL-SITE mutations this item's absence rule depends on
# and found two of them green: deleting the three keys from `Report.as_json`, and deleting the
# `REVIEW UNREADABLE` loop from `cli.report`, each failed nothing. The comment in `as_json`
# claimed the keys are "always present and empty by default, so 'no item reached review early'
# cannot be read as 'the check did not run'" — and nothing pinned it, so the claim was prose.
#
# The attempt-row call site WAS covered (`test_the_marker_reaches_the_posted_attempt_row`
# fails when `unreadable=None` is passed), which is exactly why the gap was easy to miss: one
# call site of three was proved and the conclusion was generalised to all of them.


def _report_with(wave=None):
    from gbfleet.until import Report
    return Report(ok=True, reason="idle", exit=0, wave=wave).as_json()


def test_the_three_review_keys_are_present_on_a_clean_wave():
    """Empty is "nothing of this kind happened", not "we did not look". A reader who cannot
    tell those apart has to guess, and this repo's recurring defect is that the guess is
    always the reassuring one.

    Sabotage: drop any of the three keys from `as_json` → this fails.
    """
    from gbfleet.supervisor import Wave
    payload = _report_with(Wave())

    for key in ("published_in_review", "review_unreadable", "review_unmeasured"):
        assert key in payload, f"{key} must be in the report even when nothing happened"
    assert payload["published_in_review"] == {}
    assert payload["review_unreadable"] == {}
    assert payload["review_unmeasured"] == ""


def test_the_three_review_keys_are_present_with_no_wave_at_all():
    """A report built before a wave exists must still carry them. Otherwise the keys are
    present exactly when there is something to say, which is the shape that lets a consumer
    treat `KeyError` as "clean"."""
    payload = _report_with(None)

    assert payload["published_in_review"] == {}
    assert payload["review_unreadable"] == {}
    assert payload["review_unmeasured"] == ""


def test_an_unreadable_branch_reaches_the_json_with_its_items():
    """Named, not counted: an operator deciding whether a bounce was the builder's fault needs
    the branch and the items, not a number."""
    from gbfleet.supervisor import Wave
    wave = Wave()
    wave.published_in_review = {"gb/w-1": "a" * 40}
    wave.review_unreadable = {"gb/w-2": ["GRPH-1", "GRPH-2"]}
    payload = _report_with(wave)

    assert payload["published_in_review"] == {"gb/w-1": "a" * 40}
    assert payload["review_unreadable"] == {"gb/w-2": ["GRPH-1", "GRPH-2"]}


def test_a_ledger_that_could_not_be_asked_is_its_own_answer_in_the_json():
    """The third state. "No item was in review" and "we could not ask which items were in
    review" are different facts, and collapsing them is what the whole item is about."""
    from gbfleet.supervisor import Wave
    wave = Wave()
    wave.review_unmeasured = "fleet_status refused: 401"
    payload = _report_with(wave)

    assert payload["review_unmeasured"] == "fleet_status refused: 401"
    assert payload["review_unreadable"] == {}, "unmeasured must not masquerade as measured-empty"


def test_report_prints_the_unreadable_branches_for_an_operator():
    """`cli.report` is the surface an operator actually reads. The JSON being right does not
    help someone watching the terminal.

    Sabotage: delete the `REVIEW UNREADABLE <branch>` loop from `cli.report` → this fails.
    """
    import io

    from gbfleet.cli import report
    from gbfleet.supervisor import Wave

    wave = Wave()
    wave.review_unreadable = {"gb/w-2": ["GRPH-1", "GRPH-2"]}
    out = io.StringIO()
    report(wave, out=out)
    text = out.getvalue()

    assert "REVIEW UNREADABLE gb/w-2" in text
    assert "GRPH-1" in text and "GRPH-2" in text
    # The whole point of printing it: the bounce that follows is not the builder's fault.
    assert "not the builder" in text


def test_report_prints_the_unmeasured_line_separately():
    """Sabotage: delete the `REVIEW UNREADABLE unmeasured` line → this fails."""
    import io

    from gbfleet.cli import report
    from gbfleet.supervisor import Wave

    wave = Wave()
    wave.review_unmeasured = "fleet_status refused: 401"
    out = io.StringIO()
    report(wave, out=out)

    assert "REVIEW UNREADABLE unmeasured: fleet_status refused: 401" in out.getvalue()


def test_report_says_nothing_about_review_on_a_clean_wave():
    """The other half, and the one that stops the two tests above being satisfied by a line
    that always prints. A wave with nothing unreadable must not mention it at all — a standing
    "REVIEW UNREADABLE: none" would train an operator to skip the line that matters."""
    import io

    from gbfleet.cli import report
    from gbfleet.supervisor import Wave

    out = io.StringIO()
    report(Wave(), out=out)

    assert "REVIEW UNREADABLE" not in out.getvalue()
