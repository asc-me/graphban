"""Reaped work is proposed for merge (GRPH-804).

The other half of "done does not mean merged". GRPH-798 made the supervisor HOLD an item whose
dependency's commit is not in the base; nothing made that hold clear, because nothing in the
loop ever proposed the merge.

Reported from super-arc: SA-417 reached `done` with its commit only on `origin/gb/p11-m1-4`,
while SA-416 — which only reached `review` — did have a PR. The more complete item is the one
that went missing.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gbfleet import propose as propose_mod


class _Gh:
    """A recorded `gh`. Answers `pr view` and `pr create` the way the real one does."""

    def __init__(self, *, has_pr="", create_ok=True, url="https://github.com/o/r/pull/7",
                 listed=None, reopen_ok=True):
        self.has_pr, self.create_ok, self.url = has_pr, create_ok, url
        self.listed = list(listed or [])
        self.reopen_ok = reopen_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd=None, capture_output=True, text=True, timeout=None):
        self.calls.append(argv)
        verb = " ".join(argv[1:3])
        if verb == "pr view":
            if not self.has_pr:
                return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")
            return subprocess.CompletedProcess(argv, 0, f'{{"url": "{self.has_pr}"}}', "")
        if verb == "pr list":
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(self.listed), "")
        if verb == "pr reopen":
            if not self.reopen_ok:
                return subprocess.CompletedProcess(argv, 1, "", "failed to reopen")
            return subprocess.CompletedProcess(argv, 0, "reopened\n", "")
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


def test_a_closed_pr_is_reopened_rather_than_given_a_sibling(gh):
    """GRPH-1031. `gh pr view` only finds OPEN PRs, so a bounced then closed PR
    used to get a second one. Sabotage: skip existing_any — this creates /pull/7."""
    fake = gh(_Gh(listed=[{
        "url": "https://github.com/o/r/pull/3", "state": "CLOSED", "number": 3,
    }]))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert got.ok and got.url.endswith("/pull/3")
    assert "reopened existing PR #3" in got.reason
    assert any(c[1:3] == ["pr", "reopen"] for c in fake.calls)
    assert not any(c[1:3] == ["pr", "create"] for c in fake.calls), "opened a sibling"


def test_a_closed_pr_that_will_not_reopen_is_named_not_duplicated(gh):
    fake = gh(_Gh(listed=[{
        "url": "https://github.com/o/r/pull/3", "state": "CLOSED", "number": 3,
    }], reopen_ok=False))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert got.skipped and got.url.endswith("/pull/3")
    assert "closed PR #3" in got.reason
    assert not any(c[1:3] == ["pr", "create"] for c in fake.calls), "opened a sibling"


def test_a_merged_pr_is_not_given_a_sibling(gh):
    fake = gh(_Gh(listed=[{
        "url": "https://github.com/o/r/pull/3", "state": "MERGED", "number": 3,
    }]))

    got = propose_mod.propose(REPO, "gb/w-1", "origin/main", title="t", body="b")

    assert got.skipped and "already merged as PR #3" in got.reason
    assert not any(c[1:3] == ["pr", "create"] for c in fake.calls), "opened a sibling"


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
    title, body = propose_mod.describe("gb/w-1", ["SA-417"])

    assert "SA-417" in title and "gb/w-1" in title
    assert "SA-417" in body
    assert "draft" in body.lower()
    assert "knows nothing" in body, "did not warn that the description is generated"


def test_a_branch_with_no_items_still_gets_a_usable_title():
    title, _ = propose_mod.describe("gb/w-9", [])

    assert "gb/w-9" in title


# ---- the worker's own subject leads (GRPH-817) ------------------------------------------------

def test_the_commit_subject_leads_with_the_item_prefixed():
    """Reported from a real wave: 2 of 5 PRs read `SA-415 (from gb/p11d-2)` while the other 3
    used the commit subject — two openers, two styles. The worker knows what it did; the
    supervisor does not."""
    title, _ = propose_mod.describe("gb/p11d-2", ["SA-415"], "Add DeviceToken rotation")

    assert title == "SA-415: Add DeviceToken rotation"


def test_no_subject_falls_back_rather_than_titling_nothing():
    title, _ = propose_mod.describe("gb/p11d-2", ["SA-415"], "")

    assert "SA-415" in title and "gb/p11d-2" in title


def test_no_items_still_uses_the_subject():
    title, _ = propose_mod.describe("gb/p11d-2", [], "Add DeviceToken rotation")

    assert title == "Add DeviceToken rotation"


def test_the_branch_moves_to_the_body_where_it_belongs():
    """A branch name in front of a reviewer is where a sentence about the work should be."""
    title, body = propose_mod.describe("gb/p11d-2", ["SA-415"], "Add DeviceToken rotation")

    assert "gb/p11d-2" not in title
    assert "gb/p11d-2" in body


# ---- finishing the merge after sign-off (GRPH-846) --------------------------------------------
#
# `done` is a ledger state, not a git state. These pin what the supervisor checks BEFORE it
# touches a PR, and that every miss reads as the miss it is rather than as "merged".

REVIEWED = "a" * 40
MOVED = "b" * 40
MERGE_OID = "c" * 40


class _Forge:
    """A recorded `gh` that holds one PR's state and moves it the way GitHub does.

    `pr ready` clears the draft flag; `pr merge --auto` on a CLEAN PR merges at once (that is
    what `gh` does) unless `lands=False`, in which case it only arms auto-merge.
    """

    def __init__(self, *, state="OPEN", draft=True, head=REVIEWED, mergeable="MERGEABLE",
                 status="CLEAN", fork=False, has_pr=True, merge_ok=True, ready_ok=True,
                 lands=True, url="https://github.com/o/r/pull/9", listed=None):
        self.pr = {"number": 9, "url": url, "state": state, "isDraft": draft,
                   "mergeable": mergeable, "mergeStateStatus": status, "headRefOid": head,
                   "headRefName": "gb/w-1", "isCrossRepository": fork,
                   "mergeCommit": {"oid": MERGE_OID} if state == "MERGED" else None}
        self.has_pr, self.merge_ok, self.ready_ok, self.lands = has_pr, merge_ok, ready_ok, lands
        self.listed = list(listed or [])
        self.calls: list[list[str]] = []

    def verbs(self) -> list[str]:
        return [" ".join(c[1:3]) for c in self.calls]

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        verb = " ".join(argv[1:3])
        if verb == "pr view":
            selector = argv[3] if len(argv) > 3 else ""
            # A URL or number is a selector `pr view` can resolve in any state; a branch
            # is OPEN-only (GRPH-1032). Tests that set has_pr=False still miss the branch.
            if (not self.has_pr and not selector.startswith("http")
                    and "/pull/" not in selector and not selector.lstrip("#").isdigit()):
                return subprocess.CompletedProcess(argv, 1, "", "no pull requests found for branch")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.pr), "")
        if verb == "pr list":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.listed), "")
        if verb == "pr ready":
            if not self.ready_ok:
                return subprocess.CompletedProcess(argv, 1, "", "could not mark ready")
            self.pr["isDraft"] = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "pr merge":
            if not self.merge_ok:
                return subprocess.CompletedProcess(argv, 1, "", "GraphQL: Base branch was modified")
            if self.lands:
                self.pr["state"] = "MERGED"
                self.pr["mergeCommit"] = {"oid": MERGE_OID}
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected gh call: {argv}")


def _only_gh(fake):
    """`propose.subprocess` is the subprocess module; a patch there is global, including
    `worktree.paths_unchanged`. Let git through."""
    real = subprocess.run

    def run(argv, **kw):
        return fake(argv, **kw) if argv and argv[0] == "gh" else real(argv, **kw)
    return run


def _merge(forge, gh, *, reviewed=REVIEWED, green=None, selector="gb/w-1", installed=True):
    gh(forge, installed=installed)
    return propose_mod.merge(REPO, "SA-417", selector, reviewed=reviewed,
                             green={REVIEWED} if green is None else green)


def test_a_signed_off_pr_is_made_ready_and_merged(gh):
    forge = _Forge(draft=True)

    got = _merge(forge, gh)

    assert got.ok and got.commit == MERGE_OID and got.url.endswith("/pull/9")
    assert forge.verbs() == ["pr view", "pr ready", "pr view", "pr merge", "pr view"]
    merge_argv = forge.calls[3]
    assert "--squash" in merge_argv and "--auto" in merge_argv
    assert "head=reviewed" in got.checked and "ci=suite_green" in got.checked


def test_a_pr_that_is_already_ready_is_not_marked_again(gh):
    forge = _Forge(draft=False)

    got = _merge(forge, gh)

    assert got.ok
    assert "pr ready" not in forge.verbs()


# ---- the check this exists for ---------------------------------------------------------------

def test_a_head_that_moved_after_review_is_left_alone(gh):
    """THE CASE. Same branch name, different head: somebody pushed after sign-off. The
    attestation vouches for `REVIEWED`; the PR would merge `MOVED`. Nothing may touch it.

    Sabotage: compare `headRefName` to the branch instead of `headRefOid` to the commit,
    and this passes straight through to `pr merge`."""
    forge = _Forge(draft=True, head=MOVED)

    got = _merge(forge, gh)

    assert not got.ok and not got.skipped and not got.pending
    assert "reviewed commit" in got.reason and REVIEWED[:12] in got.reason
    assert forge.verbs() == ["pr view"], "touched the PR after the head check failed"
    assert "head=reviewed" not in got.checked


def test_a_main_only_head_move_still_merges(gh, git_repo: Path):
    """GRPH-1032. Merging main into a signed branch moves the head; the item's files do
    not. That is not a new review.

    Sabotage: skip the paths_unchanged branch — this stays the refusal above.
    """
    (git_repo / "item.py").write_text("mine\n", encoding="utf-8")
    subprocess.run(["git", "add", "item.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "item"], cwd=git_repo, check=True)
    reviewed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo,
                              capture_output=True, text=True, check=True).stdout.strip()
    (git_repo / "other.py").write_text("from-main\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "main moved"], cwd=git_repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo,
                          capture_output=True, text=True, check=True).stdout.strip()

    forge = _Forge(draft=False, head=head)
    gh(_only_gh(forge))
    got = propose_mod.merge(git_repo, "SA-417", "gb/w-1", reviewed=reviewed,
                            green={reviewed}, paths=["item.py"])

    assert got.ok and got.commit == MERGE_OID
    assert "paths-unchanged" in got.checked
    assert "pr merge" in forge.verbs()


def test_a_real_edit_of_the_item_still_needs_review(gh, git_repo: Path):
    """The control. A main-merge that also edited the item is a new review."""
    (git_repo / "item.py").write_text("mine\n", encoding="utf-8")
    subprocess.run(["git", "add", "item.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "item"], cwd=git_repo, check=True)
    reviewed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo,
                              capture_output=True, text=True, check=True).stdout.strip()
    (git_repo / "item.py").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "item.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "edited the item"], cwd=git_repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo,
                          capture_output=True, text=True, check=True).stdout.strip()

    forge = _Forge(draft=False, head=head)
    gh(_only_gh(forge))
    got = propose_mod.merge(git_repo, "SA-417", "gb/w-1", reviewed=reviewed,
                            green={reviewed}, paths=["item.py"])

    assert not got.ok and "reviewed commit" in got.reason
    assert "pr merge" not in forge.verbs()


def test_a_merged_pr_is_found_when_view_of_the_branch_misses(gh):
    """GRPH-1032. `gh pr view <branch>` is OPEN-only. After GitHub's default merge the
    subject is `Merge pull request #…`, not `{TAG}-{N}:`, and view of the branch 404s.
    `pr list --state all` still finds it.

    Sabotage: skip `_url_for_ref` — this returns 'no PR' and a dependant stays held.
    """
    url = "https://github.com/o/r/pull/9"
    forge = _Forge(state="MERGED", has_pr=False, url=url, listed=[
        {"url": url, "state": "MERGED", "number": 9},
    ])

    got = _merge(forge, gh)

    assert got.ok and got.commit == MERGE_OID
    assert "state=MERGED" in got.checked
    assert "pr list" in forge.verbs()


def test_no_ci_attestation_on_the_reviewed_commit_holds_it(gh):
    """CI green on some OTHER commit is not CI green on this one."""
    forge = _Forge()

    got = _merge(forge, gh, green={MOVED})

    assert not got.ok and not got.skipped
    assert "suite_green" in got.reason
    assert forge.verbs() == ["pr view"]


def test_no_sign_off_commit_means_nothing_to_compare(gh):
    forge = _Forge()

    got = _merge(forge, gh, reviewed="")

    assert not got.ok and not got.skipped
    assert "fleet.sign_off" in got.reason
    assert forge.calls == [], "asked the forge with nothing to compare against"


# ---- skipped is not ok ------------------------------------------------------------------------

def test_branch_protection_is_skipped_not_ok(gh):
    """The backstop working. Reported as such, and never gone around."""
    forge = _Forge(draft=False, status="BLOCKED")

    got = _merge(forge, gh)

    assert got.skipped and not got.ok
    assert "branch protection" in got.reason
    assert "pr merge" not in forge.verbs()


def test_a_fork_is_skipped(gh):
    forge = _Forge(fork=True)

    got = _merge(forge, gh)

    assert got.skipped and "fork" in got.reason
    assert forge.verbs() == ["pr view"]


def test_no_pr_is_skipped(gh):
    forge = _Forge(has_pr=False)

    got = _merge(forge, gh)

    assert got.skipped and "no PR" in got.reason


def test_no_gh_is_skipped_without_asking(gh):
    forge = _Forge()

    got = _merge(forge, gh, installed=False)

    assert got.skipped and "gh is not installed" in got.reason
    assert forge.calls == []


def test_a_refused_merge_is_skipped_with_the_forge_reason(gh):
    forge = _Forge(draft=False, merge_ok=False)

    got = _merge(forge, gh)

    assert got.skipped and not got.ok
    assert "gh pr merge failed" in got.reason and "Base branch was modified" in got.reason


def test_not_clean_is_reported_with_the_status_named(gh):
    """UNKNOWN is what the forge says for a moment after a push; DIRTY is a conflict. Both are
    "not yet", named, and neither is a merge."""
    forge = _Forge(draft=False, status="UNKNOWN")

    got = _merge(forge, gh)

    assert not got.ok and not got.skipped
    assert "UNKNOWN" in got.reason


# ---- the two endings that are not "merged now" -------------------------------------------------

def test_an_already_merged_pr_reports_its_merge_commit(gh):
    """Merged by a person, or by the auto-merge armed on an earlier tick. The trunk has the
    work; the ledger should say so."""
    forge = _Forge(state="MERGED")

    got = _merge(forge, gh)

    assert got.ok and got.commit == MERGE_OID
    assert forge.verbs() == ["pr view"]


def test_auto_merge_that_has_not_landed_is_pending_not_ok(gh):
    forge = _Forge(draft=False, lands=False)

    got = _merge(forge, gh)

    assert got.pending and not got.ok and not got.final
    assert "auto-merge" in got.reason


# ---- what is read from the item ------------------------------------------------------------------

def _att(adapter, commit, *preds):
    return {"kind": "attestation", "adapter": adapter, "commit": commit,
            "predicates": [{"name": n, "passed": p} for n, p in preds]}


def test_the_reviewed_commit_is_the_last_sign_off():
    """Evidence appends. A bounced and re-signed item carries both; the standing review is
    the last one."""
    item = {"evidence": [_att("fleet.sign_off", MOVED, ("independent_review", True)),
                         _att("github-actions", REVIEWED, ("suite_green", True)),
                         _att("fleet.sign_off", REVIEWED, ("independent_review", True))]}

    assert propose_mod.reviewed_commit(item) == REVIEWED


def test_a_ci_attestation_is_not_a_review():
    item = {"evidence": [_att("github-actions", REVIEWED, ("suite_green", True))]}

    assert propose_mod.reviewed_commit(item) == ""


def test_green_commits_ignore_a_receipt_with_a_failing_predicate():
    """Same rule as the server's `valid_attestations`: one failing predicate voids the
    receipt. This reader has no server to ask and must not be laxer than the gate."""
    item = {"evidence": [_att("github-actions", REVIEWED, ("suite_green", True), ("lint", False)),
                         _att("github-actions", MOVED, ("suite_green", True)),
                         _att("fleet.sign_off", MOVED, ("independent_review", True))]}

    assert propose_mod.green_commits(item) == {MOVED}


def test_the_pr_selector_prefers_the_item_pr_then_the_receipt_then_the_branch():
    receipt = {"kind": "url", "url": "https://github.com/o/r/pull/3", "detail": "draft PR"}
    assert propose_mod.pr_selector({"pr": "https://github.com/o/r/pull/7",
                                    "evidence": [receipt], "branch": "gb/w"}).endswith("/7")
    assert propose_mod.pr_selector({"pr": None, "evidence": [receipt], "branch": "gb/w"}).endswith("/3")
    assert propose_mod.pr_selector({"pr": "", "evidence": [], "branch": "gb/w"}) == "gb/w"
    assert propose_mod.pr_selector({}) == ""


# --- GRPH-869: the receipt must land on the item ----------------------------------------


class _RecordingClient:
    """A client that records `update_item` calls. Stands in for the planner."""

    def __init__(self, refuse=False):
        self.refuse = refuse
        self.calls: list[dict] = []

    def call(self, tool, **kwargs):
        if tool == "update_item":
            if self.refuse:
                from gbfleet.client import NotPermitted
                raise NotPermitted("update_item not allowed")
            self.calls.append(kwargs)
            return {"id": kwargs.get("id")}
        return {}


def test_propose_branch_does_not_open_a_pr_for_a_salvage_subject(
    gh, git_repo, monkeypatch
):
    """GRPH-926: a salvage HEAD is a commit, not a draft PR.

    Sabotage: delete the `is_salvage_subject` return in `propose_branch` and `gh pr create`
    runs with title `WIP: salvaged by gbfleet`.
    """
    from gbfleet.supervisor import Wave, propose_branch
    from gbfleet import propose as propose_mod

    proposed = []
    monkeypatch.setattr(
        propose_mod, "subject",
        lambda *a, **kw: "WIP: salvaged by gbfleet (cursor-agent) items=GRPH-1")
    monkeypatch.setattr(
        propose_mod, "propose",
        lambda *a, **kw: proposed.append("create") or propose_mod.Proposed(
            branch="gb/w-1", ok=True, url="https://github.com/o/r/pull/99"))

    wave = Wave()
    client = _RecordingClient()
    propose_branch(wave, git_repo, "gb/w-1", ["GRPH-1"], client=client)
    assert proposed == [], "gh pr create ran for a salvage subject"
    assert wave.proposed["gb/w-1"].skipped
    assert client.calls, "item was not named"
    ev = client.calls[0]["evidence"][0]
    assert ev["kind"] == "note" and "not proposed as a PR" in ev["detail"]
    assert "/pull/" not in ev.get("url", "") + ev.get("detail", "")


def test_propose_branch_records_the_pr_url_on_the_item(gh, git_repo, monkeypatch):
    """A successful draft PR produces a `url` receipt on the item via `update_item`.

    The CALL is `client.call("update_item", id=..., evidence=[{url: ...}])`. Sabotage:
    pass `client=None` → no receipt lands. Pass a client that raises `NotPermitted` →
    the failure is recorded in `wave.failures`.
    """
    from gbfleet.supervisor import Wave, propose_branch
    from gbfleet import propose as propose_mod

    proposed = propose_mod.Proposed(branch="gb/wave-1", ok=True, url="https://github.com/o/r/pull/42")
    monkeypatch.setattr(propose_mod, "subject", lambda *a, **kw: "title")
    monkeypatch.setattr(propose_mod, "describe", lambda *a, **kw: ("title", "body"))
    monkeypatch.setattr(propose_mod, "propose", lambda *a, **kw: proposed)

    wave = Wave()
    client = _RecordingClient()
    propose_branch(wave, git_repo, "gb/wave-1", ["GRPH-1"], client=client)
    assert len(client.calls) == 1, f"expected one update_item call, got {client.calls}"
    assert client.calls[0]["id"] == "GRPH-1"
    evidence = client.calls[0]["evidence"]
    assert any(e.get("kind") == "url" and "/pull/42" in e.get("url", "") for e in evidence)


def test_propose_branch_with_no_client_opens_the_pr_but_records_nothing(gh, git_repo, monkeypatch):
    """`client=None` is the _reap_all bug: the PR opens, the receipt never lands.

    This pins the CALL — if `_reap_all` passes `client=None` to `_publish`, this test
    documents the consequence: the item has no URL receipt and a reviewer looking at
    the ledger sees nothing.
    """
    from gbfleet.supervisor import Wave, propose_branch
    from gbfleet import propose as propose_mod

    proposed = propose_mod.Proposed(branch="gb/w-1", ok=True, url="https://github.com/o/r/pull/42")
    monkeypatch.setattr(propose_mod, "subject", lambda *a, **kw: "title")
    monkeypatch.setattr(propose_mod, "describe", lambda *a, **kw: ("title", "body"))
    monkeypatch.setattr(propose_mod, "propose", lambda *a, **kw: proposed)

    wave = Wave()
    propose_branch(wave, git_repo, "gb/w-1", ["GRPH-1"], client=None)
    # PR was proposed (it's in wave.proposed), but no client call was made.
    assert "gb/w-1" in wave.proposed


def test_propose_branch_records_a_refused_client_as_a_failure(gh, git_repo, monkeypatch):
    """A client that raises NotPermitted records the failure, does not break the wave."""
    from gbfleet.supervisor import Wave, propose_branch
    from gbfleet import propose as propose_mod

    proposed = propose_mod.Proposed(branch="gb/w-1", ok=True, url="https://github.com/o/r/pull/42")
    monkeypatch.setattr(propose_mod, "subject", lambda *a, **kw: "title")
    monkeypatch.setattr(propose_mod, "describe", lambda *a, **kw: ("title", "body"))
    monkeypatch.setattr(propose_mod, "propose", lambda *a, **kw: proposed)

    wave = Wave()
    client = _RecordingClient(refuse=True)
    propose_branch(wave, git_repo, "gb/w-1", ["GRPH-1"], client=client)
    assert wave.failures, "a refused write should be recorded"
    assert any("not recorded" in f for f in wave.failures), wave.failures


# ---- an abbreviated sign-off names the same commit (2026-09-23) --------------------------------

def test_an_abbreviated_reviewed_commit_that_prefixes_the_head_merges(gh):
    """A reviewer writes `git rev-parse --short`; the forge reports 40 characters. The first
    real wave under `--merge` was refused on exactly this: "PR head d565a6835398 is not the
    reviewed commit d565a683". Same commit. CI attests the full id, so that side must match
    the abbreviation too.

    Sabotage: put `!=` back; this fails with the sentence above."""
    forge = _Forge(draft=True)

    got = _merge(forge, gh, reviewed=REVIEWED[:8], green={REVIEWED})

    assert got.ok, got.reason
    assert "head=reviewed" in got.checked


def test_an_abbreviation_of_a_different_commit_is_still_refused(gh):
    forge = _Forge(draft=True, head=MOVED)

    got = _merge(forge, gh, reviewed=REVIEWED[:8])

    assert not got.ok and "reviewed commit" in got.reason
    assert forge.verbs() == ["pr view"]


def test_a_fragment_too_short_to_name_a_commit_never_matches(gh):
    """`same_commit` must not become "starts with the same letter". Below git's own
    abbreviation floor a prefix names many commits, and a check that exists to refuse the
    wrong commit cannot accept it."""
    forge = _Forge(draft=True)

    got = _merge(forge, gh, reviewed=REVIEWED[:5])

    assert not got.ok and "reviewed commit" in got.reason
    assert forge.verbs() == ["pr view"]
    assert not propose_mod.same_commit("", REVIEWED)
    assert not propose_mod.same_commit(REVIEWED, "")
    assert not propose_mod.same_commit("zzzzzzzz", "zzzzzzzz")  # not hex, not a commit
