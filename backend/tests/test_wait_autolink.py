"""GRPH-1650: a decision wait filed by a claim holder is linked, blocked and never claimable."""
from __future__ import annotations

import pytest

from app.db import SessionLocal
from app.models import Link, utcnow
from app.services import items as items_svc
from app.services import waits as waits_svc


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class _A:
    id = "GRPH-A1650"


def _holder(db):
    item = items_svc.create_item(db, project_id="core", title="work", status="in_progress")
    item.claimed_by, item.claimed_at = "GRPH-A1650", utcnow()
    db.commit()
    return _A, item


def test_claim_holder_question_is_linked_and_blocks(db):
    agent, item = _holder(db)
    wait = items_svc.create_item(db, project_id="core", title="q", question="which?",
                                 options=["a", "b"])
    out = waits_svc.attach_to_claim(db, wait, agent.id)
    db.refresh(item)
    assert out["linked"] is True
    assert wait.status == "blocked"
    assert item.status == "blocked" and item.blocker == wait.id
    link = db.query(Link).filter(Link.a == item.id, Link.b == wait.id).one()
    assert link.type == "dependency" and link.reason == "wait:decision"
    assert waits_svc.question_state(db, item) is not None


def test_no_claim_keeps_old_behaviour(db):
    wait = items_svc.create_item(db, project_id="core", title="q", question="which?")
    out = waits_svc.attach_to_claim(db, wait, None)
    assert out["linked"] is False and "no ticket was linked" in out["note"]
    assert wait.status == "backlog"
    assert db.query(Link).filter(Link.b == wait.id).count() == 0


def test_wait_decision_is_never_claimable(db):
    wait = items_svc.create_item(db, project_id="core", title="q", question="which?",
                                 status="next")
    assert items_svc.claimable(wait) is False
    got = items_svc.claim_next(db, "GRPH-A1651", project_id="core")
    assert got is None or got.id != wait.id
