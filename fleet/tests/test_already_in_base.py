"""GRPH-983 — ancestry alone is not delivery.

**Accept:** a branch whose tip is an ancestor of the base reads as delivered only with a
RECEIPT that work existed — a commit recorded for that branch, or the item's own PR. And an
attestation that names a branch is believed only about that branch.

**What went wrong, twice.**

1. *An empty branch.* A branch cut from the base and never committed to still points AT the
   base, so it is trivially an ancestor and `reaches` answers True. A child that produced
   NOTHING therefore marked its item delivered. Wave p48a left four items undelegable that
   way in one wave — GRPH-982, 983, 984 and 985, whose children lost their leases mid-read
   (GRPH-932) and exited without writing a line.

2. *A sibling PR's attestation.* CI attaches `suite_green` receipts by scanning a PR body for
   item keys, so a PR that merely MENTIONS an id — including to say "this is NOT that item" —
   leaves a merged sha on it. GRPH-955 was made permanently undelegable by two of my own PRs
   that way, and evidence only appends, so it could not be undone: the work had to be re-filed
   as GRPH-981 and the item's bounce history was lost.

Both present identically: a supervisor exiting `{"ok": true, "reason": "idle", "spawned": 0}`
with ready, unblocked work on the board. The failure looks exactly like having nothing to do,
which is why it survived several waves before anyone chased it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet.until import _already_in_base


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=True).stdout.strip()


def _commit_on(repo: Path, branch: str, name: str) -> str:
    """A branch off the current base with one real commit, left checked out on the base."""
    base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", base)
    return head


def _empty_branch(repo: Path, branch: str) -> None:
    base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "branch", branch)
    assert _git(repo, "rev-parse", branch) == _git(repo, "rev-parse", base)


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


# ---- trigger 1: the empty branch --------------------------------------------------------

def test_an_empty_branch_is_not_delivery(git_repo: Path):
    """THE ONE THAT COST FOUR ITEMS. The branch is an ancestor of the base because it IS the
    base — a child that wrote nothing left it exactly where it was cut."""
    _empty_branch(git_repo, "gb/w-1")

    assert _already_in_base({"branch": "gb/w-1"}, git_repo, _base(git_repo)) is False


def test_a_merged_branch_with_a_receipt_still_counts(git_repo: Path):
    """The control, and the reason ancestry is narrowed rather than removed: a genuinely
    delivered item must still be recognised, or every done item would be rebuilt forever. Either
    receipt does it — a recorded commit, or the item's PR."""
    base = _base(git_repo)
    head = _commit_on(git_repo, "gb/w-2", "real.txt")
    _git(git_repo, "merge", "-q", "--no-ff", "-m", "merge w-2", "gb/w-2")

    assert _already_in_base(
        {"branch": "gb/w-2", "evidence": [{"kind": "test", "commit": head}]},
        git_repo, base) is True
    assert _already_in_base(
        {"branch": "gb/w-2", "pr": "https://github.com/o/r/pull/7"}, git_repo, base) is True


def test_a_merged_branch_with_no_receipt_at_all_is_answered_false(git_repo: Path):
    """THE TRADE, stated as a test so it is a decision and not an accident. This branch really
    did merge and the answer is still False, because git cannot tell it from an empty one and
    the item carries nothing to break the tie.

    The two errors are not equal. A wrong False re-delegates work that may already be merged:
    wasteful, and the child opens an empty diff and says so. A wrong True marks the item
    delivered forever, and evidence only appends, so nothing can take it back — which is what
    happened to GRPH-955 and to four items in wave p48a. Prefer the recoverable mistake."""
    base = _base(git_repo)
    _commit_on(git_repo, "gb/w-6", "real.txt")
    _git(git_repo, "merge", "-q", "--no-ff", "-m", "merge w-6", "gb/w-6")

    assert _already_in_base({"branch": "gb/w-6"}, git_repo, base) is False


def test_a_branch_ahead_of_the_base_is_not_in_it(git_repo: Path):
    """Unmerged work is unmerged. Pinned so the new guard cannot be satisfied by answering
    True whenever a branch has commits."""
    _commit_on(git_repo, "gb/w-3", "pending.txt")

    assert _already_in_base({"branch": "gb/w-3"}, git_repo, _base(git_repo)) is False


def test_counting_the_branchs_own_commits_cannot_separate_the_two_cases(git_repo: Path):
    """WHY the fix is corroboration rather than a commit count, pinned so nobody re-derives the
    simpler idea. My first version required `rev-list base..branch > 0` and it broke the merged
    case: once a branch merges, it has no commits outside the base either, so the count is 0
    for an empty branch AND for a delivered one. Git cannot tell them apart by ancestry, which
    is exactly why the branch reading needs a second signal."""
    base = _base(git_repo)
    _empty_branch(git_repo, "gb/empty")
    _commit_on(git_repo, "gb/merged", "done.txt")
    _git(git_repo, "merge", "-q", "--no-ff", "-m", "merge merged", "gb/merged")

    ahead_empty = _git(git_repo, "rev-list", "--count", f"{base}..gb/empty")
    ahead_merged = _git(git_repo, "rev-list", "--count", f"{base}..gb/merged")

    assert ahead_empty == "0"
    assert ahead_merged == "0", "if this ever differs, a count WOULD work and this fix is moot"


# ---- trigger 2: a sibling PR's attestation ----------------------------------------------

def test_an_attested_commit_from_another_branch_is_not_this_items_delivery(git_repo: Path):
    """GRPH-955's exact shape. A merged sibling PR's sha sits on this item as a `suite_green`
    receipt because CI scanned a PR body for item keys. It proves that PR merged; it says
    nothing about this item, and evidence only appends so it can never be taken back."""
    base = _base(git_repo)
    sibling = _commit_on(git_repo, "gb/other", "sibling.txt")
    _git(git_repo, "merge", "-q", "--no-ff", "-m", "merge other", "gb/other")
    _empty_branch(git_repo, "gb/mine")

    # The predicate detail is what real CI writes, and it is the only place the ref appears.
    details = {
        "branch": "gb/mine",
        "evidence": [{
            "kind": "attestation", "commit": sibling, "adapter": "github-actions",
            "predicates": [{"name": "suite_green", "passed": True,
                            "detail": f"CI passed on gb/other at {sibling[:12]}"}],
        }],
    }

    assert _already_in_base(details, git_repo, base) is False


def test_an_attestation_naming_no_branch_is_still_believed(git_repo: Path):
    """The limit of reading 2, stated rather than left implicit. Receipts written before the ref
    was recorded name no branch, and refusing those would trade one false reading for another —
    every pre-existing attestation would stop counting at once. So a nameless attestation is
    believed; only one that NAMES a different branch is set aside."""
    base = _base(git_repo)
    mine = _commit_on(git_repo, "gb/mine-3", "m.txt")
    _git(git_repo, "merge", "-q", "--no-ff", "-m", "merge mine-3", "gb/mine-3")

    details = {"branch": "gb/mine-3", "evidence": [{"kind": "sabotage", "commit": mine}]}

    assert _already_in_base(details, git_repo, base) is True


# ---- bounce 1: a receipt has to be commit-bearing ----------------------------------------
#
# The reviewer of PR #901 probed the head with reaches=True and found the second signal was
# satisfied by anything at all:
#
#   empty branch + no evidence        -> False   (the reported defect, fixed)
#   empty branch + one `note`         -> True    (still broken)
#   empty branch + `test`, no commit  -> True    (still broken)
#
# An item accumulates notes as a matter of course, so "has evidence" is true of almost every
# item that anyone has written on. GRPH-983 itself carries two notes, so its own empty branch
# gb/p48a-1 would still have been held by the fix meant to release it.


def test_a_plain_note_is_not_evidence_that_a_revision_exists(git_repo: Path):
    """THE BOUNCE. A note is prose somebody typed; it says nothing about whether work was
    committed. Every item picks these up, which is what made the first version of this fix
    hold the very items it was written to release."""
    _empty_branch(git_repo, "gb/noted")

    details = {"branch": "gb/noted", "evidence": [{"kind": "note", "detail": "looked at it"}]}

    assert _already_in_base(details, git_repo, _base(git_repo)) is False


def test_a_test_receipt_with_no_commit_is_not_evidence_either(git_repo: Path):
    """The reviewer's third probe. `test` receipts usually carry counts, not revisions — "4582
    passed" is not a revision anyone can read."""
    _empty_branch(git_repo, "gb/tested")

    details = {"branch": "gb/tested",
               "evidence": [{"kind": "test", "detail": "4582 passed, 34 skipped"}]}

    assert _already_in_base(details, git_repo, _base(git_repo)) is False


def test_a_commit_bearing_receipt_on_this_branch_is_evidence(git_repo: Path):
    """The control: narrowing to commit-bearing receipts must not throw away the real case."""
    base = _base(git_repo)
    head = _commit_on(git_repo, "gb/real", "r.txt")
    _git(git_repo, "merge", "-q", "--no-ff", "-m", "merge real", "gb/real")

    details = {"branch": "gb/real", "evidence": [{"kind": "test", "commit": head}]}

    assert _already_in_base(details, git_repo, base) is True


# ---- bounce 2: the ref is a field, not prose ---------------------------------------------

def test_the_ref_is_read_from_the_field_not_the_prose(git_repo: Path):
    """The reviewer noted the discrimination read `CI passed on <ref> at` out of the predicate
    detail. `attest_ci` now writes the ref as a field and this reads that first.

    Why it matters more than tidiness: an unnamed receipt is the BELIEVED case, so a wording
    change in the prose would make every receipt read as unnamed and switch the guard off
    silently — the exact shape of defect this item is about."""
    base = _base(git_repo)
    sibling = _commit_on(git_repo, "gb/sib", "s.txt")
    _git(git_repo, "merge", "-q", "--no-ff", "-m", "merge sib", "gb/sib")
    _empty_branch(git_repo, "gb/mine-4")

    # No prose at all — only the field. The old regex would have read this as unnamed.
    details = {"branch": "gb/mine-4",
               "evidence": [{"kind": "attestation", "commit": sibling, "branch": "gb/sib",
                             "predicates": [{"name": "suite_green", "passed": True,
                                             "detail": "the suite passed"}]}]}

    assert _already_in_base(details, git_repo, base) is False


def test_the_prose_fallback_still_reads_older_receipts(git_repo: Path):
    """Receipts written before the field existed carry the ref only in the detail, and there
    are plenty of them on this board. Dropping the fallback would make every one of them
    unnamed, which is believed — so the guard would weaken, not break loudly."""
    base = _base(git_repo)
    sibling = _commit_on(git_repo, "gb/sib-2", "s2.txt")
    _git(git_repo, "merge", "-q", "--no-ff", "-m", "merge sib-2", "gb/sib-2")
    _empty_branch(git_repo, "gb/mine-5")

    details = {"branch": "gb/mine-5",
               "evidence": [{"kind": "attestation", "commit": sibling,
                             "predicates": [{"name": "suite_green", "passed": True,
                                             "detail": f"CI passed on gb/sib-2 at {sibling[:12]}"}]}]}

    assert _already_in_base(details, git_repo, base) is False
