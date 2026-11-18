"""A head that already landed is not proposed again (GRPH-1709).

P86 drain, 2026-11-18. GRPH-1701 merged as #1846, squashed as b4431f2c, at head 960d8965 on
gb/p86-56. Reaping that branch then opened draft #1847 from the SAME head, and the merge loop
logged "proposed #1847; merge is attempted next time" on every later cycle. The same log shows
`gh pr create` failing on "GraphQL: API rate limit already exceeded" for GRPH-1703, which was
already done and merged as #1838 — so the calls that reopen a landed PR and the calls that
cannot get through are competing for one limit shared with every other drain on the account.
A planner closed #1847 by hand.

Two facts stop it, each read where it is knowable:

* the HEAD a proposal is about to make, against the PRs that already merged. Keyed on the
  commit because the branch-keyed lookups can miss it: `existing` is OPEN-only, and
  `existing_any` answers {} for any `gh` that did not exit zero — which is what one rate limit
  shared by every drain on the account produces.
* the item's own MERGE RECEIPT, which only the ledger holds — so the supervisor reads it, at
  reap and again in the merge pass before that pass asks GitHub anything at all.

Sabotage: drop the `merged_prs` scan from `propose` and the first test opens a PR; drop the
receipt guard from `Merger._attempt` and the merge-pass test's `gh.calls` is no longer empty.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gbfleet import propose as propose_mod
from gbfleet import supervisor as sup
from gbfleet.supervisor import Merger, Wave

from tests.test_merge_after_sign_off import (  # noqa: F401  (forge is a fixture)
    MERGE_OID, _done_item, _Forge, _Ledger, _stored, forge,
)

REPO = Path("/tmp/does-not-matter")

#: The head #1846 merged, and the head the reap then proposed again.
HEAD = "960d8965a1b2c3d4e5f60718293a4b5c6d7e8f90"
BRANCH = "gb/p86-56"
ITEM = "GRPH-1701"


class _Gh:
    """A `gh` that answers the way the incident's did — nothing for the branch, everything
    for the commit.

    #1846 is MERGED, so the OPEN-only `pr view` misses it. The branch-keyed
    `pr list --head gb/p86-56` misses it too, which is what the incident's own log shows —
    #1847 was opened for a head #1846 had landed — and what any `gh` exiting non-zero under
    the shared rate limit also reads as. Only a listing by state still shows the merge, and
    that is the listing `merged_prs` added.
    """

    def __init__(self, merged=(), create_url="https://github.com/o/r/pull/1847"):
        self.merged, self.create_url = list(merged), create_url
        self.calls: list[list[str]] = []

    def created(self) -> bool:
        return any(c[1:3] == ["pr", "create"] for c in self.calls)

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        verb = " ".join(argv[1:3])
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        if verb == "pr view":
            return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")
        if verb == "pr list":
            state = argv[argv.index("--state") + 1] if "--state" in argv else "open"
            if state == "merged" and "--head" not in argv:
                return ok(json.dumps(self.merged))
            return ok("[]")
        if verb == "pr create":
            return ok(self.create_url + "\n")
        raise AssertionError(f"unexpected gh call: {argv}")


@pytest.fixture()
def gh(monkeypatch):
    def _wire(fake):
        monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh")
        monkeypatch.setattr(propose_mod.subprocess, "run", fake)
        return fake
    return _wire


def _merged(number=1846, branch=BRANCH, head=HEAD) -> dict:
    return {"number": number, "url": f"https://github.com/o/r/pull/{number}",
            "headRefName": branch, "headRefOid": head}


def _receipt(commit=MERGE_OID, url="https://github.com/o/r/pull/1846") -> dict:
    """The row `Merger._record` writes, as the SERVER stores it."""
    return _stored([{"kind": "url", "commit": commit, "url": url,
                     "detail": f"merged by gbfleet: squash of #1846 landed as {commit[:12]}"}])[0]


# ---- the head a reap is about to propose ------------------------------------------------------

def test_a_reaped_head_that_already_merged_is_not_given_a_second_pr(gh):
    """The reported defect: #1847 opened for the head #1846 had just landed."""
    fake = gh(_Gh(merged=[_merged()]))

    got = propose_mod.propose(REPO, BRANCH, "origin/main", title="t", body="b",
                              head=HEAD, items=[ITEM])

    assert got.skipped and not got.ok
    assert got.url.endswith("/pull/1846"), got
    assert "#1846" in got.reason and "merged" in got.reason
    assert not fake.created(), "opened a second PR for an already-merged head"


def test_a_head_no_pr_merged_is_still_proposed(gh):
    """The control, and the reason the scan is keyed on a commit. A merged PR at ANOTHER head
    is a fact about other work: refusing this one would stop every first proposal and still
    pass the test above."""
    fake = gh(_Gh(merged=[_merged(number=1200, branch="gb/p86-1", head="f" * 40)]))

    got = propose_mod.propose(REPO, BRANCH, "origin/main", title="t", body="b",
                              head=HEAD, items=[ITEM])

    assert got.ok and fake.created()


def test_no_head_to_compare_still_proposes(gh):
    """`head` is what the scan compares. Without it there is nothing to ask, and the answer
    must stay the one it was — a caller that cannot read its own head still gets its PR."""
    fake = gh(_Gh(merged=[_merged()]))

    got = propose_mod.propose(REPO, BRANCH, "origin/main", title="t", body="b", items=[ITEM])

    assert got.ok and fake.created()


# ---- the receipt only the ledger holds --------------------------------------------------------

class _Ledger2:
    """Answers the item read a reap makes, and records what it posted.

    `allowed` is the credential's tool set, which is what decides whether the read is made at
    all — `SPAWN_READS` and `PLANNER_TOOLS` both carry it, the supervisor's own two do not.
    """

    allowed = frozenset({"get_item_details"})

    def __init__(self, items: dict[str, dict], allowed=None):
        self.items, self.calls, self.observations = items, [], []
        if allowed is not None:
            self.allowed = allowed

    def call(self, tool, /, **kw):
        if tool not in self.allowed:
            raise AssertionError(f"this credential may not call {tool!r}")
        self.calls.append((tool, kw))
        if tool == "get_item_details":
            return dict(self.items[kw["id"]])
        return {}

    def post_observation(self, **kw):
        self.observations.append(kw)
        return {"recorded": True}


def _done_with_receipt(status: str = "done") -> dict:
    item = _done_item(ITEM, branch=BRANCH, status=status)
    item["evidence"] = list(item["evidence"]) + [_receipt()]
    return item


@pytest.fixture()
def reaped(git_repo: Path, monkeypatch):
    """A branch carrying one item's commit, and a record of every PR the reap would open."""
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "-b", BRANCH], check=True)
    (git_repo / "a.py").write_text("the work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-qm", f"{ITEM}: the work"],
                   check=True)
    opened: list[str] = []
    monkeypatch.setattr(propose_mod, "find", lambda: "/usr/bin/gh")

    def propose(repo, branch, base, **_kw):
        opened.append(branch)
        return propose_mod.Proposed(branch=branch, url="https://github.com/o/r/pull/1847",
                                    ok=True)

    monkeypatch.setattr(propose_mod, "propose", propose)
    return opened


def test_a_reap_of_an_item_whose_merge_is_recorded_opens_no_pr(git_repo: Path, reaped):
    """The other half of the acceptance, and the receipt is the ONLY thing stopping this one:
    `propose` is faked, so nothing here consults a forge at all. A done item whose merge is
    written down has landed; its branch being pushable again is not new work."""
    ledger = _Ledger2({ITEM: _done_with_receipt()})
    wave = Wave()

    sup.propose_branch(wave, git_repo, BRANCH, [ITEM], client=ledger, base_override="main")

    assert reaped == [], "proposed a PR for an item whose merge is already recorded"
    got = wave.proposed[BRANCH]
    assert got.skipped and not got.ok
    assert ITEM in got.reason and "1846" in got.reason
    assert not ledger.observations, "wrote a pr_opened receipt for a PR it did not open"


def test_a_receipt_on_an_item_that_is_not_done_still_gets_its_pr(git_repo: Path, reaped):
    """The control on the status guard. Evidence only APPENDS, so a bounced item keeps the
    receipt from the merge that already happened — and its rework is exactly the work that
    needs a PR. Reading the receipt alone would strand it."""
    ledger = _Ledger2({ITEM: _done_with_receipt(status="next")})
    wave = Wave()

    sup.propose_branch(wave, git_repo, BRANCH, [ITEM], client=ledger, base_override="main")

    assert reaped == [BRANCH], wave.proposed
    assert wave.proposed[BRANCH].ok


def test_an_item_with_no_merge_receipt_still_gets_its_pr(git_repo: Path, reaped):
    """The control on the receipt itself: `done` alone is not a merge. GRPH-1457 exists
    because a done item with no PR is a stuck state, and this is that item."""
    ledger = _Ledger2({ITEM: _done_item(ITEM, branch=BRANCH)})
    wave = Wave()

    sup.propose_branch(wave, git_repo, BRANCH, [ITEM], client=ledger, base_override="main")

    assert reaped == [BRANCH], wave.proposed


def test_a_credential_that_may_not_read_items_proposes_without_asking(git_repo: Path, reaped):
    """The supervisor's own two reads do not include `get_item_details`. Not being able to
    ask is not an answer, so the proposal goes ahead as it did before — and the head-keyed
    check inside `propose`, which needs no ledger, is what covers that credential."""
    ledger = _Ledger2({ITEM: _done_with_receipt()}, allowed=frozenset({"fleet_status"}))
    wave = Wave()

    sup.propose_branch(wave, git_repo, BRANCH, [ITEM], client=ledger, base_override="main")

    assert reaped == [BRANCH], wave.proposed
    assert ledger.calls == [], "asked a tool this credential does not hold"


def test_the_receipt_is_the_merger_row_and_not_any_pr_row():
    """`pr_receipts` already lists every `/pull/` url an item carries, so a receipt that only
    meant "this item has a PR" would skip the merge pass for every proposed item there is."""
    opened = {"kind": "url", "detail": f"draft PR opened by gbfleet for {BRANCH}",
              "url": "https://github.com/o/r/pull/1846"}
    assert propose_mod.merge_receipt({"evidence": [opened]}) == ""
    assert propose_mod.merge_receipt({"evidence": [opened, _receipt()]}) == (
        "https://github.com/o/r/pull/1846")
    # A row the server stripped the commit from says nothing about a merge landing.
    no_commit = {"kind": "url", "detail": "merged by gbfleet: squash of #9 landed as ",
                 "url": "https://github.com/o/r/pull/9"}
    assert propose_mod.merge_receipt({"evidence": [no_commit]}) == ""
    assert propose_mod.merge_receipt({"evidence": []}) == ""


# ---- the merge pass makes no GitHub call for a finished item ----------------------------------

def test_the_merge_pass_asks_github_nothing_about_a_recorded_merge(forge):
    """GRPH-1703 was done and merged as #1838, and the pass still ran `gh pr create` for it.
    Zero gh invocations is the criterion: under a shared rate limit the call is the cost,
    whatever it returns."""
    fake = forge(_Forge(draft=True))
    ledger = _Ledger({ITEM: _done_with_receipt()})
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    landed = merger.tick(wave)

    assert landed is False
    assert fake.calls == [], f"asked GitHub about a recorded merge: {fake.calls}"
    got = wave.merged[ITEM]
    assert got.skipped and not got.ok and "already merged and recorded" in got.reason
    assert ITEM not in merger.watching, "a finished item is still being re-asked"
    assert ledger.wrote("update_item") == [], "wrote a second merge receipt"


def test_a_held_dependency_whose_merge_is_recorded_is_also_left_alone(forge):
    """The same guard on the other door into `watching`: `note_hold` adds a dependency
    bypassing candidate discovery, and a finished dependency is the one a hold names."""
    fake = forge(_Forge(draft=True))
    ledger = _Ledger({ITEM: _done_with_receipt()})
    merger = Merger(REPO, ledger, enabled=True)

    merger.note_hold([{"id": ITEM, "commits": [MERGE_OID]}])
    merger.tick(Wave())

    assert fake.calls == [], f"asked GitHub about a recorded merge: {fake.calls}"
    assert ITEM not in merger.watching


def test_a_done_item_with_no_receipt_is_still_asked(forge):
    """The control: the pass exists to finish merges, so an item with a sign-off and no
    recorded merge is still put to GitHub. A guard that skipped every done item would pass
    both tests above and never merge anything."""
    fake = forge(_Forge(draft=True))
    ledger = _Ledger({ITEM: _done_item(ITEM, branch=BRANCH)})
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    landed = merger.tick(wave)

    assert landed is True and wave.merged[ITEM].ok
    assert "pr merge" in fake.verbs()
    assert len(ledger.wrote("update_item")) == 1, "the merge was not recorded"
