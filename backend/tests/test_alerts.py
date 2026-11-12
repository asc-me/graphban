"""Alerts: hand_to_person, the grace sweep, clearing, the REST surface (GRPH-1527)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.db import SessionLocal
from app.models import Alert, Item, Membership, User, utcnow
from app.services import alerts as alerts_svc
from app.services import items as items_svc
from app.services import links as links_svc
from app.services import waits


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _pair(db):
    ticket = items_svc.create_item(db, project_id="core", title="feature", status="in_progress")
    wait = items_svc.create_item(db, project_id="core", title="which?", status="blocked",
                                 question="Which store?", options=["a", "b"])
    links_svc.create_link(db, a=ticket.id, b=wait.id, type_="dependency", project_id="core")
    items_svc.update_item(db, ticket.id, status="blocked")
    return ticket, wait


def _count(db, wait=None):
    q = db.query(Alert)
    if wait is not None:
        q = q.filter(Alert.item_id == wait.id)
    return q.count()


def _owner(db):
    m = db.query(Membership).filter(Membership.project_id == "core", Membership.role == "owner").first()
    return m.user_id if m else None


def test_filing_writes_no_alert_and_needs_person_writes_one_set(db):
    _, wait = _pair(db)
    assert _count(db) == 0
    out = waits.answer_wait(db, wait.id, needs_person=True)
    assert out["handed"] is True
    db.refresh(wait)
    assert wait.decision["needs_person_at"]
    n = _count(db, wait)
    assert n == (1 if _owner(db) else 0)
    # twice: still one set
    waits.hand_to_person(db, wait)
    assert _count(db, wait) == n


def test_answer_first_makes_hand_off_a_noop_and_answer_after_hand_off_clears(db):
    _, wait = _pair(db)
    waits.hand_to_person(db, wait)
    waits.answer_wait(db, wait.id, choice="a")
    assert all(a.cleared_at for a in db.query(Alert).filter(Alert.item_id == wait.id))
    _, wait2 = _pair(db)
    waits.answer_wait(db, wait2.id, choice="b")
    before = _count(db)
    assert waits.hand_to_person(db, wait2)["handed"] is False
    assert _count(db) == before


def test_failed_alert_write_leaves_needs_person_unset_and_sweep_retries(db, monkeypatch):
    _, wait = _pair(db)
    real = alerts_svc.write_for_wait

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(alerts_svc, "write_for_wait", boom)
    with pytest.raises(RuntimeError):
        waits.hand_to_person(db, wait)
    db.refresh(wait)
    assert not wait.decision.get("needs_person_at")
    monkeypatch.setattr(alerts_svc, "write_for_wait", real)
    assert waits.sweep_questions(db, grace_seconds=0, now=utcnow() + timedelta(seconds=1))
    db.refresh(wait)
    assert wait.decision["needs_person_at"]


def test_sweep_respects_the_grace_period(db):
    _, wait = _pair(db)
    assert waits.sweep_questions(db, grace_seconds=600) == []
    db.refresh(wait)
    assert not wait.decision.get("needs_person_at")
    assert waits.sweep_questions(db, grace_seconds=600, now=utcnow() + timedelta(seconds=700))
    db.refresh(wait)
    assert wait.decision["needs_person_at"]


def test_no_recipient_writes_no_alert_and_triage_says_so(db, monkeypatch):
    _, wait = _pair(db)
    monkeypatch.setattr("app.services.question_recipients.question_recipients",
                        lambda db, item: {"users": [], "reason": "nobody with access could be found"})
    waits.hand_to_person(db, wait)
    db.refresh(wait)
    assert _count(db, wait) == 0
    assert wait.decision["needs_person_at"] and wait.decision["nobody_alerted"]
    from app.services import triage

    rows = triage._blocker_rows(db, "core", utcnow())
    assert any(r.get("nobody_alerted") for r in rows)


def test_rest_list_read_and_clear_drops_the_count(client, auth, db):
    _, wait = _pair(db)
    me = db.query(User).first()
    alerts_svc.write_for_wait(db, wait, [me.id])
    db.commit()
    body = client.get("/api/me/alerts", headers=auth).json()
    assert body["unread"] == 1 and body["alerts"][0]["question"] == "Which store?"
    aid = body["alerts"][0]["id"]
    assert client.post(f"/api/me/alerts/{aid}/read", headers=auth).json()["unread"] == 0
    assert client.post("/api/me/alerts/nope/read", headers=auth).status_code == 404
    db2 = SessionLocal()
    try:
        w = db2.get(Item, wait.id)
        waits.answer_wait(db2, w.id, choice="a")
    finally:
        db2.close()
    assert client.get("/api/me/alerts", headers=auth).json()["alerts"] == []
