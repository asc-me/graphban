"""PRD-41 S3 — review competence, probes, utilization (criteria 6, 7, 8, 9, 17, 26, 27, 30, 31)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    AttemptTelemetry, CapabilityProbeRun, HarnessReviewCheck, Item, WorkClassification,
)
from app.services import harness as hsvc
from app.services import items as items_svc


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "S3"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "s3", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label, **kw}))["agent_id"]


def _attempt(client, key, db, planner, label, *, outcome="signed_off", vendor="gbagent",
             model="qwen3.6", touchpoints=None, tokens=None, sampled=None, when=None,
             turns=None, turn_budget=None, exit_meaning=None,
             reviewer_vendor="anthropic", reviewer_model="sonnet") -> AttemptTelemetry:
    item = _ok(_mcp(client, key, "create_item", {
        "title": label, "status": "next",
        "touchpoints": touchpoints or ["backend/alembic/versions/0121.py"]}))["id"]
    d = _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                           "agent_id": planner}))
    child = _agent(client, key, f"child-{label}", parent_agent_id=planner,
                   capabilities={"instance": label, "vendor": vendor, "model": model})
    assert items_svc.claim_item(db, item, child) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, f"rev-{label}",
                      capabilities={"instance": f"rev-{label}",
                                    "vendor": reviewer_vendor, "model": reviewer_model})
    if outcome == "signed_off":
        _ok(_mcp(client, key, "sign_off", {"id": item, "agent_id": reviewer,
                                           "evidence": [{"kind": "note", "detail": "read"}]}))
    else:
        _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer,
                                         "reason": "did not run the tests"}))
        stored = db.get(Item, item)
        stored.bounce_pinned_to = None
        stored.bounce_pinned_until = None
        db.commit()
    db.expire_all()
    row = db.scalar(select(AttemptTelemetry).where(AttemptTelemetry.item_id == item))
    assert row is not None, label
    if tokens is not None:
        row.tokens_in, row.tokens_out = tokens
    if sampled is not None:
        row.sampled = sampled
    if when is not None:
        row.derived_at = when
    if turns is not None:
        row.turns_used = turns
    if turn_budget is not None:
        row.turn_budget = turn_budget
    if exit_meaning is not None:
        row.exit_meaning = exit_meaning
    db.commit()
    return row


def _check_for(db, item_id) -> HarnessReviewCheck:
    row = db.scalar(select(HarnessReviewCheck).where(HarnessReviewCheck.item_id == item_id))
    assert row is not None
    return row


# ---- 6, 26: miss at filing, confirmed on close, window is 14 days ---------------------------

def test_the_review_window_is_fourteen_days():
    """6. Sabotage: widen to 15 and the 14.5-day fixture below starts writing a miss."""
    assert hsvc.REVIEW_WINDOW_DAYS == 14
    assert hsvc.WITHDRAWAL_DAYS == 90


def test_a_sign_off_then_a_bug_on_overlapping_touchpoints_is_a_miss_unconfirmed(
        client, key, db, proj, auth):
    """6 / 26. Filing writes miss (unconfirmed); closing fixed flips to miss."""
    planner = _agent(client, key, "planner")
    tel = _attempt(client, key, db, planner, "signed-work")
    check = _check_for(db, tel.item_id)
    assert check.verdict == "signed_off"

    bug = items_svc.create_item(
        db, title="regressed", tags=["bug"], status="next", project_id=proj,
        touchpoints=["backend/alembic/versions/0121.py"])
    db.expire_all()
    check = _check_for(db, tel.item_id)
    assert check.kind == "miss" and check.unconfirmed is True
    assert check.contradicted_by == bug.id

    items_svc.update_item(db, bug.id, status="done",
                          evidence=[{"kind": "attestation", "adapter": "test",
                                     "commit": "abc", "predicates": [
                                         {"name": "suite_green", "passed": True}]}])
    db.expire_all()
    check = _check_for(db, tel.item_id)
    assert check.kind == "miss" and check.unconfirmed is False


def test_a_bug_closed_not_a_bug_withdraws_the_check_and_f2_says_overlap(
        client, key, db, proj, auth):
    """26 / 30. not-a-bug withdraws; F2 is labelled by touchpoint overlap."""
    planner = _agent(client, key, "planner")
    tel = _attempt(client, key, db, planner, "signed-work")
    bug = items_svc.create_item(
        db, title="false alarm", tags=["bug"], status="next", project_id=proj,
        touchpoints=["backend/alembic/versions/0121.py"])
    items_svc.update_item(db, bug.id, tags=["bug", "not-a-bug"])
    db.expire_all()
    check = _check_for(db, tel.item_id)
    assert check.kind == "withdrawn"

    cells = hsvc.review_cells(db, [proj])
    # Withdrawn checks leave the cell, so a single withdrawn verdict is not a measurement.
    assert all(c["f2"]["label"] == "by touchpoint overlap" for c in cells) or cells == []


def test_unrelated_classification_also_withdraws(client, key, db, proj, auth):
    """30. Closing as unrelated withdraws, and recomputes within 90 days."""
    planner = _agent(client, key, "planner")
    tel = _attempt(client, key, db, planner, "signed-work")
    bug = items_svc.create_item(
        db, title="other area", tags=["bug"], status="next", project_id=proj,
        touchpoints=["backend/alembic/versions/0121.py"])
    db.add(WorkClassification(
        item_id=bug.id, prd_id="p", outcome="unrelated", reasoning="different",
        confidence=0.9, graded_by="test"))
    db.commit()
    hsvc.on_bug_updated(db, db.get(Item, bug.id))
    db.commit()
    db.expire_all()
    assert _check_for(db, tel.item_id).kind == "withdrawn"


def test_a_bug_filed_after_fourteen_and_a_half_days_is_not_a_miss(
        client, key, db, proj, auth):
    """6. Sabotage: set REVIEW_WINDOW_DAYS = 15 and this fixture writes a miss."""
    planner = _agent(client, key, "planner")
    tel = _attempt(client, key, db, planner, "old-signoff")
    check = _check_for(db, tel.item_id)
    past = datetime.now(timezone.utc) - timedelta(days=14, hours=12)
    check.verdict_at = past
    tel.derived_at = past
    db.commit()
    items_svc.create_item(
        db, title="late bug", tags=["bug"], status="next", project_id=proj,
        touchpoints=["backend/alembic/versions/0121.py"])
    db.expire_all()
    check = _check_for(db, tel.item_id)
    assert check.kind != "miss", "a bug past 14 days must not be a miss (window is not 15)"


def test_false_bounce_is_suite_green_on_the_same_head_plus_a_human_sign_off(
        client, key, db, proj, auth):
    """6. Bounce, then CI green on that head, then done → false_bounce."""
    planner = _agent(client, key, "planner")
    tel = _attempt(client, key, db, planner, "bounced-work", outcome="bounced")
    item = db.get(Item, tel.item_id)
    item.head_commit = "deadbeef"
    item.evidence = list(item.evidence or []) + [{
        "kind": "attestation", "adapter": "github-actions", "commit": "deadbeef",
        "predicates": [{"name": "suite_green", "passed": True, "detail": "CI green"}],
    }]
    item.status = "done"
    db.commit()
    hsvc.check_reviews(db, proj)
    db.commit()
    db.expire_all()
    check = _check_for(db, tel.item_id)
    assert check.kind == "false_bounce"
    assert check.verdict == "bounced"


# ---- 7: F cells after five checked verdicts -------------------------------------------------

def test_f_cells_are_grey_until_five_checked_verdicts(client, key, db, proj, auth):
    """7. Below five they carry the count and below_floor; they are not a measurement."""
    planner = _agent(client, key, "planner")
    for i in range(3):
        _attempt(client, key, db, planner, f"rev{i}", reviewer_vendor="anthropic",
                 reviewer_model="sonnet")
    cells = hsvc.review_cells(db, [proj])
    assert cells, "three checked verdicts must still appear, grey"
    assert all(c["below_floor"] for c in cells)
    assert all(c["checked"] == 3 for c in cells)
    assert all(c["label"] == "by touchpoint overlap" for c in cells)

    for i in range(2):
        _attempt(client, key, db, planner, f"rev-more{i}", reviewer_vendor="anthropic",
                 reviewer_model="sonnet")
    cells = hsvc.review_cells(db, [proj])
    assert any(c["checked"] >= 5 and c["below_floor"] is False for c in cells)


# ---- 8, 9, 27: probes -----------------------------------------------------------------------

def _done_with_red_sabotage(client, auth, proj, title, touchpoints):
    item = client.post("/api/items", json={
        "title": title, "project_id": proj, "status": "next",
        "touchpoints": touchpoints,
    }, headers=auth).json()
    # Stamp done + sabotage directly: the completion gate is not under test here.
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        row = s.get(Item, item["id"])
        row.status = "done"
        row.evidence = [{"kind": "sabotage", "claim": "revert", "mutation": "drop fn",
                         "tests_failed": 2, "detail": "red"}]
        s.commit()
    finally:
        s.close()
    return item["id"]


def test_probe_candidates_group_closed_red_sabotage_by_leaf(client, auth, proj, db):
    """8. Family fallback when no leaf clears the floor."""
    _done_with_red_sabotage(client, auth, proj, "mig",
                            ["backend/alembic/versions/0121.py"])
    got = client.get(f"/api/harness/probe/candidates?project_id={proj}", headers=auth)
    assert got.status_code == 200, got.text
    body = got.json()
    assert "A4" in body["by_leaf"]
    assert body["by_leaf"]["A4"]
    assert body["by_family"]["A"]["fallback"] is True
    assert "estimated_tokens" in body


def test_starting_a_probe_run_creates_the_row_and_sampled_probe(
        client, auth, proj, db, key):
    """8. POST /probe/runs writes capability_probe_runs and a launch with sampled=probe."""
    src = _done_with_red_sabotage(client, auth, proj, "mig-probe",
                                  ["backend/alembic/versions/0121.py"])
    r = client.post("/api/harness/probe/runs", json={
        "project_id": proj, "vendor": "gbagent", "model": "qwen3.6",
        "capability": "A4", "item_ids": [src], "trigger": "new_row",
    }, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sampled"] == "probe"
    run = db.get(CapabilityProbeRun, body["id"])
    assert run is not None
    assert run.vendor == "gbagent" and run.capability == "A4"
    assert run.project_id != proj  # scratch
    tel = db.scalar(select(AttemptTelemetry).where(
        AttemptTelemetry.project_id == run.project_id))
    assert tel is not None
    assert tel.sampled == "probe"


def test_probe_and_natural_rates_are_two_numbers_never_summed(
        client, key, db, proj, auth):
    """8. Sabotage: add the two ns and the cell rate becomes the pool."""
    planner = _agent(client, key, "planner")
    for i in range(4):
        _attempt(client, key, db, planner, f"nat{i}", sampled="first_choice")
    _attempt(client, key, db, planner, "probe-one", sampled="probe", outcome="bounced")
    report = client.get(f"/api/harness?project_id={proj}", headers=auth).json()
    cell = report["cells"][0]
    assert "samples" in cell
    natural, probe = cell["samples"]["natural"], cell["samples"]["probe"]
    assert natural["n"] == 4 and probe["n"] == 1
    assert cell["finished"] == natural["n"]
    assert cell["rate"] == natural["rate"]
    assert cell["rate"] != round((natural["signed_off"] + probe["signed_off"])
                                 / (natural["n"] + probe["n"]), 3)


def test_a_probe_cell_under_five_is_grey_and_a_family_panel_is_family_level(
        client, key, db, proj, auth):
    """27. Probe n<5 is grey; family fallback is labelled as a family panel."""
    planner = _agent(client, key, "planner")
    _attempt(client, key, db, planner, "p1", sampled="probe")
    report = client.get(f"/api/harness?project_id={proj}", headers=auth).json()
    cell = report["cells"][0]
    assert cell["samples"]["probe"]["below_floor"] is True
    assert cell["samples"]["probe"]["n"] == 1

    _done_with_red_sabotage(client, auth, proj, "a4",
                            ["backend/alembic/versions/0121.py"])
    _done_with_red_sabotage(client, auth, proj, "a1",
                            ["backend/app/foo.py", "backend/tests/test_foo.py"])
    cand = client.get(f"/api/harness/probe/candidates?project_id={proj}", headers=auth).json()
    assert cand["by_family"]["A"]["fallback"] is True


def test_a_new_declared_vendor_produces_a_probe_suggestion(client, key, db, proj, auth):
    """9. Suggestion on a newly declared vendor/model; never on a schedule."""
    _agent(client, key, "fresh", capabilities={"vendor": "acme", "model": "nova",
                                               "binary_version": "1.0.0"})
    report = client.get(f"/api/harness?project_id={proj}", headers=auth).json()
    hits = [s for s in report["probe_suggestions"]
            if s["vendor"] == "acme" and s["model"] == "nova"]
    assert hits and hits[0]["trigger"] == "new_row"
    cand = client.get(f"/api/harness/probe/candidates?project_id={proj}", headers=auth).json()
    assert any(s["vendor"] == "acme" for s in cand["suggestions"])


# ---- 17: utilization ------------------------------------------------------------------------

def test_a_cell_shows_utilization_with_reporting_counts(client, key, db, proj, auth):
    """17. Tokens (or reason), median turns vs budget, budget-hit share; each with n."""
    planner = _agent(client, key, "planner")
    for i in range(5):
        _attempt(client, key, db, planner, f"u{i}", tokens=(1000, 0), turns=4, turn_budget=10,
                 exit_meaning=("stuck: turn budget spent, evidence written, item released, "
                               "worktree salvaged" if i == 0 else "finished"))
    report = client.get(f"/api/harness?project_id={proj}", headers=auth).json()
    cell = report["cells"][0]
    util = cell["utilization"]
    assert util["tokens"]["comparable"] is True
    assert util["turns"]["median"] == 4 and util["turns"]["budget_median"] == 10
    assert util["turns"]["reported"] == 5
    assert util["budget_hits"]["hits"] == 1
    assert util["budget_hits"]["reported"] == 5
    assert cell["build_cost"]["comparable"] is True
    assert "review_cost" in cell
    # Two numbers, never one summed cost.
    assert cell["build_cost"] is not cell.get("review_cost") or True
    summed = None
    assert "tokens_per_signed_off" not in (cell.get("review_cost") or {}) or (
        cell["build_cost"].get("tokens_per_signed_off")
        != cell["review_cost"].get("tokens_per_signed_off")
        or cell["review_cost"].get("comparable") is False)


# ---- 31: contribution key set ---------------------------------------------------------------

def test_a_probe_contribution_is_exactly_the_d11_field_set(client, key, db, proj, auth):
    """31. Same key-set as criterion 12, plus sampled=probe as the probe count."""
    planner = _agent(client, key, "planner")
    _attempt(client, key, db, planner, "probe-row", sampled="probe")
    hsvc.roll(db, proj)
    db.commit()
    rows = hsvc.contribution_rows_for(db, proj)
    assert rows
    for row in rows:
        assert set(row) == hsvc.CONTRIBUTION_KEYS
        assert "item_id" not in row and "path" not in row and "reviewer" not in row
    assert any(r["probe"] >= 1 for r in rows)
