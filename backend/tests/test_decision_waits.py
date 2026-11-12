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


def _gate_pair(db, kind="merge"):
    ticket = items_svc.create_item(db, project_id="core", title="ticket", status="in_progress")
    gate = items_svc.create_item(db, project_id="core", title=f"gate {kind}", status="blocked",
                                 tags=[f"wait:{kind}"])
    links_svc.create_link(db, a=ticket.id, b=gate.id, type_="dependency", project_id="core")
    items_svc.update_item(db, ticket.id, status="blocked")
    db.refresh(ticket)
    return ticket, gate


def _seed_needs_person(db, wait):
    wait.decision = {**wait.decision, "needs_person_at": "2026-11-11T10:00:00+00:00"}
    db.commit()
    db.refresh(wait)


def test_question_state_checking_then_response_needed_then_cleared(db):
    ticket, wait = _pair(db)
    assert waits.question_state(db, wait) == "checking"
    assert waits.question_state(db, ticket) == "checking"
    assert items_svc.item_dict(ticket)["question_state"] == "checking"
    assert items_svc.get_item_details(db, ticket.key)["question_state"] == "checking"
    _seed_needs_person(db, wait)
    assert waits.question_state(db, wait) == "response_needed"
    assert waits.question_state(db, ticket) == "response_needed"
    assert items_svc.item_dict(ticket)["question_state"] == "response_needed"
    waits.answer_wait(db, wait.id, choice="redis")
    db.refresh(wait)
    db.refresh(ticket)
    assert waits.question_state(db, wait) is None
    assert waits.question_state(db, ticket) is None
    assert "question_state" not in items_svc.item_dict(wait)


def test_gate_waits_read_as_plain_blocked(db):
    for kind in ("merge", "deploy", "secret", "access"):
        ticket, gate = _gate_pair(db, kind)
        assert waits.question_state(db, gate) is None
        assert waits.question_state(db, ticket) is None
        assert "question_state" not in items_svc.item_dict(ticket)


def test_item_dict_renders_question_state_from_the_one_function(db, monkeypatch):
    """Removing the `question_state` call from `item_dict` fails this test."""
    ticket, _ = _pair(db)
    monkeypatch.setattr(waits, "question_state", lambda _db, _item: "response_needed")
    assert items_svc.item_dict(ticket)["question_state"] == "response_needed"


def test_rest_list_and_item_carry_question_state(client, auth, db):
    ticket, wait = _pair(db)
    rows = {r["id"]: r for r in client.get("/api/items?project_id=core", headers=auth).json()}
    assert rows[ticket.key]["question_state"] == "checking"
    assert rows[wait.key]["question_state"] == "checking"
    _seed_needs_person(db, wait)
    got = client.get(f"/api/items/{ticket.key}", headers=auth).json()
    assert got["question_state"] == "response_needed"


# ---- P75 S1: answer_wait for agents, refused unanswered clear, answered_decisions --------------

def test_an_unanswered_decision_wait_cannot_be_cleared_by_update_item(db):
    _, wait = _pair(db)
    with pytest.raises(ValueError, match="answer_wait"):
        items_svc.update_item(db, wait.id, status="done")
    db.refresh(wait)
    assert wait.status != "done"


def test_the_filing_agent_cannot_answer_its_own_wait(db):
    _, wait = _pair(db, holder="GRPH-A1")
    with pytest.raises(waits.ActionRefused, match="cannot answer"):
        waits.answer_wait(db, wait.id, text="redis", agent="GRPH-A1",
                          source={"kind": "session", "quote": "I decided"})


def test_an_agent_answer_with_an_unresolvable_prd_ref_is_refused(db):
    _, wait = _pair(db)
    with pytest.raises(ValueError, match="does not resolve"):
        waits.answer_wait(db, wait.id, text="redis", agent="GRPH-A2",
                          source={"kind": "prd", "ref": "GRPH-P99999", "quote": "x"})
    db.refresh(wait)
    assert not wait.decision.get("answer")


def test_an_agent_answer_needs_a_source(db):
    _, wait = _pair(db)
    with pytest.raises(ValueError, match="source"):
        waits.answer_wait(db, wait.id, text="redis", agent="GRPH-A2")


def test_agent_answer_records_by_and_source_and_reaches_the_brief(db):
    from app.services import delegation as delegation_svc

    original, wait = _pair(db, holder="GRPH-A1")
    out = waits.answer_wait(db, wait.id, text="redis", agent="GRPH-A2",
                            source={"kind": "session", "quote": "the user said redis"})
    assert out["answer"]["by"]["agent"] == "GRPH-A2"
    assert out["answer"]["source"]["kind"] == "session"
    db.refresh(original)
    assert original.status == "next"
    # A different agent reading the item sees the same answer.
    entry = delegation_svc.brief(db, original)["answered_decisions"]
    assert len(entry) == 1
    assert entry[0]["wait"] == wait.key and entry[0]["answer"] == "redis"
    assert entry[0]["by"]["agent"] == "GRPH-A2" and entry[0]["source"]["quote"]


def test_a_person_answer_has_by_user_and_no_source(db):
    from app.services import delegation as delegation_svc

    original, wait = _pair(db)
    waits.answer_wait(db, wait.id, choice="redis")
    entry = delegation_svc.brief(db, original)["answered_decisions"][0]
    assert "agent" not in entry["by"] and entry["source"] == {}


def test_a_done_wait_with_no_stored_answer_reads_unanswered_on_the_brief(db):
    from app.services import delegation as delegation_svc

    original, wait = _pair(db)
    wait.status = "done"  # as a legacy row, written before the refusal
    db.commit()
    entry = delegation_svc.brief(db, original)["answered_decisions"][0]
    assert entry["unanswered"] is True
    assert "still open" in delegation_svc.brief(db, original)["text"]


def test_needs_person_records_no_answer(db):
    _, wait = _pair(db)
    out = waits.answer_wait(db, wait.id, agent="GRPH-A2", needs_person=True)
    db.refresh(wait)
    assert out["needs_person"] is True
    assert wait.decision.get("needs_person_at") and not wait.decision.get("answer")
    assert wait.status != "done"


def test_the_graphban_answer_skill_uses_answer_wait_not_update_item_done():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in ("cli/src/gban/skills/graphban-answer/SKILL.md",
                ".claude/skills/graphban-answer/SKILL.md",
                ".agents/skills/graphban-answer/SKILL.md"):
        text = (root / rel).read_text()
        assert "answer_wait" in text, rel
        assert "update_item status=done" not in text and 'update_item(status="done"' not in text, rel
        assert "then `status=done`" not in text, rel


def _mcp_answer(client, auth, args):
    key = client.post("/api/api-keys", json={"name": "answerer", "project_id": "core",
                                             "scopes": ["read", "write"]},
                      headers=auth).json()["plaintext"]
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "answer_wait", "arguments": args}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_mcp_answer_wait_records_the_answer_and_releases_the_ticket(client, auth, db):
    original, wait = _pair(db, holder="GRPH-A1")
    res = _mcp_answer(client, auth, {"wait": wait.key, "answer": "redis", "agent_id": "GRPH-A2",
                                     "source": {"kind": "session", "quote": "the user said redis"}})
    assert not res.get("isError"), res
    out = res["structuredContent"]
    assert out["wait"] == wait.key
    assert out["answer"]["by"]["agent"] == "GRPH-A2"
    db.refresh(original)
    assert original.status == "next"


def test_mcp_answer_wait_on_its_own_wait_is_refused_naming_the_rule(client, auth, db):
    _, wait = _pair(db, holder="GRPH-A1")
    res = _mcp_answer(client, auth, {"wait": wait.key, "answer": "redis", "agent_id": "GRPH-A1",
                                     "source": {"kind": "session", "quote": "mine"}})
    assert res["isError"]
    assert res["structuredContent"]["error"]["rule_id"] == "decision.filer_cannot_answer"
    db.refresh(wait)
    assert not wait.decision.get("answer")


# --- GRPH-1529: drain status after an answer --------------------------------------------


def _seat(db, *, prd_id=None, seen_ago=0, number=90):
    from app.models import Agent, Enrolment

    eid = f"enr-d{number}-{prd_id}"
    db.add(Enrolment(id=eid, project_id="core", code_hash="x", role="worker", prd_id=prd_id,
                     expires_at=utcnow() + timedelta(hours=1)))
    db.flush()
    db.add(Agent(id=f"core-A{number}", project_id="core", number=number, enrolment_id=eid,
                 state="working", last_seen_at=utcnow() - timedelta(seconds=seen_ago)))
    db.commit()


def test_drain_status_live_not_live_and_scoped(db):
    assert fleet_svc.drain_status(db, "core", "p1") == "not_live"
    _seat(db, prd_id="other-prd", number=91)
    assert fleet_svc.drain_status(db, "core", "p1") == "not_live"
    _seat(db, prd_id="p1", number=92, seen_ago=10)
    assert fleet_svc.drain_status(db, "core", "p1") == "live"


def test_drain_status_stale_seat_is_not_live(db):
    _seat(db, prd_id="p1", number=93, seen_ago=fleet_svc.presence_ttl_seconds() + 60)
    assert fleet_svc.drain_status(db, "core", "p1") == "not_live"


def test_drain_status_failed_read_is_unknown_never_no_drain(db):
    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

    assert fleet_svc.drain_status(Broken(), "core", "p1") == "unknown"
    _, wait = _pair(db)
    note = waits.drain_note(db, wait, status="unknown")
    assert note["message"] == "Could not check for a running drain."
    assert "No drain is running" not in note["message"] and "command" not in note


def test_answer_response_says_drain_and_who_answered(db):
    original, wait = _pair(db)
    out = waits.answer_wait(db, wait.id, text="redis", agent="GRPH-A2",
                            source={"kind": "session", "quote": "the user said redis"})
    assert out["drain"]["status"] == "not_live"
    assert out["drain"]["message"].startswith("No drain is running")
    assert out["answered_by"] == "answered from session by GRPH-A2"
    assert out["quote"] == "the user said redis"
    _, wait2 = _pair(db)
    assert waits.answer_wait(db, wait2.id, choice="redis")["answered_by"].startswith("answered by ")


def test_answer_response_says_drain_is_running_with_a_seat_scoped_to_the_prd(db):
    from app.services import prds as prds_svc

    prd = prds_svc.create_prd(db, project_id="core", title="Drained", body="## A\n\nx\n")
    original, wait = _pair(db)
    original.prd_id = prd.id
    db.commit()
    _seat(db, prd_id=prd.id, number=94)
    out = waits.answer_wait(db, wait.id, choice="redis")
    key = original.prd_key
    assert key and out["drain"]["status"] == "live" and out["drain"]["prd"] == key
    assert out["drain"]["message"] == f"A drain is running for {key}. It will pick this ticket up."
    assert "command" not in out["drain"]
