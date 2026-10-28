"""Request triage actions and undo (GRPH-1136 / PRD-60 D6)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Event, Item, Request, RequestVote
from app.services import email as email_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _mk(client, auth, title="Dark mode"):
    r = client.post("/api/requests", headers=auth,
                    json={"type": "feature", "title": title, "project_id": "core"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _vote(db, rid, *keys):
    req = db.get(Request, rid)
    for k in keys:
        db.add(RequestVote(request_id=req.id, voter_key=k))
    req.votes += len(keys)
    db.commit()


def _events(db, action):
    return db.query(Event).filter(Event.action == action).count()


def test_accept_to_next_and_undo(client, auth, db):
    rid = _mk(client, auth)
    r = client.post(f"/api/requests/{rid}/accept", headers=auth, json={"status": "next"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item"]["status"] == "next" and body["request"]["status"] == "linked"
    assert _events(db, "request_accept") == 1
    u = client.post(f"/api/requests/{rid}/undo", headers=auth, json={"action_id": body["action_id"]})
    assert u.status_code == 200, u.text
    assert u.json()["status"] == "new" and u.json()["linked_to"] is None
    db.expire_all()
    assert db.get(Item, body["item"]["id"]) is None
    assert _events(db, "request_undo") == 1


def test_accept_rejects_bad_status(client, auth):
    rid = _mk(client, auth)
    assert client.post(f"/api/requests/{rid}/accept", headers=auth, json={"status": "done"}).status_code == 422


def test_undo_accept_refused_after_item_edit(client, auth, db):
    rid = _mk(client, auth)
    body = client.post(f"/api/requests/{rid}/accept", headers=auth, json={}).json()
    item = db.get(Item, body["item"]["id"])
    item.title = "edited by someone else"
    db.commit()
    u = client.post(f"/api/requests/{rid}/undo", headers=auth, json={"action_id": body["action_id"]})
    assert u.status_code == 409 and "edited" in u.json()["detail"]
    db.expire_all()
    assert db.get(Item, item.id) is not None
    assert db.get(Request, db.get(Request, rid).id).status == "linked"


def test_undo_refused_after_request_changed(client, auth, db):
    rid = _mk(client, auth)
    d = client.post(f"/api/requests/{rid}/decline", headers=auth, json={"reason": "no"}).json()
    req = db.get(Request, rid)
    req.status = "triaging"
    db.commit()
    u = client.post(f"/api/requests/{rid}/undo", headers=auth, json={"action_id": d["action_id"]})
    assert u.status_code == 409


def test_decline_notifies_and_undoes(client, auth, db):
    rid = _mk(client, auth)
    req = db.get(Request, rid)
    req.meta = {"account": {"email": "sub@example.com"}}
    db.commit()
    email_svc.outbox.clear()
    d = client.post(f"/api/requests/{rid}/decline", headers=auth, json={"reason": "out of scope"})
    assert d.json()["status"] == "declined"
    assert [m.to for m in email_svc.outbox] == ["sub@example.com"]
    assert _events(db, "request_decline") == 1
    u = client.post(f"/api/requests/{rid}/undo", headers=auth, json={"action_id": d.json()["action_id"]})
    assert u.json()["status"] == "new"


def test_decline_without_email_still_writes_event(client, auth, db):
    rid = _mk(client, auth)
    assert client.post(f"/api/requests/{rid}/decline", headers=auth, json={}).status_code == 200
    assert _events(db, "request_decline") == 1


def test_snooze_hides_from_triage_until_date(client, auth, db):
    rid = _mk(client, auth)
    until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    s = client.post(f"/api/requests/{rid}/snooze", headers=auth, json={"until": until})
    assert s.status_code == 200 and s.json()["status"] == "snoozed"
    ids = lambda: [r["request"]["id"] for r in client.get("/api/requests/triage?project_id=core", headers=auth).json()]
    assert rid not in ids()
    req = db.get(Request, rid)
    req.snoozed_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert rid in ids()
    past = client.post(f"/api/requests/{rid}/snooze", headers=auth,
                       json={"until": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
    assert past.status_code == 409


def test_snooze_undo_restores(client, auth):
    rid = _mk(client, auth)
    until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    s = client.post(f"/api/requests/{rid}/snooze", headers=auth, json={"until": until}).json()
    u = client.post(f"/api/requests/{rid}/undo", headers=auth, json={"action_id": s["action_id"]}).json()
    assert u["status"] == "new" and u["snoozed_until"] is None


def test_merge_votes_union_and_undo(client, auth, db):
    a, b = _mk(client, auth, "A"), _mk(client, auth, "B")
    _vote(db, a, "v1", "v2")
    _vote(db, b, "v2", "v3")
    m = client.post(f"/api/requests/{a}/merge", headers=auth, json={"target_id": b})
    assert m.status_code == 200, m.text
    assert m.json()["status"] == "merged" and m.json()["votes"] == 0
    db.expire_all()
    assert db.get(Request, b).votes == 3
    keys = {v.voter_key for v in db.query(RequestVote).filter(RequestVote.request_id == b)}
    assert keys == {"v1", "v2", "v3"}
    assert _events(db, "request_merge") == 1
    u = client.post(f"/api/requests/{a}/undo", headers=auth, json={"action_id": m.json()["action_id"]})
    assert u.status_code == 200 and u.json()["votes"] == 2 and u.json()["status"] == "new"
    db.expire_all()
    assert db.get(Request, b).votes == 2
    assert {v.voter_key for v in db.query(RequestVote).filter(RequestVote.request_id == b)} == {"v2", "v3"}
    assert {v.voter_key for v in db.query(RequestVote).filter(RequestVote.request_id == a)} == {"v1", "v2"}


def test_merge_follows_chain_and_refuses_cycle(client, auth, db):
    a, b, c = _mk(client, auth, "A"), _mk(client, auth, "B"), _mk(client, auth, "C")
    assert client.post(f"/api/requests/{b}/merge", headers=auth, json={"target_id": c}).status_code == 200
    m = client.post(f"/api/requests/{a}/merge", headers=auth, json={"target_id": b})
    assert m.status_code == 200
    db.expire_all()
    assert db.get(Request, a).merged_into == db.get(Request, c).id
    # c is the end of the chain; merging it into a (merged -> b -> c) loops back to c.
    cyc = client.post(f"/api/requests/{c}/merge", headers=auth, json={"target_id": a})
    assert cyc.status_code == 409 and "cycle" in cyc.json()["detail"]
    self_ = client.post(f"/api/requests/{c}/merge", headers=auth, json={"target_id": c})
    assert self_.status_code == 409


def test_merge_into_item_and_public_pointer(client, auth, db):
    rid = _mk(client, auth)
    it = client.post("/api/items", headers=auth, json={"title": "dup", "project_id": "core"}).json()
    m = client.post(f"/api/requests/{rid}/merge", headers=auth, json={"target_id": it["id"]})
    assert m.status_code == 200 and m.json()["merged_into"] == it["id"]
    from app.services import requests as req_svc
    assert req_svc.merged_target_key(db, db.get(Request, rid)) == it["id"]
