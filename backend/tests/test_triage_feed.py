"""Unified triage feed (GRPH-1135 / PRD-60 D5)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Item, Request


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _feed(client, auth, pid="core"):
    r = client.get(f"/api/projects/{pid}/triage", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _req(client, auth, type_="bug", title="Crash on save"):
    r = client.post("/api/requests", headers=auth,
                    json={"type": type_, "title": title, "project_id": "core"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _item(client, auth, title, status="backlog", **kw):
    r = client.post("/api/items", headers=auth, json={"title": title, "project_id": "core", **kw})
    assert r.status_code in (200, 201), r.text
    key = r.json()["id"]
    if status != "backlog":
        client.patch(f"/api/items/{key}", headers=auth, json={"status": status})
    return key


def test_all_kinds_blockers_first_with_counts(client, auth, db):
    _req(client, auth, "bug", "Crash on save")
    _req(client, auth, "feature", "Dark mode")
    a = _item(client, auth, "Edit router", touchpoints=["app/routers/x.py"])
    b = _item(client, auth, "Edit router too", touchpoints=["app/routers/x.py"])
    blocked = _item(client, auth, "Needs a call")
    client.patch(f"/api/items/{blocked}", headers=auth,
                 json={"status": "blocked", "blocker": "waiting on legal"})
    f = _feed(client, auth)
    kinds = [r["kind"] for r in f["rows"]]
    assert kinds[0] == "blocker"
    assert {"bug", "feature", "collision"} <= set(kinds)
    assert f["failed_sources"] == []
    c = f["counts"]
    for tab, kinds_ in (("blockers", {"blocker"}), ("bugs", {"bug"}),
                        ("requests", {"feature", "enhancement"}), ("collisions", {"collision"})):
        assert c[tab] == sum(r["kind"] in kinds_ for r in f["rows"]) >= 1
    assert c["all"] == len(f["rows"])
    blocker = next(r for r in f["rows"] if r["id"] == blocked)
    assert blocker["reason"] == "waiting on legal"
    assert all(r["kind"] == "blocker" for r in f["rows"][:c["blockers"]])
    col = next(r for r in f["rows"] if r["kind"] == "collision"
               and {a, b} <= {m["id"] for m in r["members"]})
    assert col["risk"] == "serialize"
    assert col["recommendation"] == (
        "Running them together is what produces the conflict this check exists to predict."
    )


def test_collision_recommendation_blocked_when_a_member_is_held(client, auth):
    held = _item(client, auth, "Held member", touchpoints=["app/shared.py"], status="in_progress")
    _item(client, auth, "Waiting behind", touchpoints=["app/shared.py"])
    f = _feed(client, auth)
    col = next(r for r in f["rows"] if r["kind"] == "collision"
               and held in {m["id"] for m in r["members"]})
    assert col["risk"] == "blocked"
    assert col["recommendation"] == (
        "the overlap is already held, so a second claim would be refused rather than queued"
    )


def test_failing_source_is_named_not_empty(client, auth, monkeypatch):
    before = _feed(client, auth)
    _req(client, auth)
    from app.services import collision

    def boom(*a, **k):
        raise RuntimeError("clustering down")

    monkeypatch.setattr(collision, "triage", boom)
    f = _feed(client, auth)
    assert f["failed_sources"] == ["collisions"]
    assert not any(r["kind"] == "collision" for r in f["rows"])
    assert len(f["rows"]) > 0 and f["counts"]["bugs"] == before["counts"]["bugs"] + 1


def test_snoozed_request_returns_after_its_date(client, auth, db):
    rid = _req(client, auth)
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert client.post(f"/api/requests/{rid}/snooze", headers=auth, json={"until": until}).status_code == 200
    assert rid not in {r["id"] for r in _feed(client, auth)["rows"]}
    req = db.get(Request, rid)
    req.snoozed_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert rid in {r["id"] for r in _feed(client, auth)["rows"]}


def test_collision_id_stable_until_member_set_changes(client, auth):
    _item(client, auth, "One", touchpoints=["app/a.py"])
    _item(client, auth, "Two", touchpoints=["app/a.py"])
    def ids():
        return {r["id"] for r in _feed(client, auth)["rows"] if r["kind"] == "collision"}
    first = ids()
    assert first and ids() == first
    _item(client, auth, "Three", touchpoints=["app/a.py"])
    assert ids() != first


def test_other_projects_key_is_refused(client, auth):
    r = client.get("/api/projects/does-not-exist/triage", headers=auth)
    assert r.status_code == 404


def test_decision_wait_rows_carry_question_state_and_only_response_needed_counts(
        client, auth, db):
    from app.services import items as items_svc
    from app.services import links as links_svc

    ticket = items_svc.create_item(db, project_id="core", title="needs an answer",
                                   status="in_progress")
    wait = items_svc.create_item(db, project_id="core", title="which store?", status="blocked",
                                 question="Which store?", options=["a", "b"])
    gate = items_svc.create_item(db, project_id="core", title="merge it", status="blocked",
                                 tags=["wait:merge"])
    links_svc.create_link(db, a=ticket.id, b=wait.id, type_="dependency", project_id="core")
    items_svc.update_item(db, ticket.id, status="blocked")
    f = _feed(client, auth)
    rows = {r["id"]: r for r in f["rows"]}
    assert rows[wait.key]["question_state"] == "checking"
    assert rows[ticket.key]["question_state"] == "checking"
    assert "question_state" not in rows[gate.key]
    assert f["waiting_on_person"] == 0
    wait.decision = {**wait.decision, "needs_person_at": "2026-11-11T10:00:00+00:00"}
    db.commit()
    f = _feed(client, auth)
    rows = {r["id"]: r for r in f["rows"]}
    assert rows[wait.key]["question_state"] == "response_needed"
    assert rows[ticket.key]["question_state"] == "response_needed"
    assert f["waiting_on_person"] == 2


# --- GRPH-1529: an answered decision shows who answered and whether a drain is live ---------


def _answered_pair(db, title="which store?"):
    from app.services import items as items_svc
    from app.services import links as links_svc
    from app.services import prds as prds_svc

    prd = prds_svc.create_prd(db, project_id="core", title="Store", body="## A\n\nx\n")
    ticket = items_svc.create_item(db, project_id="core", title="needs an answer",
                                   status="in_progress", prd_id=prd.id)
    ticket.claimed_by = "core-A1"
    wait = items_svc.create_item(db, project_id="core", title=title, status="blocked",
                                 question="Which store?", options=["redis", "postgres"])
    links_svc.create_link(db, a=ticket.id, b=wait.id, type_="dependency", project_id="core")
    items_svc.update_item(db, ticket.id, status="blocked")
    db.refresh(ticket)
    return prd, ticket, wait


def test_agent_answer_shows_source_quote_and_restart_command_on_the_row(client, auth, db):
    from app.services import waits as waits_svc

    _, ticket, wait = _answered_pair(db)
    waits_svc.answer_wait(db, wait.id, choice="redis", agent="core-A7",
                          source={"kind": "session", "quote": "the user said redis"})
    f = _feed(client, auth)
    row = next(r for r in f["rows"] if r["id"] == wait.key)
    assert row["kind"] == "answered" and row["answer"] == "redis"
    assert row["answered_by"] == "answered from session by core-A7"
    assert row["by"]["agent"] == "core-A7" and row["source"]["kind"] == "session"
    assert row["quote"] == "the user said redis"
    key = ticket.prd_key
    assert row["drain"] == {"status": "not_live", "prd": key,
                            "message": f"No drain is running for {key}.",
                            "command": f"gban fleet until --prd {key}"}
    # Listed, but not waiting on anyone: it adds to no tab's count.
    assert f["counts"]["all"] == sum(r["kind"] != "answered" for r in f["rows"])
    assert f["waiting_on_person"] == 0


def test_person_answer_shows_the_person_and_a_live_drain(client, auth, db):
    from app.models import Agent, Enrolment, utcnow
    from app.services import waits as waits_svc

    prd, _, wait = _answered_pair(db)
    db.add(Enrolment(id="enr-tri-live", project_id="core", code_hash="x", role="worker",
                     prd_id=prd.id, expires_at=utcnow() + timedelta(hours=1)))
    db.flush()
    db.add(Agent(id="core-A88", project_id="core", number=88, enrolment_id="enr-tri-live",
                 state="working", last_seen_at=utcnow()))
    db.commit()
    r = client.post(f"/api/items/{wait.key}/answer", headers=auth, json={"option": "postgres"})
    assert r.status_code == 200, r.text
    row = next(r for r in _feed(client, auth)["rows"] if r["id"] == wait.key)
    assert row["answered_by"].startswith("answered by ") and row["by"]["agent"] is None
    assert row["quote"] is None and row["source"] is None
    assert row["drain"]["status"] == "live"
    assert row["drain"]["message"].startswith("A drain is running for ")


def test_answered_row_leaves_the_feed_after_the_window(client, auth, db):
    from app.services import triage as triage_svc
    from app.services import waits as waits_svc

    _, _, wait = _answered_pair(db)
    waits_svc.answer_wait(db, wait.id, choice="redis")
    assert any(r["id"] == wait.key for r in _feed(client, auth)["rows"])
    db.refresh(wait)
    wait.done_at = wait.done_at - timedelta(seconds=triage_svc.ANSWERED_WINDOW_SECONDS + 60)
    db.commit()
    assert all(r["id"] != wait.key for r in _feed(client, auth)["rows"])
