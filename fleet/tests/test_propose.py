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
                 lands=True, url="https://github.com/o/r/pull/9"):
        self.pr = {"number": 9, "url": url, "state": state, "isDraft": draft,
                   "mergeable": mergeable, "mergeStateStatus": status, "headRefOid": head,
                   "headRefName": "gb/w-1", "isCrossRepository": fork,
                   "mergeCommit": {"oid": MERGE_OID} if state == "MERGED" else None}
        self.has_pr, self.merge_ok, self.ready_ok, self.lands = has_pr, merge_ok, ready_ok, lands
        self.calls: list[list[str]] = []

    def verbs(self) -> list[str]:
        return [" ".join(c[1:3]) for c in self.calls]

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        verb = " ".join(argv[1:3])
        if verb == "pr view":
            if not self.has_pr:
                return subprocess.CompletedProcess(argv, 1, "", "no pull requests found for branch")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.pr), "")
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
