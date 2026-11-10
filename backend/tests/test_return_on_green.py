"""A passing `suite_green` returns a `gbfleet CI red:` bounce to review.

The fleet sends a review row to `next` when CI is red and leaves it there after the branch
goes green, so `claim_review` never sees it. The return is the server's, on the attestation
write: clear that reason, bring every sibling bounced for the same run, and leave a human
bounce where it is.
"""
from app.models import Item
from app.services.fleet import CI_RED_BOUNCE_PREFIX, REVIEW_CI_GREEN_NOTE
from tests.test_fleet_adversarial_gate import (  # noqa: F401  (fixtures + helpers)
    _ok, db, key, proj)
from tests.test_rule_reopen_on_red import _gate, _signed_off

HEAD = "abcdef0123456789abcdef0123456789abcdef01"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
RUN = "https://github.com/o/r/actions/runs/42/attempts/1"
BRANCH = "gb/test"


def _agent(client, key, label, branch=BRANCH):
    return _ok(client, key, "register_agent",
               {"branch": branch, "label": label,
                "capabilities": {"instance": label}})["agent_id"]


def _to_review(client, key, worker, title):
    """Create one item and hand it to review before another `next` row exists.

    `claim_next` takes whatever is ready, so a second item created first would be claimed
    by this worker.
    """
    _ok(client, key, "create_item", {"title": title, "status": "next", "effort": 0})
    claimed = _ok(client, key, "claim_next", {"agent_id": worker})
    item = claimed["item"]["id"]
    _ok(client, key, "update_item",
        {"id": item, "status": "review", "agent_id": worker})
    return item


def _bounce(client, key, reviewer, item, reason):
    _ok(client, key, "bounce", {"id": item, "agent_id": reviewer, "reason": reason})


def _ci_reason(commit=HEAD):
    return f"{CI_RED_BOUNCE_PREFIX} CI red at {commit[:12]} — failing check: backend"


def _post(client, gate, item, *, commit=HEAD, passed=True, branch=BRANCH, run=RUN,
          extra=None):
    receipt = {
        "kind": "attestation", "adapter": "github-actions", "commit": commit,
        "run_ref": run, "branch": branch,
        "predicates": [{"name": "suite_green", "passed": passed,
                        "detail": "CI passed" if passed else "backend failed"}],
    }
    evidence = [receipt, *(extra or [])]
    _ok(client, gate, "update_item", {"id": item, "evidence": evidence})
    return _ok(client, gate, "get_item_details", {"id": item})


def _row(db, item_id):
    row = db.get(Item, item_id) or db.query(Item).filter(Item.key == item_id).one()
    db.refresh(row)
    return row


def _green_notes(got):
    return [e for e in got["evidence"]
            if e.get("kind") == "note" and REVIEW_CI_GREEN_NOTE in (e.get("detail") or "")]


def _has_pass_at(got, commit):
    return any(
        e.get("kind") == "attestation" and str(e.get("commit") or "").startswith(commit[:7])
        and any(q.get("name") == "suite_green" and q.get("passed") is True
                for q in e.get("predicates") or [])
        for e in got["evidence"])


# ---- the return --------------------------------------------------------------------------

def test_a_passing_suite_returns_a_ci_red_bounce_to_review(client, key, auth, proj, db):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "ci red then green")
    _bounce(client, key, reviewer, item, _ci_reason())
    author = _row(db, item).built_by

    got = _post(client, _gate(client, auth, proj), item)

    assert got["status"] == "review"
    assert "bounce_reason" not in got
    assert "reserved_for" not in got
    note = _green_notes(got)
    assert len(note) == 1
    assert HEAD[:12] in note[0]["detail"]
    assert note[0]["url"] == RUN
    row = _row(db, item)
    assert row.built_by == author and author
    assert row.bounce_reason == ""
    assert row.bounce_pinned_to is None and row.bounce_pinned_until is None
    assert row.review_claimed_by is None and row.review_takes == 0
    claimed = _ok(client, key, "claim_review", {"agent_id": reviewer})
    assert claimed["claimed"] is True
    assert claimed["item"]["id"] == item


def test_a_human_bounce_stays_next_when_the_suite_passes(client, key, auth, proj):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "a real defect")
    reason = "the privacy sentence still describes the code graph"
    _bounce(client, key, reviewer, item, reason)

    got = _post(client, _gate(client, auth, proj), item)

    assert got["status"] == "next"
    assert got["bounce_reason"] == reason
    assert _green_notes(got) == []


def test_a_failing_suite_does_not_return_the_row(client, key, auth, proj):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "still red")
    _bounce(client, key, reviewer, item, _ci_reason())

    got = _post(client, _gate(client, auth, proj), item, passed=False)

    assert got["status"] == "next"
    assert got["bounce_reason"].startswith(CI_RED_BOUNCE_PREFIX)
    assert _green_notes(got) == []


def test_a_pass_and_a_fail_at_the_same_commit_do_not_return(client, key, auth, proj):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "mixed verdict")
    _bounce(client, key, reviewer, item, _ci_reason())
    failing = {
        "kind": "attestation", "adapter": "github-actions", "commit": HEAD,
        "run_ref": RUN + "-fail", "branch": BRANCH,
        "predicates": [{"name": "suite_green", "passed": False, "detail": "backend failed"}],
    }

    got = _post(client, _gate(client, auth, proj), item, extra=[failing])

    assert got["status"] == "next"
    assert got["bounce_reason"].startswith(CI_RED_BOUNCE_PREFIX)
    assert _green_notes(got) == []


def test_a_short_commit_does_not_return_the_row(client, key, auth, proj):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "abbreviated too far")
    _bounce(client, key, reviewer, item, _ci_reason())

    got = _post(client, _gate(client, auth, proj), item, commit="abc123")

    assert got["status"] == "next"
    assert _green_notes(got) == []


def test_a_seven_character_commit_returns_the_row(client, key, auth, proj):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "short but real")
    _bounce(client, key, reviewer, item, _ci_reason())

    got = _post(client, _gate(client, auth, proj), item, commit=HEAD[:7])

    assert got["status"] == "review"
    assert HEAD[:7] in _green_notes(got)[0]["detail"]


def test_a_newer_commit_on_the_branch_returns_the_row(client, key, auth, proj):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "fixed forward")
    _bounce(client, key, reviewer, item, _ci_reason(HEAD))

    got = _post(client, _gate(client, auth, proj), item, commit=OTHER)

    assert got["status"] == "review"
    assert OTHER[:12] in _green_notes(got)[0]["detail"]


def test_a_later_green_after_the_return_does_not_note_it_again(client, key, auth, proj):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "already back")
    _bounce(client, key, reviewer, item, _ci_reason())
    gate = _gate(client, auth, proj)
    _post(client, gate, item)

    again = _post(client, gate, item, commit=OTHER, run=RUN + "/2")

    assert again["status"] == "review"
    assert len(_green_notes(again)) == 1
    assert "bounce_reason" not in again


def test_an_item_in_progress_is_not_pulled_back_to_review(client, key, auth, proj):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "author is fixing")
    _bounce(client, key, reviewer, item, _ci_reason())
    claimed = _ok(client, key, "claim_next", {"agent_id": worker})
    assert claimed["item"]["id"] == item

    got = _post(client, _gate(client, auth, proj), item)

    assert got["status"] == "in_progress"
    assert got["bounce_reason"].startswith(CI_RED_BOUNCE_PREFIX)
    assert _green_notes(got) == []


def test_a_blocker_keeps_a_ci_red_row_next(client, key, auth, proj):
    worker = _agent(client, key, "w")
    reviewer = _agent(client, key, "rev")
    item = _to_review(client, key, worker, "held by a person")
    _bounce(client, key, reviewer, item, _ci_reason())
    _ok(client, key, "update_item",
        {"id": item, "agent_id": worker, "blocker": "waiting on a product answer"})

    got = _post(client, _gate(client, auth, proj), item)

    assert got["status"] == "next"
    assert got["bounce_reason"].startswith(CI_RED_BOUNCE_PREFIX)
    assert _green_notes(got) == []


def test_a_pass_on_a_signed_off_item_stays_done(client, key, auth, proj):
    item, _builder = _signed_off(client, key)

    got = _post(client, _gate(client, auth, proj), item)

    assert got["status"] == "done"
    assert _green_notes(got) == []


def test_a_row_already_in_review_does_not_gain_a_return_note(client, key, auth, proj):
    worker = _agent(client, key, "w")
    item = _to_review(client, key, worker, "still in review")

    got = _post(client, _gate(client, auth, proj), item)

    assert got["status"] == "review"
    assert "bounce_reason" not in got
    assert _green_notes(got) == []


# ---- siblings ----------------------------------------------------------------------------

def test_siblings_on_the_branch_return_together_and_a_human_bounce_does_not(
        client, key, auth, proj):
    reviewer = _agent(client, key, "rev")
    first = _to_review(client, key, _agent(client, key, "a"), "posted")
    sibling = _to_review(client, key, _agent(client, key, "b"), "same branch")
    human = _to_review(client, key, _agent(client, key, "c"), "real defect")
    elsewhere = _to_review(client, key, _agent(client, key, "d", branch="gb/other"),
                           "other branch")
    for item in (first, sibling, elsewhere):
        _bounce(client, key, reviewer, item, _ci_reason())
    _bounce(client, key, reviewer, human, "checkbox 4 is still wrong")

    _post(client, _gate(client, auth, proj), first)
    got_first = _ok(client, key, "get_item_details", {"id": first})
    got_sib = _ok(client, key, "get_item_details", {"id": sibling})
    got_human = _ok(client, key, "get_item_details", {"id": human})
    got_else = _ok(client, key, "get_item_details", {"id": elsewhere})

    assert got_first["status"] == "review"
    assert got_sib["status"] == "review"
    assert "bounce_reason" not in got_sib
    assert _has_pass_at(got_sib, HEAD)
    assert len(_green_notes(got_sib)) == 1
    assert _green_notes(got_sib)[0]["url"] == RUN
    assert got_human["status"] == "next"
    assert got_human["bounce_reason"] == "checkbox 4 is still wrong"
    assert not _has_pass_at(got_human, HEAD)
    assert got_else["status"] == "next"
    assert got_else["bounce_reason"].startswith(CI_RED_BOUNCE_PREFIX)
    assert not _has_pass_at(got_else, HEAD)


def test_an_empty_attestation_branch_returns_only_the_posted_item(client, key, auth, proj):
    reviewer = _agent(client, key, "rev")
    posted = _to_review(client, key, _agent(client, key, "a"), "posted")
    sibling = _to_review(client, key, _agent(client, key, "b"), "sibling")
    _bounce(client, key, reviewer, posted, _ci_reason())
    _bounce(client, key, reviewer, sibling, _ci_reason())

    got = _post(client, _gate(client, auth, proj), posted, branch="")
    other = _ok(client, key, "get_item_details", {"id": sibling})

    assert got["status"] == "review"
    assert other["status"] == "next"
    assert other["bounce_reason"].startswith(CI_RED_BOUNCE_PREFIX)
    assert not _has_pass_at(other, HEAD)


def test_a_receipt_for_another_branch_returns_that_branch_and_not_the_posted_row(
        client, key, auth, proj):
    reviewer = _agent(client, key, "rev")
    posted = _to_review(client, key, _agent(client, key, "a"), "posted on gb/test")
    other = _to_review(client, key, _agent(client, key, "b", branch="gb/other"),
                       "lives on gb/other")
    _bounce(client, key, reviewer, posted, _ci_reason())
    _bounce(client, key, reviewer, other, _ci_reason())

    got = _post(client, _gate(client, auth, proj), posted, branch="gb/other")
    moved = _ok(client, key, "get_item_details", {"id": other})

    assert got["status"] == "next"
    assert got["bounce_reason"].startswith(CI_RED_BOUNCE_PREFIX)
    assert _green_notes(got) == []
    assert moved["status"] == "review"
    assert _has_pass_at(moved, HEAD)
