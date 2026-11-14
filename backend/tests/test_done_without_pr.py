"""`done` is refused for work on a branch that has no PR (GRPH-1457, GRPH-1633).

The PR gates apply only to an item with a linked PR, so a missing PR read as "no PR needed".
GRPH-1457 closed that for a branch carrying the supervisor's salvage note; GRPH-1615 and
GRPH-1614 then went `done` from ordinary built branches with no PR and no CI run on the
signed-off commit. Absence of a PR is not a PR that needs no CI: any item whose work is on a
branch (or at a builder's commit) needs a linked PR, and the PR needs `suite_green`.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models import utcnow
from app.services import fleet as fleet_svc
from app.services import items as items_svc

CUT_FROM = "1111222233334444555566667777888899990000"
HEAD = "b350be6c" + "0" * 32
PR = {"kind": "url", "url": "https://github.com/asc-me/graphban/pull/999", "detail": "PR #999"}
DRAFT_PR = {"kind": "url", "url": "https://github.com/asc-me/graphban/pull/1699",
            "detail": "draft PR #1699"}
SALVAGE = {"kind": "note",
           "detail": "gbfleet: salvaged onto `gb/p73-118` `01ea7d0dfc2c`; not proposed as a PR"}
GREEN = {"kind": "attestation", "adapter": "github-actions", "commit": HEAD,
         "predicates": [{"name": "suite_green", "passed": True, "detail": "CI passed"}]}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def cooldown(monkeypatch):
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)


def _item(db, *, branch="gb/audit1121-3", notes=()):
    it = items_svc.create_item(db, project_id="core", title="Built")
    it.branch = branch
    it.evidence = items_svc.append_evidence(it.evidence, [
        {"kind": "note", "detail": f"{items_svc.CUT_FROM_MARKER}{CUT_FROM} (`{branch}`)"},
        *notes])
    db.commit()
    db.refresh(it)
    return it


def _in_review(db, item):
    item.status = "review"
    item.built_by = "builder-agent"
    db.commit()
    return item


# ---- GRPH-1457: the salvaged branch -------------------------------------------------------

def test_done_is_refused_for_a_salvaged_branch_with_no_pr_naming_the_branch(db, cooldown):
    item = _item(db, branch="gb/p73-118", notes=[SALVAGE])
    with pytest.raises(items_svc.NoPRForBranch) as exc:
        items_svc.refuse_if_pr_cooling_down(db, item, [])
    assert "gb/p73-118" in str(exc.value) and "no PR" in str(exc.value)


def test_a_linked_pr_lifts_the_no_pr_refusal(db, cooldown):
    """The control: the refusal is about the missing PR, not the branch."""
    item = _item(db, notes=[SALVAGE])
    with pytest.raises(items_svc.PRCooldown) as exc:
        items_svc.refuse_if_pr_cooling_down(db, item, [PR])
    assert not isinstance(exc.value, items_svc.NoPRForBranch)


# ---- GRPH-1633: an ordinary branch, signed off -------------------------------------------

def test_sign_off_of_an_ordinary_branch_with_no_pr_is_refused_naming_the_pr(db, cooldown):
    """THE DEFECT (GRPH-1615): a reviewer read the branch, signed off, and the item went
    `done` with no PR opened and no CI run on the commit."""
    item = _in_review(db, _item(db))
    with pytest.raises(items_svc.NoPRForBranch) as exc:
        fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1", commit=HEAD)
    msg = str(exc.value)
    assert "gb/audit1121-3" in msg and "no PR is linked" in msg and "suite_green" in msg
    db.refresh(item)
    assert item.status == "review"


def test_a_green_ci_run_on_the_branch_does_not_stand_in_for_the_pr(db, cooldown):
    """CI on the branch push is not a PR: nothing proposes the code to the base."""
    item = _in_review(db, _item(db, notes=[GREEN]))
    with pytest.raises(items_svc.NoPRForBranch):
        fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1", commit=HEAD)


def test_a_builder_commit_with_no_branch_recorded_is_refused(db, cooldown):
    item = _item(db, branch="")
    item.head_commit = HEAD
    db.commit()
    with pytest.raises(items_svc.NoPRForBranch) as exc:
        items_svc.refuse_if_pr_cooling_down(db, item, [])
    assert HEAD[:12] in str(exc.value)


def test_a_draft_pr_without_suite_green_at_the_head_is_refused_like_a_ready_one(db, cooldown):
    """GRPH-1614: its PR was a draft with no CI run. A draft is a linked PR and gets the
    same CI gate; past the cooldown with no `suite_green` at the head it is refused."""
    item = _in_review(db, _item(db, notes=[DRAFT_PR]))
    item.pr_linked_at = utcnow() - timedelta(seconds=61)
    db.commit()
    with pytest.raises(items_svc.PRCINotGreen) as exc:
        fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1", commit=HEAD)
    assert not isinstance(exc.value, items_svc.NoPRForBranch)
    assert "suite_green" in str(exc.value)
    db.refresh(item)
    assert item.status == "review"


def test_a_pr_recorded_as_github_url_gets_the_ci_gate_not_a_pass(db, cooldown):
    """A PR url on `github_url` used to exempt the item from the no-PR gate while the CI gate
    read only url receipts, so it skipped both."""
    item = _in_review(db, _item(db))
    item.github_url = "https://github.com/asc-me/graphban/pull/1700"
    db.commit()
    with pytest.raises(items_svc.PRCooldown) as exc:
        fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1", commit=HEAD)
    assert not isinstance(exc.value, items_svc.NoPRForBranch)


def test_an_issue_link_on_github_url_is_not_a_pr(db, cooldown):
    item = _item(db)
    item.github_url = "https://github.com/asc-me/graphban/issues/9"
    db.commit()
    with pytest.raises(items_svc.NoPRForBranch):
        items_svc.refuse_if_pr_cooling_down(db, item, [])


def test_a_linked_pr_with_green_ci_at_the_head_signs_off(db, cooldown):
    """The control for the sign-off path: PR linked and CI green at the head is `done`."""
    item = _in_review(db, _item(db, notes=[PR, GREEN]))
    out = fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1", commit=HEAD)
    assert out.status == "done"


# ---- the defined path for items with no code ---------------------------------------------

def test_an_item_with_no_code_is_not_gated_and_the_refusal_names_that_path(db, cooldown):
    """A chore with no branch and no commit has nothing for CI to run on. It passes this gate,
    and the refusal text says so, so the exemption is written down rather than silent."""
    item = items_svc.create_item(db, project_id="core", title="Chore, no code")
    assert items_svc.refuse_if_pr_cooling_down(db, item, []) == []

    coded = _item(db)
    with pytest.raises(items_svc.NoPRForBranch) as exc:
        items_svc.refuse_if_pr_cooling_down(db, coded, [])
    msg = str(exc.value)
    assert "owner close" in msg and "no branch and no head commit" in msg


def test_an_owner_close_is_the_path_for_work_that_landed_elsewhere(db, cooldown):
    item = _item(db)
    closed = {"kind": "attestation", "adapter": items_svc.OWNER_CLOSE_ADAPTER, "commit": HEAD,
              "predicates": [{"name": "owner_close", "passed": True, "detail": "closed"}]}
    assert items_svc.refuse_if_pr_cooling_down(db, item, [closed]) == []


def test_the_effective_floor_names_items_with_code_not_only_linked_prs(db):
    from app.services import rule_toggles
    floor = [p for p in rule_toggles.done_predicate_list(db, "core")
             if p["name"] == "suite_green"]
    assert floor and "linked PR" in floor[0]["when"] and "branch or commit" in floor[0]["when"]
