"""A PR needs time to be checked before its outcome is recorded (GRPH-567, PRD-26).

> A minimum interval between linking a PR and recording its outcome, so CI has time to run.
> Kills an entire failure class — *reporting green before green existed* — for a few lines.

An attestation proves something was checked. It does not prove the check had time to happen:
a reviewer who links a PR and signs it off in the same minute records an outcome for a run
that has not finished. That is the failure here, and it is invisible afterwards — a completed
item with a green receipt looks identical whether CI ran or not.

Each test below puts the system in the state the refusal exists to catch. The two that matter
most are the single-call case (link and complete together, which is the WORST version rather
than an edge of it) and the re-link case (the cooldown must not be escapable by re-posting a
URL, which is the cheapest possible action).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.models import utcnow
from app.services import items as items_svc

PR = {"kind": "url", "url": "https://github.com/asc-me/graphban/pull/999", "detail": "PR #999"}
ATTESTATION = {
    "kind": "attestation",
    "adapter": "github-actions",
    "commit": "a" * 40,
    "predicates": [{"name": "suite_green", "passed": True, "detail": "ok"}],
}


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
    """A real, non-zero cooldown. The product default is 600s; 60 keeps the arithmetic
    obvious without changing which branch runs."""
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)
    return 60


def _item(db, **kw):
    return items_svc.create_item(db, project_id="core", title="Built it", **kw)


def _backdate(db, item, seconds):
    """Move the link into the past. Time is the thing under test, so it is controlled
    directly rather than waited for — a test that slept would be the same test, slower."""
    item.pr_linked_at = utcnow() - timedelta(seconds=seconds)
    db.commit()


# ---- the refusal, and that it clears itself -----------------------------------------------

def test_completing_an_item_whose_pr_was_just_linked_is_refused(db, cooldown):
    """THE DEFECT. The PR is linked, CI cannot have finished, and `done` is asked for."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, ATTESTATION])

    with pytest.raises(items_svc.PRCooldown) as exc:
        items_svc.update_item(db, item.id, status="done")

    assert "cooldown" in str(exc.value)


def test_the_refusal_says_how_long_is_left_rather_than_what_to_change(db, cooldown):
    """It is the one refusal here that resolves on its own. A message naming an action would
    send the caller editing a payload that is already correct."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, ATTESTATION])
    _backdate(db, item, 45)

    with pytest.raises(items_svc.PRCooldown) as exc:
        items_svc.update_item(db, item.id, status="done")

    message = str(exc.value)
    assert "Try again in 1" in message, message          # ~15s remain of 60
    assert "nothing needs changing" in message


def test_the_same_completion_succeeds_once_the_interval_has_elapsed(db, cooldown):
    """The control. Without it, a cooldown that refused FOREVER would pass the test above
    and make every item with a PR permanently uncompletable."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, ATTESTATION])
    _backdate(db, item, 61)

    out = items_svc.update_item(db, item.id, status="done")
    assert out.status == "done"


# ---- the two that carry the design --------------------------------------------------------

def test_linking_and_completing_in_one_call_is_refused(db, cooldown):
    """The WORST version of the defect, not an edge of it.

    Reading only the stored stamp would miss it: the item has no `pr_linked_at` when the gate
    runs, because the evidence in this very call has not been appended yet. A cooldown that
    could be skipped by doing both at once would protect only the careful.
    """
    item = _item(db)

    with pytest.raises(items_svc.PRCooldown):
        items_svc.update_item(db, item.id, status="done", evidence=[PR, ATTESTATION])


def test_relinking_the_pr_does_not_restart_the_clock(db, cooldown):
    """FIRST LINK WINS. Otherwise the cooldown is escaped by re-posting the same URL — and an
    agent would not need to know the cooldown existed to escape it."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, ATTESTATION])
    _backdate(db, item, 61)
    first = item.pr_linked_at

    items_svc.update_item(db, item.id, evidence=[dict(PR, detail="PR #999 (again)")])
    db.refresh(item)

    assert item.pr_linked_at == first, "re-linking moved the clock forward"
    assert items_svc.update_item(db, item.id, status="done").status == "done"


# ---- what must NOT be delayed --------------------------------------------------------------

def test_an_item_with_no_linked_pr_is_not_delayed(db, cooldown):
    """This must not become a tax on every completion in the product — only on the ones
    making a claim about a PR."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[ATTESTATION])

    assert items_svc.update_item(db, item.id, status="done").status == "done"


def test_a_non_pr_url_does_not_start_a_cooldown(db, cooldown):
    """An item may carry any number of links. Only a pull/merge request means "a change is
    proposed and something is going to check it"."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[
        {"kind": "url", "url": "https://example.com/docs/design", "detail": "design note"},
        ATTESTATION,
    ])

    assert items_svc.update_item(db, item.id, status="done").status == "done"


def test_a_zero_cooldown_disables_it(db, monkeypatch):
    """The right setting for a repository with no CI: a delay protecting nothing is friction."""
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 0, raising=False)
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, ATTESTATION])

    assert items_svc.update_item(db, item.id, status="done").status == "done"


def test_sign_off_of_a_just_linked_pr_is_refused(db, cooldown):
    """THE CALL. Every test above drives `update_item`. `fleet.sign_off` is the other
    allowed writer of `done`, and it used to set the status directly — so a reviewer
    who linked a PR and signed it off in the same minute (this file's own docstring)
    recorded green before CI existed (GRPH-567 bounce).
    """
    from app.services import fleet as fleet_svc

    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, ATTESTATION])
    item.status = "review"
    item.built_by = "builder-agent"
    db.commit()

    # Reviewed at a commit CI has NOT attested — the green receipt is for an older head.
    with pytest.raises(items_svc.PRCooldown):
        fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1", commit="b" * 40)

    db.refresh(item)
    assert item.status == "review", "sign_off still completed it inside the cooldown"


def test_sign_off_consults_the_same_cooldown_helper():
    """Wiring, not behaviour. A reimplementation that inlined a weaker check — or
    dropped the call — would satisfy a source grep for PRCooldown in a comment.
    """
    import ast
    import inspect

    from app.services import fleet as fleet_svc

    tree = ast.parse(inspect.getsource(fleet_svc.sign_off))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        if name == "refuse_if_pr_cooling_down":
            found = True
    assert found, (
        "sign_off no longer calls refuse_if_pr_cooling_down — the reviewer path "
        "the cooldown exists for would skip it again"
    )


def test_an_item_already_done_is_not_re_gated(db, cooldown):
    """Gated on the TRANSITION, like the attestation check it sits beside. Re-saving a
    completed item must not re-ask a question it already answered."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, ATTESTATION])
    _backdate(db, item, 61)
    items_svc.update_item(db, item.id, status="done")

    items_svc.update_item(db, item.id, evidence=[dict(PR, detail="relinked after done")])
    assert items_svc.update_item(db, item.id, status="done").status == "done"


# ---- CI answering ends the wait, and the first link sticks (GRPH-947) ----------------------

MERGE = "c" * 40
MERGED_PR = dict(PR, commit=MERGE)
CI_AT_MERGE = dict(ATTESTATION, commit=MERGE, run_ref="gha-run-42")


def _reviewed_at(commit):
    """A passing attestation that is NOT CI — it opens the completion gate at `commit` without
    saying anything about whether the suite ran there."""
    return {"kind": "attestation", "adapter": "fleet.sign_off", "commit": commit,
            "predicates": [{"name": "independent_review", "passed": True, "detail": "ok"}]}


def _in_review(db, item):
    item.status = "review"
    item.built_by = "builder-agent"
    db.commit()


def test_sign_off_of_a_merged_ci_attested_pr_is_not_delayed(db, cooldown):
    """THE REPORTED DEFECT (SA-573). The PR merged days ago and CI attested its merge commit;
    the reviewer links it and signs off. There is nothing left for the cooldown to wait for."""
    from app.services import fleet as fleet_svc

    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[CI_AT_MERGE])
    _in_review(db, item)

    out = fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1",
                             evidence=[MERGED_PR], commit=MERGE)
    assert out.status == "done"


def test_sign_off_matches_ci_at_the_commit_it_reviewed(db, cooldown):
    """THE CALL. Here only the reviewer's `commit` names the attested head — the PR link
    carries none — so a sign_off that stopped passing it would be refused."""
    from app.services import fleet as fleet_svc

    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[ATTESTATION])
    _in_review(db, item)

    out = fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1",
                             evidence=[PR], commit="a" * 40)
    assert out.status == "done"


def test_the_merge_commit_on_the_pr_link_is_enough_without_a_named_commit(db, cooldown):
    """`update_item` names no commit here; the PR receipt does (the fleet's merge receipt)."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[CI_AT_MERGE, MERGED_PR])

    assert items_svc.update_item(db, item.id, status="done").status == "done"


def test_an_attested_head_commit_skips_the_cooldown(db, cooldown):
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, ATTESTATION])

    out = items_svc.update_item(db, item.id, status="done", head_commit="a" * 40)
    assert out.status == "done"


def test_a_green_receipt_at_another_commit_does_not_skip(db, cooldown):
    """Bound to the commit. A pass at an older head says nothing about the one completing."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, ATTESTATION, _reviewed_at("d" * 40)])

    with pytest.raises(items_svc.PRCooldown):
        items_svc.update_item(db, item.id, status="done", head_commit="d" * 40)


def test_a_failing_suite_at_the_head_does_not_skip(db, cooldown):
    item = _item(db)
    failing = dict(ATTESTATION, predicates=[{"name": "suite_green", "passed": False}])
    items_svc.update_item(db, item.id, evidence=[PR, failing, _reviewed_at("a" * 40)])

    with pytest.raises(items_svc.PRCooldown) as exc:
        items_svc.update_item(db, item.id, status="done", head_commit="a" * 40)
    assert "did not pass suite_green" in str(exc.value)


def test_a_first_link_arriving_with_done_is_stored_so_the_retry_passes(db, cooldown):
    """Before, the refusal rolled the stamp away with it: every retry measured ~0s again and
    the item could only complete after some OTHER write stored the link."""
    item = _item(db)
    with pytest.raises(items_svc.PRCooldown):
        items_svc.update_item(db, item.id, status="done", evidence=[PR, ATTESTATION])

    db.refresh(item)
    assert item.pr_linked_at is not None, "the refusal did not keep the first link"
    _backdate(db, item, 61)
    assert items_svc.update_item(db, item.id, status="done",
                                 evidence=[PR, ATTESTATION]).status == "done"


def test_a_first_link_arriving_with_sign_off_is_stored_so_the_retry_passes(db, cooldown):
    from app.services import fleet as fleet_svc

    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[ATTESTATION])
    _in_review(db, item)
    with pytest.raises(items_svc.PRCooldown):
        fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1",
                           evidence=[PR], commit="b" * 40)

    db.refresh(item)
    assert item.pr_linked_at is not None, "sign_off's refusal did not keep the first link"
    _backdate(db, item, 61)
    out = fleet_svc.sign_off(db, item_id=item.id, agent_id="reviewer-1",
                             evidence=[PR], commit="b" * 40)
    assert out.status == "done"


def test_a_refused_retry_does_not_move_the_stored_link(db, cooldown):
    """First link wins on the refusal path too — a different URL on the retry is later."""
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[ATTESTATION])
    with pytest.raises(items_svc.PRCooldown):
        items_svc.update_item(db, item.id, status="done", evidence=[PR])
    _backdate(db, item, 30)
    db.refresh(item)
    first = item.pr_linked_at

    other = dict(PR, url="https://github.com/asc-me/graphban/pull/1000")
    with pytest.raises(items_svc.PRCooldown):
        items_svc.update_item(db, item.id, status="done", evidence=[other])
    db.refresh(item)
    assert item.pr_linked_at == first


def test_the_refusal_names_the_commit_and_the_ci_run(db, cooldown):
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[PR, dict(ATTESTATION, run_ref="gha-run-7"),
                                                 _reviewed_at("d" * 40)])

    with pytest.raises(items_svc.PRCooldown) as exc:
        items_svc.update_item(db, item.id, status="done", head_commit="d" * 40)
    message = str(exc.value)
    assert "d" * 12 in message, message
    assert "gha-run-7" in message and "a" * 12 in message, message


def test_the_refusal_says_when_no_commit_is_named(db, cooldown):
    """No commit is not the same as nothing to wait for — the message says which it is."""
    item = _item(db)
    with pytest.raises(items_svc.PRCooldown) as exc:
        items_svc.update_item(db, item.id, status="done", evidence=[PR, ATTESTATION])
    assert "no commit is named" in str(exc.value)


# ---- the detector --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://github.com/asc-me/graphban/pull/322",
    "https://gitlab.com/g/p/-/merge_requests/7",
    "https://bitbucket.org/g/p/pull-requests/12",
])
def test_pull_and_merge_requests_are_recognised(url):
    assert items_svc.is_pr_url(url) is True


@pytest.mark.parametrize("url", [
    "", "https://example.com/pulls", "https://github.com/asc-me/graphban/issues/322",
    "https://github.com/asc-me/graphban/commit/abc123",
])
def test_anything_else_is_not_a_pr(url):
    """An unrecognised forge stays OUTSIDE the gate rather than being delayed by a pattern
    that happened to match: a cooldown nobody can explain is worse than none."""
    assert items_svc.is_pr_url(url) is False


def test_the_stamp_is_taken_from_the_incoming_rows(db):
    """`pr_linked_at` is derived, so it is worth pinning that it reads the rows given to it
    and prefers an existing stamp over any of them."""
    earlier = utcnow() - timedelta(hours=3)
    assert items_svc.pr_linked_at([PR], existing=earlier) == earlier
    assert items_svc.pr_linked_at([PR]) is not None
    assert items_svc.pr_linked_at([{"kind": "note", "detail": "no url"}]) is None
