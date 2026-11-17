"""Console team-triage decisions applied on the box (GRPH-1679)."""
import pytest
from sqlalchemy import select

from app.models import Event, Item, Request
from app.services import triage


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(client, auth, title, status="backlog"):
    r = client.post("/api/items", headers=auth, json={"title": title, "project_id": "core", "status": status})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _d(**kw):
    return {"id": "d1", "author": {"name": "Mo Manager"}, **kw}


def test_accept_item_moves_to_next_and_logs_console_author(client, auth, db):
    key = _item(client, auth, "Thing", "backlog")
    out = triage.apply_decision(db, _d(target=key, decision="accept", status="backlog"))
    assert out == {"id": "d1", "result": "applied"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Thing").one().status == "next"
    ev = db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()
    assert len(ev) == 1 and ev[0].actor_label == "Mo Manager"


def test_park_item_to_backlog(client, auth, db):
    key = _item(client, auth, "Parkme", "next")
    assert triage.apply_decision(db, _d(target=key, decision="park", status="next"))["result"] == "applied"
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Parkme").one().status == "backlog"


def test_moved_item_refused_and_unchanged(client, auth, db):
    key = _item(client, auth, "Moved", "next")
    out = triage.apply_decision(db, _d(target=key, decision="park", status="backlog"))
    assert out == {"id": "d1", "result": "refused", "reason": "item_moved"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Moved").one().status == "next"
    assert not db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()


def test_applying_twice_changes_nothing_second_time(client, auth, db):
    key = _item(client, auth, "Twice", "backlog")
    dec = _d(target=key, decision="accept", status="backlog")
    assert triage.apply_decision(db, dec)["result"] == "applied"
    assert triage.apply_decision(db, dec)["result"] == "applied"
    assert len(db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()) == 1


def test_assign_team_and_duplicate_links(client, auth, db):
    a = _item(client, auth, "A one")
    b = _item(client, auth, "B two")
    assert triage.apply_decision(db, _d(id="t", target=a, decision="assign_team", team="core"))["result"] == "applied"
    db.expire_all()
    assert "team:core" in db.query(Item).filter(Item.title == "A one").one().tags
    assert triage.apply_decision(db, _d(id="u", target=a, decision="duplicate_of", duplicate_of={"id": b}))["result"] == "applied"
    assert triage.apply_decision(db, _d(id="v", target=b, decision="duplicate_of",
                                        duplicate_of={"deployment": "other-box", "id": "X-9"}))["result"] == "applied"
    from app.models import Link
    assert {(l.a, l.b) for l in db.query(Link).filter(Link.reason.like("duplicate_of%"))} >= {(a, b)}
    assert any(l.b == "other-box:X-9" for l in db.query(Link))


def test_request_accept_and_decline(client, auth, db):
    from app.services import requests as req_svc

    r1 = Request(id="req_1", project_id="core", number=901, type="feature", title="R1", status="new")
    r2 = Request(id="req_2", project_id="core", number=902, type="bug", title="R2", status="new")
    db.add_all([r1, r2]); db.commit()
    assert triage.apply_decision(db, _d(id="a", target=r1.key, decision="accept", status="new"))["result"] == "applied"
    assert triage.apply_decision(db, _d(id="b", target=r2.key, decision="decline", status="new", reason="no"))["result"] == "applied"
    db.expire_all()
    assert db.get(Request, "req_1").status == "linked"
    assert db.get(Request, "req_1").linked_to
    assert db.get(Request, "req_2").status == "declined"
    assert db.query(Item).filter(Item.id == db.get(Request, "req_1").linked_to).one().status == "next"
