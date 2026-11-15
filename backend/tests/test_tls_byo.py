"""Bring your own certificate, Let's Encrypt, box address and the redirect switch (GRPH-1637)."""
import ipaddress
import os
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.config import settings
from app.db import SessionLocal
from app.models import TlsAuthority, TlsCustom, TlsName
from app.services import tls_byo, tls_ca, tls_names

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def make_cert(sans=("graphban.acme.com",), days=90, key=None, start=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    start = start or datetime.now(timezone.utc) - timedelta(minutes=5)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0])])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(start + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


@pytest.fixture(autouse=True)
def fake_net(monkeypatch, tmp_path):
    monkeypatch.setattr(tls_names, "interface_addresses", lambda: ["100.101.102.103"])
    monkeypatch.setattr(tls_names.socket, "gethostname", lambda: "ubuntu-srv")
    monkeypatch.setattr(tls_names, "resolves_to_box", lambda n: True)
    monkeypatch.setattr(settings, "tls_dir", str(tmp_path))
    monkeypatch.setattr(settings, "secret_encryption_key", "unit-test-secret")


@pytest.fixture()
def db(client):
    s = SessionLocal()
    for m in (TlsCustom, TlsAuthority, TlsName):
        s.query(m).delete()
    s.commit()
    try:
        yield s
    finally:
        s.close()


def codes(exc):
    return [r["code"] for r in exc.value.reasons]


def test_mismatched_key_refused_alone(db):
    cert, _ = make_cert()
    _, other = make_cert()
    with pytest.raises(tls_byo.CertificateRefused) as e:
        tls_byo.upload(db, cert, other, "graphban.acme.com")
    assert codes(e) == ["key_mismatch"]


def test_uncovered_address_refused_alone(db):
    cert, key = make_cert()
    with pytest.raises(tls_byo.CertificateRefused) as e:
        tls_byo.upload(db, cert, key, "other.acme.com")
    assert codes(e) == ["address_not_covered"]


def test_expired_certificate_refused_alone(db):
    cert, key = make_cert(days=10, start=datetime.now(timezone.utc) - timedelta(days=40))
    with pytest.raises(tls_byo.CertificateRefused) as e:
        tls_byo.upload(db, cert, key, "graphban.acme.com")
    assert codes(e) == ["expired"]
    assert "expired on" in e.value.reasons[0]["message"]


def test_all_three_reported_separately(db):
    cert, _ = make_cert(days=10, start=datetime.now(timezone.utc) - timedelta(days=40))
    _, other = make_cert()
    with pytest.raises(tls_byo.CertificateRefused) as e:
        tls_byo.upload(db, cert, other, "other.acme.com")
    assert sorted(codes(e)) == ["address_not_covered", "expired", "key_mismatch"]


def test_address_not_resolving_here_is_a_warning(db, monkeypatch):
    monkeypatch.setattr(tls_names, "resolves_to_box", lambda n: False)
    cert, key = make_cert()
    info = tls_byo.upload(db, cert, key, "graphban.acme.com")
    assert info["warnings"] == ["address_not_here"]
    assert tls_byo.custom_status(db)["state"] == "active"


def test_wildcard_accepted_with_warning(db):
    cert, key = make_cert(sans=("*.acme.com",))
    info = tls_byo.upload(db, cert, key, "graphban.acme.com")
    assert info["warnings"] == ["wildcard"]


def test_key_encrypted_in_db_and_never_returned(client, auth, db):
    cert, key = make_cert()
    r = client.post("/api/https/certificate", headers=auth, json={
        "certificate": cert, "key": key, "box_address": "graphban.acme.com"})
    assert r.status_code == 200, r.text
    assert "PRIVATE KEY" not in r.text and "cert_key" not in r.text
    assert "PRIVATE KEY" not in client.get("/api/https", headers=auth).text
    db.expire_all()
    row = db.get(TlsCustom, "box")
    assert row.cert_key.startswith("enc::") and "PRIVATE KEY" not in row.cert_key


def test_refusals_over_the_api(client, auth, db):
    cert, key = make_cert()
    r = client.post("/api/https/certificate", headers=auth, json={
        "certificate": cert, "key": key, "box_address": "nope.example.org"})
    assert r.status_code == 422
    assert [x["code"] for x in r.json()["detail"]["reasons"]] == ["address_not_covered"]


def test_valid_upload_is_served_without_restart(db):
    cert, key = make_cert()
    tls_byo.upload(db, cert, key, "graphban.acme.com")
    assert open(os.path.join(settings.tls_dir, "box.crt")).read() == cert
    assert open(os.path.join(settings.tls_dir, "box.key")).read() == key
    # an own-leaf re-sync does not overwrite the upload
    tls_ca.sync_leaf(db)
    assert open(os.path.join(settings.tls_dir, "box.crt")).read() == cert


def test_expiring_soon_state(db):
    cert, key = make_cert(days=20)
    tls_byo.upload(db, cert, key, "graphban.acme.com")
    assert tls_byo.custom_status(db)["state"] == "expiring-soon"


def test_expiry_switches_to_own_cert_and_turns_redirect_off(db):
    cert, key = make_cert(days=20)
    tls_byo.upload(db, cert, key, "graphban.acme.com")
    later = datetime.now(timezone.utc) + timedelta(days=21)
    st = tls_byo.custom_status(db, later)
    assert st["state"] == "expired" and st["redirect_on"] is False
    assert st["reason"].startswith("your certificate expired on ")
    assert tls_byo.redirect_target(db, "/") is None
    published = open(os.path.join(settings.tls_dir, "box.crt")).read()
    assert published != cert  # the box's own certificate again
    assert db.get(TlsCustom, "box").cert_key == ""


def test_stop_using_removes_rows_files_and_redirect(db):
    cert, key = make_cert()
    tls_byo.upload(db, cert, key, "graphban.acme.com")
    assert tls_byo.stop(db) is True
    db.expire_all()
    assert db.query(TlsCustom).count() == 0
    assert open(os.path.join(settings.tls_dir, "box.crt")).read() != cert
    assert "PRIVATE KEY" in open(os.path.join(settings.tls_dir, "box.key")).read()
    assert tls_byo.redirect_target(db, "/") is None
    assert tls_byo.stop(db) is False


def test_redirect_targets_box_address_not_host(client, auth, db):
    cert, key = make_cert()
    tls_byo.upload(db, cert, key, "graphban.acme.com")
    r = client.get("/api/https/redirect-check", follow_redirects=False,
                   headers={"Host": "evil.example", "X-Forwarded-Uri": "/x?y=1", "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 307
    assert r.headers["location"] == f"https://graphban.acme.com:{settings.web_https_port}/x?y=1"
    client.put("/api/https/redirect", headers=auth, json={"enabled": False})
    assert client.get("/api/https/redirect-check", follow_redirects=False).status_code == 204


def test_redirect_carries_published_https_port(db, monkeypatch):
    cert, key = make_cert()
    tls_byo.upload(db, cert, key, "graphban.acme.com")
    monkeypatch.setattr(settings, "web_https_port", 8443)
    assert tls_byo.redirect_target(db, "/") == "https://graphban.acme.com:8443/"
    monkeypatch.setattr(settings, "web_https_port", 443)
    assert tls_byo.redirect_target(db, "/") == "https://graphban.acme.com/"


def test_api_and_health_never_redirect(db):
    cert, key = make_cert()
    tls_byo.upload(db, cert, key, "graphban.acme.com")
    assert tls_byo.redirect_target(db, "/api/items") is None
    assert tls_byo.redirect_target(db, "/api") is None
    assert tls_byo.redirect_target(db, "/health") is None
    assert tls_byo.redirect_target(db, "/settings") is not None


def test_letsencrypt_probe_uses_published_port(db, monkeypatch):
    monkeypatch.setattr(settings, "web_https_port", 9443)
    tls_byo.request_letsencrypt(db, "graphban.acme.com", NOW)
    seen = []
    tls_byo.check_letsencrypt(db, NOW, probe=lambda d, p: seen.append(p))
    assert seen == [9443]


def test_redirect_unavailable_with_own_certificate(client, auth, db):
    tls_ca.sync_leaf(db)
    r = client.put("/api/https/redirect", headers=auth, json={"enabled": True})
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"][0]["code"] == "redirect_unavailable"
    assert client.get("/api/https/redirect-check", follow_redirects=False).status_code == 204


def test_letsencrypt_pending_failed_active(db):
    tls_byo.request_letsencrypt(db, "graphban.acme.com", NOW)
    assert os.path.exists(os.path.join(settings.tls_dir, tls_byo.ACME_SNIPPET))
    down = lambda d, p: "could not reach"
    tls_byo.check_letsencrypt(db, NOW + timedelta(minutes=1), probe=down)
    assert db.get(TlsCustom, "box").state == "pending"
    tls_byo.check_letsencrypt(db, NOW + timedelta(minutes=11), probe=down)
    row = db.get(TlsCustom, "box")
    assert row.state == "failed" and "could not reach" in row.reason
    tls_byo.check_letsencrypt(db, NOW + timedelta(minutes=12), probe=lambda d, p: None)
    assert db.get(TlsCustom, "box").state == "active"
    st = tls_byo.custom_status(db)
    assert st["state"] == "active" and st["redirect_on"] is True
    tls_byo.stop(db)
    assert not os.path.exists(os.path.join(settings.tls_dir, tls_byo.ACME_SNIPPET))


def test_letsencrypt_refuses_addresses(db):
    for bad in ("192.168.1.5", "box.local", "singlelabel"):
        with pytest.raises(tls_byo.CertificateRefused):
            tls_byo.request_letsencrypt(db, bad)
