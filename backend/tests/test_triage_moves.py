"""Cross-box moves on the box (GRPH-1979): a routed accept carrying an assignee reports
`created_item_id`, and `moved` closes the source item through the `moved` resolution."""
import pytest
from sqlalchemy import select

from app.models import Event, Item
from app.services import triage
from tests.test_triage_console_wire import _Console, _decision, _did, _item, _project_name, _writer
from tests.test_triage_console_wire import db  # noqa: F401

REQ = {"kind": "request", "request_id": "11111111-2222-4333-8444-555555555555",
       "title": "Routed here", "body": "b"}


def _routed(n, assignee=None):
    d = _decision(_did(n), dict(REQ, title=f"Routed {n}"), "accept")
    if assignee is not None:
        d["assignee"] = assignee
    return d


def _moved(n, key, to=None):
    d = _decision(_did(n), {"kind": "item", "item_id": key}, "moved")
    d["to"] = to or {"box_label": "other-box", "item_id": "OB-9"}
    return d


def _pull(db, monkeypatch, decisions):
    name = _project_name(db)
    console = _Console({name: decisions}).install(monkeypatch)
    return triage.pull_decisions(db, cloud_url="https://c", api_key="k"), console


def test_routed_accept_with_assignee_assigns_and_reports_created_item_id(client, auth, db, monkeypatch):
    _writer(db, "u_ada", "Ada")
    out, console = _pull(db, monkeypatch, [_routed(1, {"id": "u_ada"})])
    assert out["applied"] == 1 and console.refused == []
    db.expire_all()
    made = db.query(Item).filter(Item.title == "Routed 1").one()
    assert made.assignee == "u-ada"
    assert console.posts[0][1]["created_item_id"] == made.key


def test_retried_decision_reports_the_same_created_item_id(client, auth, db, monkeypatch):
    _, first = _pull(db, monkeypatch, [_routed(2)])
    _, second = _pull(db, monkeypatch, [_routed(2)])
    key = first.posts[0][1]["created_item_id"]
    assert key and second.posts[0][1]["created_item_id"] == key
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Routed 2").count() == 1


def test_invalid_assignee_is_refused_and_creates_nothing(client, auth, db, monkeypatch):
    _writer(db, "u_reader", "R", access="read")
    out, console = _pull(db, monkeypatch, [_routed(3, {"id": "u_nobody"}),
                                           _routed(4, {"id": "u_reader"})])
    assert out["refused"] == 2
    assert [b["reason"] for _, b in console.posts] == ["not_assignable"] * 2
    db.expire_all()
    assert db.query(Item).filter(Item.title.in_(["Routed 3", "Routed 4"])).count() == 0


def test_moved_closes_done_not_delivered_with_a_note(client, auth, db, monkeypatch):
    key = _item(client, auth, "Move me")
    out, console = _pull(db, monkeypatch, [_moved(5, key)])
    assert out["applied"] == 1
    assert "created_item_id" not in console.posts[0][1]
    db.expire_all()
    it = db.query(Item).filter(Item.title == "Move me").one()
    assert it.status == "done" and not it.delivered
    assert it.resolution["kind"] == "moved"
    assert it.resolution["to"] == {"box_label": "other-box", "item_id": "OB-9"}
    assert "other-box" in it.resolution["reason"] and "OB-9" in it.resolution["reason"]


def test_moved_refusals(client, auth, db, monkeypatch):
    done = _item(client, auth, "Done one")
    wait = _item(client, auth, "Typed wait")
    held = _item(client, auth, "Held")
    db.expire_all()
    db.query(Item).filter(Item.title == "Done one").one().status = "done"
    db.query(Item).filter(Item.title == "Typed wait").one().tags = ["wait:access"]
    db.query(Item).filter(Item.title == "Held").one().claimed_by = "agent-1"
    db.commit()
    out, console = _pull(db, monkeypatch, [_moved(6, done), _moved(7, wait), _moved(8, held)])
    assert [b["reason"] for _, b in console.posts] == ["already_done", "typed_wait", "claimed"]
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Held").one().status != "done"


def test_stale_status_moved_is_item_moved(client, auth, db, monkeypatch):
    key = _item(client, auth, "Stale")
    d = _moved(9, key)
    d["status"] = "done"
    _, console = _pull(db, monkeypatch, [d])
    assert console.posts[0][1]["reason"] == "item_moved"
