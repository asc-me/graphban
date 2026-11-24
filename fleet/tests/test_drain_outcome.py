from gbfleet.drain_outcome import DRAINED, STALLED, WORKING, OpenItem, classify


def test_all_done_is_drained():
    items = [OpenItem("A", status="done"), OpenItem("W", status="next", walk=True)]
    assert classify("idle", items).state == DRAINED


def test_exit_reasons_drained():
    items = [OpenItem("A")]
    assert classify("no-open-items", items).state == DRAINED
    assert classify("prd-closed", items).state == DRAINED


def test_dependency_cycle_is_stalled_and_named():
    out = classify("idle", [OpenItem("A")],
                   dependency_holds=[{"item": "A", "waiting_on": "cycle A -> B -> A"}])
    assert out.state == STALLED
    assert "cycle" in out.reasons["A"]


def test_review_with_no_reviewer_is_stalled():
    out = classify("review-unsigned", [OpenItem("A", status="review")])
    assert out.state == STALLED
    assert out.reasons["A"] == "in review, no live reviewer"


def test_review_wait_line_used():
    out = classify("review-unsigned", [OpenItem("A", status="review")],
                   review_waits=["A: review_waits_on_ci (run 1)"])
    assert out.reasons["A"].startswith("review_waits_on_ci")


def test_delegated_or_live_is_working():
    assert classify("idle", [OpenItem("A")], spawned=1).state == WORKING
    assert classify("idle", [OpenItem("A", live=True)]).state == WORKING


def test_refused_delegation_is_stalled():
    out = classify("stalled", [OpenItem("A", ready=True)],
                   delegate_refused=[{"item": "A", "reason": "no room"}])
    assert out.state == STALLED
    assert out.reasons["A"] == "delegate refused: no room"


def test_held_reason():
    out = classify("idle", [OpenItem("A")],
                   held=[{"items": ["A"], "held_by": ["SA-1"], "because": ["x.py"]}])
    assert out.reasons["A"] == "held by SA-1 (x.py)"
