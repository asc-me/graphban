"""Console team-triage decisions applied on the box (GRPH-1679).

Every decision here carries the console's OBJECT `target` (GRPH-1927): `{kind: "item", item_id}`
naming the key this box published in its digest, or `{kind: "request", ...}` for a public request
the console routed here. The pull/report wire itself — one `POST .../applied` per decision, and a
fake that refuses what the real console refuses — lives in `test_triage_console_wire.py`.
"""
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
    return r.json()["id"]  # `ItemOut.id` is the item's key — the value the digest publishes


def _at(key):
    """The console's item target: an object, never the bare key the box used to stringify."""
    return {"kind": "item", "item_id": key}


def _d(**kw):
    return {"id": "d1", "author": {"name": "Mo Manager"}, **kw}


def test_accept_item_moves_to_next_and_logs_console_author(client, auth, db):
    key = _item(client, auth, "Thing", "backlog")
    out = triage.apply_decision(db, _d(target=_at(key), decision="accept", status="backlog"))
    assert out == {"id": "d1", "result": "applied"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Thing").one().status == "next"
    ev = db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()
    assert len(ev) == 1 and ev[0].actor_label == "Mo Manager"


def test_park_item_to_backlog(client, auth, db):
    key = _item(client, auth, "Parkme", "next")
    assert triage.apply_decision(
        db, _d(target=_at(key), decision="park", status="next"))["result"] == "applied"
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Parkme").one().status == "backlog"


def test_moved_item_refused_and_unchanged(client, auth, db):
    key = _item(client, auth, "Moved", "next")
    out = triage.apply_decision(db, _d(target=_at(key), decision="park", status="backlog"))
    assert out == {"id": "d1", "result": "refused", "reason": "item_moved"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Moved").one().status == "next"
    assert not db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()


def test_applying_twice_changes_nothing_second_time(client, auth, db):
    key = _item(client, auth, "Twice", "backlog")
    dec = _d(target=_at(key), decision="accept", status="backlog")
    assert triage.apply_decision(db, dec)["result"] == "applied"
    assert triage.apply_decision(db, dec)["result"] == "applied"
    assert len(db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()) == 1


def test_assign_team_and_duplicate_links(client, auth, db):
    a = _item(client, auth, "A one")
    b = _item(client, auth, "B two")
    assert triage.apply_decision(db, _d(id="t", target=_at(a), decision="assign_team", team="core",
                                        status="backlog"))["result"] == "applied"
    db.expire_all()
    assert "team:core" in db.query(Item).filter(Item.title == "A one").one().tags
    assert triage.apply_decision(db, _d(id="u", target=_at(a), decision="duplicate_of",
                                        duplicate_of={"id": b},
                                        status="backlog"))["result"] == "applied"
    assert triage.apply_decision(db, _d(id="v", target=_at(b), decision="duplicate_of",
                                        status="backlog",
                                        duplicate_of={"deployment": "other-box",
                                                      "id": "X-9"}))["result"] == "applied"
    from app.models import Link
    assert {(l.a, l.b) for l in db.query(Link).filter(Link.reason.like("duplicate_of%"))} >= {(a, b)}
    assert any(l.b == "other-box:X-9" for l in db.query(Link))


def test_request_accept_and_decline(client, auth, db):
    r1 = Request(id="req_1", project_id="core", number=901, type="feature", title="R1", status="new")
    r2 = Request(id="req_2", project_id="core", number=902, type="bug", title="R2", status="new")
    db.add_all([r1, r2]); db.commit()
    assert triage.apply_decision(
        db, _d(id="a", target=_at(r1.key), decision="accept", status="new"))["result"] == "applied"
    assert triage.apply_decision(db, _d(id="b", target=_at(r2.key), decision="decline",
                                        status="new", reason="no"))["result"] == "applied"
    db.expire_all()
    assert db.get(Request, "req_1").status == "linked"
    assert db.get(Request, "req_1").linked_to
    assert db.get(Request, "req_2").status == "declined"
    assert db.query(Item).filter(Item.id == db.get(Request, "req_1").linked_to).one().status == "next"


def test_a_console_payload_carries_no_status_so_it_applies(client, auth, db):
    """The console serves `{id, target, decision, note, decided_at}` — no status to check.

    Refusing that as `no_status` is what left every real console decision unapplied, so an
    absent status applies. The guarantee still bites on a payload that carries one, which is
    `test_moved_item_refused_and_unchanged` above.
    """
    key = _item(client, auth, "Nostatus", "backlog")
    out = triage.apply_decision(db, {"id": "d1", "target": _at(key), "decision": "accept",
                                     "note": "", "decided_at": "2026-11-24T09:00:00.000Z"})
    assert out == {"id": "d1", "result": "applied"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Nostatus").one().status == "next"
    # The console sends no `author` either, so the event names the surface, not a person.
    ev = db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).one()
    assert ev.actor_label == "console"


def test_a_target_outside_the_pulled_project_is_refused(client, auth, db):
    key = _item(client, auth, "Elsewhere", "backlog")
    out = triage.apply_decision(db, _d(target=_at(key), decision="accept", status="backlog"),
                                "some-other-project")
    assert out == {"id": "d1", "result": "refused", "reason": "wrong_project"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Elsewhere").one().status == "backlog"
    assert triage.apply_decision(
        db, _d(target=_at(key), decision="accept", status="backlog"), "core")["result"] == "applied"


def test_a_target_that_is_not_the_consoles_object_is_refused(client, auth, db):
    """The box's old shape — a bare key where the console sends an object — names nothing."""
    key = _item(client, auth, "Stringtarget", "backlog")
    out = triage.apply_decision(db, _d(target=key, decision="accept", status="backlog"))
    assert out == {"id": "d1", "result": "refused", "reason": "bad_decision"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Stringtarget").one().status == "backlog"
    assert not db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()


def test_sync_tail_reports_a_triage_transport_error(monkeypatch):
    """A transport error raised by the pull is surfaced in the sync result, not dropped."""
    from app.services import code_sync, triage

    def boom(db, cloud_url, api_key):
        raise ConnectionError("console unreachable")

    monkeypatch.setattr(triage, "pull_decisions", boom)

    class _DB:
        def rollback(self):
            pass

    out = code_sync._pull_triage_on_sync(_DB(), "https://console", "k")
    assert out == {"pulled": False, "error": "ConnectionError: console unreachable"}


def test_sync_tail_omits_triage_when_console_does_not_serve_it(monkeypatch):
    from app.services import code_sync, triage

    monkeypatch.setattr(
        triage, "pull_decisions", lambda db, cloud_url, api_key: {"pulled": False, "reason": "x"}
    )
    assert code_sync._pull_triage_on_sync(object(), "https://console", "k") is None
