"""GRPH-1226: a builder handing back its lease from `review` must not pre-empt the verdict."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.models import AttemptTelemetry, Delegation, HarnessReviewCheck
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
    _, did_b, _ = _linked(client, key, db, planner, "mig-b")
    t = datetime.now(timezone.utc)
    for did in (did_a, did_b):
        row = db.get(Delegation, did)
        row.outcome, row.finished_at = "released", t
        db.add(AttemptTelemetry(id=f"at_{did[-8:]}", delegation_id=did, project_id=row.project_id,
                                item_id=row.item_id, outcome="released"))
    db.add(HarnessReviewCheck(
        id="hrc_mig", project_id=db.get(Delegation, did_a).project_id, delegation_id=did_a,
        item_id=db.get(Delegation, did_a).item_id, reviewer_agent_id="r", verdict="signed_off",
        kind="confirmed", checked_at=t + timedelta(minutes=5)))
    db.commit()
    mig, bind = _load_migration(), db.connection()

    assert mig.backfill(bind, dry_run=True) == 1
    assert db.get(Delegation, did_a).outcome == "released", "dry run writes nothing"
    assert mig.backfill(bind) == 1
    assert mig.backfill(bind) == 0, "second run changes nothing more"
    db.commit(); db.expire_all()
    assert db.get(Delegation, did_a).outcome == "signed_off"
    assert db.get(Delegation, did_b).outcome == "released"
    tel = db.scalars(select(AttemptTelemetry).where(AttemptTelemetry.delegation_id == did_a)).one()
    assert tel.outcome == "signed_off"
