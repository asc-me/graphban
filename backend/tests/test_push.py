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
    monkeypatch.setattr(push_svc, "_stored", None)  # no stored pair resolved either
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
    monkeypatch.setattr(push_svc, "_stored", None)  # e.g. the startup ensure step failed
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


# --- The generated, stored key pair (GRPH-1582) -------------------------------------------


@pytest.fixture()
def no_env_keys(monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "")
    monkeypatch.setattr(settings, "vapid_private_key", "")
    monkeypatch.setattr(settings, "vapid_subject", "mailto:admin@localhost")
    monkeypatch.setattr(push_svc, "_stored", None)


def _empty_table(db):
    from app.models import VapidKey

    db.query(VapidKey).delete()
    db.commit()


def _b64decode(s: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_startup_generates_a_pair_from_an_empty_table(db, no_env_keys):
    _empty_table(db)
    assert push_svc.status()["configured"] is False
    push_svc.ensure_keys(db)
    got = push_svc.status()
    assert got["configured"] is True
    raw = _b64decode(got["public_key"])
    assert len(raw) == 65 and raw[0] == 0x04  # uncompressed P-256 point
    from cryptography.hazmat.primitives.asymmetric import ec

    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)  # a real curve point


def test_restart_reuses_the_stored_pair(db, no_env_keys, monkeypatch):
    """Sabotage: drop the read-the-stored-row-first branch and the second start generates a
    pair it was never meant to make; generation is refused here, so push comes up off."""
    _empty_table(db)
    push_svc.ensure_keys(db)
    first = push_svc.status()["public_key"]
    push_svc._stored = None  # a new process: nothing cached

    def must_not_generate():
        raise AssertionError("a restart generated a new pair instead of reusing the stored one")

    monkeypatch.setattr(push_svc, "_generate", must_not_generate)
    push_svc.ensure_keys(db)
    assert push_svc.status()["public_key"] == first
    from app.models import VapidKey

    assert db.query(VapidKey).count() == 1


def test_the_stored_pair_is_what_a_running_app_serves(client, auth, no_env_keys):
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app):  # the lifespan's ensure step, as a real start runs it
        pass
    got = client.get("/api/me/push", headers=auth).json()
    assert got["configured"] is True and len(_b64decode(got["public_key"])) == 65


def test_env_keys_win_and_the_table_stays_empty(db, monkeypatch):
    from app.models import VapidKey

    _empty_table(db)
    monkeypatch.setattr(settings, "vapid_public_key", "BENVPUB")
    monkeypatch.setattr(settings, "vapid_private_key", "ENVPRIV")
    monkeypatch.setattr(push_svc, "_stored", None)
    push_svc.ensure_keys(db)
    assert push_svc.status() == {"configured": True, "public_key": "BENVPUB"}
    assert db.query(VapidKey).count() == 0


def test_the_stored_private_key_is_encrypted_at_rest(db, no_env_keys, monkeypatch):
    from app.models import VapidKey

    monkeypatch.setattr(settings, "secret_encryption_key", "test-secret-for-vapid")
    _empty_table(db)
    public, private = push_svc.ensure_keys(db)
    db.expire_all()
    row = db.get(VapidKey, "vapid")
    assert row.public_key == public
    assert row.private_key.startswith("enc::") and private not in row.private_key
    push_svc._stored = None
    assert push_svc.ensure_keys(db) == (public, private)  # and it decrypts back on restart


def test_a_generation_failure_leaves_push_off_and_does_not_raise(db, no_env_keys, monkeypatch):
    from app.models import VapidKey

    _empty_table(db)

    def boom():
        raise RuntimeError("no entropy today")

    monkeypatch.setattr(push_svc, "_generate", boom)
    assert push_svc.ensure_keys(db) is None
    assert push_svc.status() == {"configured": False, "public_key": None}
    assert db.query(VapidKey).count() == 0


def test_concurrent_first_starts_end_with_one_row_and_one_pair(db, no_env_keys, monkeypatch):
    """Both workers read an empty table, both generate, both insert: the loser re-reads the
    winner. Sabotage: drop the IntegrityError re-read and the loser ends with no pair."""
    import threading

    from app.models import VapidKey

    _empty_table(db)
    real = push_svc._generate
    barrier = threading.Barrier(2, timeout=10)

    def generate_after_both_read():
        pair = real()
        barrier.wait()  # both have seen no row and made their own pair before either inserts
        return pair

    monkeypatch.setattr(push_svc, "_generate", generate_after_both_read)
    results: list = [None, None]

    def start(i):
        s = SessionLocal()
        try:
            results[i] = push_svc.ensure_keys(s)
        finally:
            s.close()

    threads = [threading.Thread(target=start, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert results[0] is not None and results[0] == results[1]
    db.expire_all()
    assert db.query(VapidKey).count() == 1
    assert db.get(VapidKey, "vapid").public_key == results[0][0]


def test_send_signs_with_the_stored_key_and_the_first_users_email(db, no_env_keys, monkeypatch):
    from app.models import User

    _empty_table(db)
    _, private = push_svc.ensure_keys(db)
    seen = []

    def fake(*, subscription_info, data=None, vapid_private_key=None, vapid_claims=None, **kw):
        seen.append((vapid_private_key, vapid_claims))
        return SimpleNamespace(status_code=201)

    monkeypatch.setattr(push_svc, "webpush", fake)
    _sub(db, _owner(db))
    _, wait = _pair(db)
    waits.hand_to_person(db, wait)
    first = db.query(User).order_by(User.created_at, User.id).first()
    assert seen == [(private, {"sub": f"mailto:{first.email}"})]


def test_an_env_subject_is_used_as_given(db, no_env_keys, monkeypatch):
    monkeypatch.setattr(settings, "vapid_subject", "https://ops.example")
    assert push_svc._subject(db) == "https://ops.example"
