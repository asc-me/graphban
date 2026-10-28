"""Structured decision waits and blocker actions (GRPH-1137 / PRD-60 D7)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.db import SessionLocal
from app.models import Event, Item, Membership, User, utcnow
from app.services import email as email_svc
from app.services import fleet as fleet_svc
from app.services import items as items_svc
from app.services import links as links_svc
from app.services import waits as waits


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _pair(db, *, holder="GRPH-A1", options=("redis", "postgres")):
    original = items_svc.create_item(db, project_id="core", title="feature", status="in_progress")
    original.claimed_by, original.claimed_at = holder, utcnow()
    wait = items_svc.create_item(db, project_id="core", title="which store?", status="blocked",
                                 question="Which store?", options=list(options))
    links_svc.create_link(db, a=original.id, b=wait.id, type_="dependency", project_id="core")
    items_svc.update_item(db, original.id, status="blocked")
    db.refresh(original)
    return original, wait


def _events(db, action):
    return db.query(Event).filter(Event.action == action).all()


def test_question_and_options_ride_the_item_read(db):
    _, wait = _pair(db)
    d = items_svc.item_dict(wait)["decision"]
    assert d == {"question": "Which store?", "options": ["redis", "postgres"]}
    assert "wait:decision" in wait.tags
    assert wait in waits.waiting(db, "core")
    feed = {r["id"]: r for r in waits.triage_blockers(db, "core")}
    assert feed[wait.key]["decision"]["options"] == ["redis", "postgres"]


def test_options_are_optional_but_need_a_question(db):
    ok = items_svc.create_item(db, project_id="core", title="q", question="why?")
    assert ok.decision == {"question": "why?", "options": []}
    with pytest.raises(ValueError):
        items_svc.create_item(db, project_id="core", title="q", options=["a"])


def test_answer_marks_done_returns_original_to_next(db):
    original, wait = _pair(db)
    waits.answer_wait(db, wait.id, choice="redis")
    db.refresh(wait)
    db.refresh(original)
    assert wait.status == "done"
    assert wait.decision["answer"]["choice"] == "redis"
    assert original.status == "next" and original.claimed_by is None
    assert len(_events(db, "blocker.answer")) == 1


def test_answer_rejects_unknown_option_and_double_answer(db):
    _, wait = _pair(db)
    with pytest.raises(ValueError):
        waits.answer_wait(db, wait.id, choice="mysql")
    with pytest.raises(ValueError):
        waits.answer_wait(db, wait.id)
    waits.answer_wait(db, wait.id, text="use sqlite")
    with pytest.raises(waits.ActionRefused):
        waits.answer_wait(db, wait.id, choice="redis")


def test_live_holder_gets_the_answer_as_a_directive_once(db):
    _, wait = _pair(db, holder="GRPH-A1")
    waits.answer_wait(db, wait.id, choice="postgres")
    assert fleet_svc.collect_directive(db, "GRPH-A9") is None
    d = fleet_svc.collect_directive(db, "GRPH-A1")
    assert d["type"] == "decision_answered" and d["answer"]["choice"] == "postgres"
    assert fleet_svc.collect_directive(db, "GRPH-A1") is None


def test_a_dead_holder_is_not_notified_but_the_wait_keeps_the_answer(db):
    original, wait = _pair(db)
    original.claimed_at = utcnow() - timedelta(hours=2)
    db.commit()
    waits.answer_wait(db, wait.id, choice="redis")
    assert fleet_svc.collect_directive(db, "GRPH-A1") is None
    db.refresh(wait)
    assert wait.decision["answer"]["choice"] == "redis"


def test_release_claim_drops_the_lease(db):
    original, _ = _pair(db)
    original.status = "in_progress"
    db.commit()
    waits.release_claim(db, original.id)
    db.refresh(original)
    assert (original.status, original.claimed_by, original.assignee) == ("next", None, "")
    assert len(_events(db, "blocker.release_claim")) == 1


def test_unblock_picks_next_or_in_progress_by_lease(db):
    original, _ = _pair(db)
    assert waits.unblock(db, original.id)["status"] == "in_progress"
    other = items_svc.create_item(db, project_id="core", title="x", status="blocked")
    other.blocker = "stuck"
    db.commit()
    assert waits.unblock(db, other.id)["status"] == "next"
    db.refresh(other)
    assert other.blocker == ""
    with pytest.raises(waits.ActionRefused):
        waits.unblock(db, other.id)


def test_snooze_hides_from_triage_and_undo_restores(db):
    _, wait = _pair(db)
    waits.snooze(db, wait.id, until=utcnow() + timedelta(hours=1))
    assert wait.key not in {r["id"] for r in waits.triage_blockers(db, "core")}
    waits.undo(db, wait.id)
    assert wait.key in {r["id"] for r in waits.triage_blockers(db, "core")}
    with pytest.raises(ValueError):
        waits.snooze(db, wait.id, until=utcnow() - timedelta(minutes=1))


def test_undo_answer_restores_wait_and_dependent(db):
    original, wait = _pair(db)
    waits.answer_wait(db, wait.id, choice="redis")
    waits.undo(db, wait.id)
    db.refresh(wait)
    db.refresh(original)
    assert wait.status == "blocked"
    assert "answer" not in wait.decision
    assert original.status == "blocked"
    assert len(_events(db, "blocker.undo")) == 1


def test_undo_after_a_later_change_is_refused(db):
    original, _ = _pair(db)
    waits.unblock(db, original.id)
    db.refresh(original)
    original.blocker = "changed later"
    db.commit()
    with pytest.raises(waits.ActionRefused):
        waits.undo(db, original.id)


def test_escalate_writes_one_event_and_emails_the_owner_when_smtp_is_set(db, monkeypatch):
    from app.config import settings

    wait = items_svc.create_item(db, project_id="core", title="w", status="blocked")
    owner = db.query(Membership).filter(Membership.project_id == "core",
                                        Membership.role == "owner").first()
    monkeypatch.setattr(settings, "smtp_host", "smtp.invalid")
    sent = []
    monkeypatch.setattr(email_svc, "send_email", lambda *a, **k: sent.append(a) or True)
    out = waits.escalate(db, wait.id)
    assert len(_events(db, "blocker.escalate")) == 1
    if owner is not None:
        assert out["emailed"] == len(sent) >= 1
        assert db.get(User, owner.user_id).email == sent[0][0]
    monkeypatch.setattr(settings, "smtp_host", "")
    assert waits.escalate(db, wait.id)["emailed"] == 0


def test_rest_files_answers_and_undoes(client, auth):
    made = client.post("/api/items", headers=auth, json={
        "title": "pick", "project_id": "core", "status": "blocked",
        "question": "Which?", "options": ["a", "b"]})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["decision"] == {"question": "Which?", "options": ["a", "b"]}
    wid = body["id"]
    assert any(r["id"] == wid and r["decision"] for r in
               client.get("/api/items/blockers?project_id=core", headers=auth).json()["blockers"])
    assert client.post(f"/api/items/{wid}/answer", headers=auth, json={"option": "z"}).status_code == 422
    assert client.post(f"/api/items/{wid}/answer", headers=auth, json={"option": "a"}).status_code == 200
    assert client.get(f"/api/items/{wid}", headers=auth).json()["decision"]["answer"]["choice"] == "a"
    assert client.post(f"/api/items/{wid}/answer", headers=auth, json={"option": "a"}).status_code == 409
    assert client.post(f"/api/items/{wid}/undo", headers=auth).status_code == 200
    assert client.get(f"/api/items/{wid}", headers=auth).json()["status"] == "blocked"


def test_mcp_create_item_schema_advertises_question_and_options():
    from app.mcp_server import TOOLS

    props = next(t for t in TOOLS if t["name"] == "create_item")["inputSchema"]["properties"]
    assert "question" in props and "options" in props
