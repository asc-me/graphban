"""Browser push for Response Needed alerts (GRPH-1528). A fake stands in for the push service:
no test reaches a real one."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException

from app.config import settings
from app.db import SessionLocal
from app.models import Alert, Membership, PushSubscription
from app.services import alerts as alerts_svc
from app.services import items as items_svc
from app.services import links as links_svc
from app.services import push as push_svc
from app.services import waits


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def vapid(monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "BPUB")
    monkeypatch.setattr(settings, "vapid_private_key", "PRIV")


class FakePushService:
    def __init__(self, status=201):
        self.status, self.calls = status, []

    def __call__(self, *, subscription_info, data=None, **kw):
        self.calls.append((subscription_info, json.loads(data)))
        if self.status >= 400:
            raise WebPushException("push failed", response=SimpleNamespace(status_code=self.status))
        return SimpleNamespace(status_code=self.status)


def _pair(db):
    ticket = items_svc.create_item(db, project_id="core", title="feature", status="in_progress")
    wait = items_svc.create_item(db, project_id="core", title="which?", status="blocked",
                                 question="Which store?\nsecond line", options=["a", "b"])
    links_svc.create_link(db, a=ticket.id, b=wait.id, type_="dependency", project_id="core")
    items_svc.update_item(db, ticket.id, status="blocked")
    return ticket, wait


def _owner(db):
    m = db.query(Membership).filter(Membership.project_id == "core", Membership.role == "owner").first()
    assert m is not None
    return m.user_id


def _sub(db, uid, endpoint="https://push.test/a"):
    push_svc.subscribe(db, uid, endpoint, "p256", "authsecret")


def test_one_push_per_subscription_with_first_line_and_ticket(db, vapid, monkeypatch):
    fake = FakePushService()
    monkeypatch.setattr(push_svc, "webpush", fake)
    uid = _owner(db)
    _sub(db, uid, "https://push.test/a")
    _sub(db, uid, "https://push.test/b")
    ticket, wait = _pair(db)
    waits.hand_to_person(db, wait)
    assert len(fake.calls) == 2
    payload = fake.calls[0][1]
    assert payload["body"] == "Which store?" and payload["ticket"] == ticket.key
    assert payload["url"] == f"/triage?item={ticket.key}"
    assert all(s.last_ok_at for s in db.query(PushSubscription))


def test_no_subscription_attempts_nothing(db, vapid, monkeypatch):
    fake = FakePushService()
    monkeypatch.setattr(push_svc, "webpush", fake)
    _, wait = _pair(db)
    waits.hand_to_person(db, wait)
    assert fake.calls == [] and db.query(Alert).count() >= 1


def test_410_deletes_the_subscription(db, vapid, monkeypatch):
    monkeypatch.setattr(push_svc, "webpush", FakePushService(410))
    _sub(db, _owner(db))
    _, wait = _pair(db)
    assert waits.hand_to_person(db, wait)["handed"] is True
    assert db.query(PushSubscription).count() == 0
    assert db.query(Alert).count() >= 1


def test_500_keeps_the_subscription_marks_failed_and_alert_exists(db, vapid, monkeypatch):
    monkeypatch.setattr(push_svc, "webpush", FakePushService(500))
    _sub(db, _owner(db))
    _, wait = _pair(db)
    assert waits.hand_to_person(db, wait)["handed"] is True
    sub = db.query(PushSubscription).one()
    assert sub.failed_at is not None
    assert db.query(Alert).filter(Alert.item_id == wait.id).count() == 1


def test_unexpected_error_never_fails_the_hand_off(db, vapid, monkeypatch):
    def boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(push_svc, "webpush", boom)
    _sub(db, _owner(db))
    _, wait = _pair(db)
    assert waits.hand_to_person(db, wait)["handed"] is True
    assert db.query(PushSubscription).one().failed_at is not None


def test_no_vapid_keys_sends_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "")
    fake = FakePushService()
    monkeypatch.setattr(push_svc, "webpush", fake)
    _sub(db, _owner(db))
    _, wait = _pair(db)
    waits.hand_to_person(db, wait)
    assert fake.calls == []


def test_rest_subscribe_status_unsubscribe(client, auth, db, vapid):
    assert client.get("/api/me/push", headers=auth).json() == {"configured": True, "public_key": "BPUB"}
    body = {"endpoint": "https://push.test/x", "keys": {"p256dh": "k", "auth": "a"}}
    assert client.post("/api/me/push/subscribe", json=body, headers=auth).status_code == 200
    assert client.post("/api/me/push/subscribe", json=body, headers=auth).status_code == 200
    assert db.query(PushSubscription).count() == 1
    client.post("/api/me/push/unsubscribe", json={"endpoint": body["endpoint"]}, headers=auth)
    db.expire_all()
    assert db.query(PushSubscription).count() == 0


def test_rest_unconfigured_says_so_and_refuses_subscribe(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "")
    assert client.get("/api/me/push", headers=auth).json()["configured"] is False
    body = {"endpoint": "https://push.test/x", "keys": {"p256dh": "k", "auth": "a"}}
    assert client.post("/api/me/push/subscribe", json=body, headers=auth).status_code == 503


def test_hosted_push_links_the_triage_row_by_project_tag(db, monkeypatch):
    from app.models import Project

    tag = db.get(Project, "core").tag
    assert push_svc._triage_url(db, "core", "GRPH-7") == "/triage?item=GRPH-7"
    monkeypatch.setattr(settings, "hosted_mode", True)
    assert push_svc._triage_url(db, "core", "GRPH-7") == f"/p/{tag}/triage?item=GRPH-7"


def test_no_push_to_a_user_who_lost_project_access(db, vapid, monkeypatch):
    fake = FakePushService()
    monkeypatch.setattr(push_svc, "webpush", fake)
    uid = _owner(db)
    _sub(db, uid)
    _, wait = _pair(db)
    alerts = alerts_svc.write_for_wait(db, wait, [uid])
    db.query(Membership).filter(Membership.user_id == uid, Membership.project_id == "core").delete()
    db.commit()
    assert push_svc.send_for_alerts(db, [a.id for a in alerts]) == 0
    assert fake.calls == []
