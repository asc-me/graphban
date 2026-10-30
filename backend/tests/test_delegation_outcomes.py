"""GRPH-1226: a builder handing back its lease from `review` must not pre-empt the verdict."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.models import AttemptTelemetry, Delegation, HarnessReviewCheck, Item
from app.services import harness
from app.services import items as items_svc

from test_delegation import (_agent, _child, _delegate, _item, _mcp, _ok, db, key, proj,  # noqa: F401
                             _linked)

MIGRATION = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
             / "0159_backfill_review_handin_outcomes.py")


def _handed_in(client, key, db, title):
    planner = _agent(client, key, f"planner-{title}")
    item, did, child = _linked(client, key, db, planner, title)
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    _ok(_mcp(client, key, "release_item", {"id": item, "agent_id": child}))
    row = db.get(Delegation, did); db.refresh(row)
    assert row.outcome is None, "the release from review must leave the attempt open"
    return item, did


def test_release_after_hand_in_then_sign_off_is_signed_off(client, key, db):
    item, did = _handed_in(client, key, db, "so")
    reviewer = _agent(client, key, "rev-so", capabilities={"instance": "rev-so"})
    _ok(_mcp(client, key, "sign_off", {"id": item, "agent_id": reviewer,
                                        "evidence": [{"kind": "note", "detail": "read it"}]}))
    row = db.get(Delegation, did); db.refresh(row)
    assert row.outcome == "signed_off"


def test_release_after_hand_in_then_bounce_is_bounced(client, key, db):
    item, did = _handed_in(client, key, db, "bo")
    reviewer = _agent(client, key, "rev-bo", capabilities={"instance": "rev-bo"})
    _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer, "reason": "no tests"}))
    row = db.get(Delegation, did); db.refresh(row)
    assert row.outcome == "bounced"


def test_release_from_in_progress_is_still_released(client, key, db):
    planner = _agent(client, key, "planner-ip")
    item, did, child = _linked(client, key, db, planner, "ip")
    _ok(_mcp(client, key, "release_item", {"id": item, "agent_id": child}))
    row = db.get(Delegation, did); db.refresh(row)
    assert row.outcome == "released"


def _load_migration():
    spec = importlib.util.spec_from_file_location("m0159", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_rewrites_preempted_rows_only_and_is_idempotent(client, key, db):
    planner = _agent(client, key, "planner-mig")
    a, did_a, _ = _linked(client, key, db, planner, "mig-a")
    b, did_b, _ = _linked(client, key, db, planner, "mig-b")
    c, did_c, _ = _linked(client, key, db, planner, "mig-c")
    t = datetime.now(timezone.utc)
    for did in (did_a, did_b, did_c):
        row = db.get(Delegation, did)
        row.outcome, row.claimed_at, row.finished_at = "released", t - timedelta(minutes=10), t
        db.add(AttemptTelemetry(id=f"at_{did[-8:]}", delegation_id=did, project_id=row.project_id,
                                item_id=row.item_id, outcome="released", capabilities=["E1"]))
    db.get(Item, c).bounce_reason = "no tests for the new path"
    for hrc, did, verdict in (("hrc_mig_a", did_a, "signed_off"), ("hrc_mig_c", did_c, "bounced")):
        db.add(HarnessReviewCheck(
            id=hrc, project_id=db.get(Delegation, did).project_id, delegation_id=did,
            item_id=db.get(Delegation, did).item_id, reviewer_agent_id="r", verdict=verdict,
            kind="confirmed", capabilities=["E1"], bounce_category=None,
            checked_at=t + timedelta(minutes=5)))
    db.commit()
    mig, bind = _load_migration(), db.connection()

    assert mig.backfill(bind, dry_run=True) == 2
    assert db.get(Delegation, did_a).outcome == "released", "dry run writes nothing"
    assert mig.backfill(bind) == 2
    assert mig.backfill(bind) == 0, "second run changes nothing more"
    db.commit(); db.expire_all()

    def tel(did):
        return db.scalars(select(AttemptTelemetry).where(AttemptTelemetry.delegation_id == did)).one()

    assert db.get(Delegation, did_a).outcome == "signed_off"
    assert db.get(Delegation, did_c).outcome == "bounced"
    assert db.get(Delegation, did_b).outcome == "released"
    assert "E1" in tel(did_b).capabilities, "an untouched release keeps its derivation"
    for did in (did_a, did_c):
        row = tel(did)
        assert "E1" not in row.capabilities, "a verdict ending is not a protocol failure"
        assert row.claim_to_finish_s == 15 * 60, "the attempt ends at the verdict"
        assert "E1" not in db.get(HarnessReviewCheck, f"hrc_mig_{'a' if did == did_a else 'c'}").capabilities
    assert tel(did_a).outcome == "signed_off" and tel(did_a).bounce_category is None
    assert tel(did_c).outcome == "bounced"
    assert tel(did_c).bounce_category == harness.bounce_category("no tests for the new path")
    assert tel(did_c).bounce_category is not None
    assert db.get(HarnessReviewCheck, "hrc_mig_c").bounce_category == tel(did_c).bounce_category
