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
    monkeypatch.setattr(push_svc, "_stored", None)
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
    monkeypatch.setattr(push_svc, "_stored", None)
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


# --- GRPH-1582: server-generated VAPID key pair ---------------------------------------------

import base64
import threading

from app.models import User, VapidKey


@pytest.fixture()
def no_env_keys(monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "")
    monkeypatch.setattr(settings, "vapid_private_key", "")
    monkeypatch.setattr(push_svc, "_stored", None)
    yield
    push_svc._stored = None


def _pub_bytes(key):
    return base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))


def test_ensure_generates_a_p256_pair(db, no_env_keys):
    db.query(VapidKey).delete()
    db.commit()
    push_svc.ensure_keys(db)
    st = push_svc.status()
    assert st["configured"] is True
    raw = _pub_bytes(st["public_key"])
    assert len(raw) == 65 and raw[0] == 4


def test_ensure_twice_keeps_the_same_key(db, no_env_keys, monkeypatch):
    db.query(VapidKey).delete()
    db.commit()
    push_svc.ensure_keys(db)
    first = push_svc.status()["public_key"]
    push_svc._stored = None  # a restart
    monkeypatch.setattr(push_svc, "_generate", lambda: pytest.fail("regenerated instead of reading"))
    push_svc.ensure_keys(db)
    assert push_svc.status()["public_key"] == first
    assert db.query(VapidKey).count() == 1


def test_env_keys_win_and_leave_the_table_empty(db, monkeypatch):
    db.query(VapidKey).delete()
    db.commit()
    monkeypatch.setattr(settings, "vapid_public_key", "BENV")
    monkeypatch.setattr(settings, "vapid_private_key", "PENV")
    push_svc.ensure_keys(db)
    assert push_svc.status() == {"configured": True, "public_key": "BENV"}
    assert db.query(VapidKey).count() == 0


def test_private_key_is_encrypted_at_rest(db, no_env_keys, monkeypatch):
    monkeypatch.setattr(settings, "secret_encryption_key", "s3cret")
    db.query(VapidKey).delete()
    db.commit()
    push_svc.ensure_keys(db)
    row = db.query(VapidKey).one()
    assert row.private_key.startswith("enc::")
    assert push_svc._resolved()[1] and not push_svc._resolved()[1].startswith("enc::")


def test_generation_failure_leaves_push_off(db, no_env_keys, monkeypatch):
    db.query(VapidKey).delete()
    db.commit()

    def boom():
        raise RuntimeError("no entropy")

    monkeypatch.setattr(push_svc, "_generate", boom)
    push_svc.ensure_keys(db)  # must not raise
    assert push_svc.status() == {"configured": False, "public_key": None}


def test_concurrent_first_starts_end_with_one_row_and_one_key(db, no_env_keys, monkeypatch):
    db.query(VapidKey).delete()
    db.commit()
    real_read, results = push_svc._read, []
    barrier = threading.Barrier(2)

    def racing_read(s):
        out = real_read(s)
        if out is None:
            try:
                barrier.wait(timeout=2)  # both see an empty table, then both insert
            except threading.BrokenBarrierError:
                pass
        return out

    monkeypatch.setattr(push_svc, "_read", racing_read)

    def worker():
        s = SessionLocal()
        try:
            push_svc.ensure_keys(s)
            results.append(push_svc._stored)
        finally:
            s.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert db.query(VapidKey).count() == 1
    assert len(results) == 2 and results[0] == results[1] and results[0] is not None


def test_send_signs_with_the_stored_private_key(db, no_env_keys, monkeypatch):
    db.query(VapidKey).delete()
    db.commit()
    push_svc.ensure_keys(db)
    stored_priv = push_svc._stored[1]
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return SimpleNamespace(status_code=201)

    monkeypatch.setattr(push_svc, "webpush", fake)
    sub = PushSubscription(id="ps_x", user_id=_owner(db), endpoint="https://p/x", p256dh="a", auth="b")
    db.add(sub)
    db.commit()
    assert push_svc._send_one(db, sub, "{}") == 1
    assert seen["vapid_private_key"] == stored_priv
    first = db.query(User).order_by(User.created_at, User.id).first()
    assert seen["vapid_claims"]["sub"] == f"mailto:{first.email}"
