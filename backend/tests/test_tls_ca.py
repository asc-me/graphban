"""The box's certificate authority (GRPH-1634)."""
import ipaddress
import threading
from datetime import timedelta

import pytest
from cryptography import x509

from app.config import settings
from app.db import SessionLocal
from app.models import TlsAuthority, TlsName
from app.services import tls_ca, tls_names


@pytest.fixture(autouse=True)
def fake_net(monkeypatch):
    monkeypatch.setattr(tls_names, "interface_addresses", lambda: ["100.101.102.103"])
    monkeypatch.setattr(tls_names.socket, "gethostname", lambda: "ubuntu-srv")


@pytest.fixture()
def db(client):
    s = SessionLocal()
    s.query(TlsAuthority).delete()
    s.query(TlsName).delete()
    s.commit()
    try:
        yield s
    finally:
        s.close()


def test_concurrent_init_creates_one_ca(client, db):
    def go():
        s = SessionLocal()
        try:
            tls_ca.ensure_ca(s)
        finally:
            s.close()

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    db.expire_all()
    assert db.query(TlsAuthority).count() == 1


def test_ca_key_encrypted_at_rest(db, monkeypatch):
    monkeypatch.setattr(settings, "secret_encryption_key", "unit-test-secret")
    row = tls_ca.ensure_ca(db)
    assert row.ca_key.startswith("enc::")
    assert "PRIVATE KEY" not in row.ca_key
    tls_ca.sync_leaf(db)
    db.refresh(row)
    assert row.leaf_key.startswith("enc::")


def test_name_constraints_include_interface_address(db):
    tls_names.seed_auto_names(db)
    row = tls_ca.ensure_ca(db)
    nc = x509.load_pem_x509_certificate(row.ca_cert.encode()).extensions.get_extension_for_class(
        x509.NameConstraints).value
    ips = [g.value for g in nc.permitted_subtrees if isinstance(g, x509.IPAddress)]
    dns = [g.value for g in nc.permitted_subtrees if isinstance(g, x509.DNSName)]
    assert ipaddress.ip_network("100.101.102.103/32") in ips
    assert ipaddress.ip_network("192.168.0.0/16") in ips
    assert ".lan" in dns and "ubuntu-srv" in dns


def test_leaf_covers_names_and_is_signed_by_ca(db):
    tls_names.seed_auto_names(db)
    row = tls_ca.ensure_ca(db)
    assert tls_ca.sync_leaf(db) == "issued"
    db.refresh(row)
    leaf = x509.load_pem_x509_certificate(row.leaf_cert.encode())
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "ubuntu-srv" in san.get_values_for_type(x509.DNSName)
    assert ipaddress.ip_address("100.101.102.103") in san.get_values_for_type(x509.IPAddress)
    assert leaf.issuer == x509.load_pem_x509_certificate(row.ca_cert.encode()).subject
    assert tls_ca.sync_leaf(db) == "current"


def test_two_new_names_in_window_one_reissue(db):
    tls_names.seed_auto_names(db)
    tls_ca.sync_leaf(db)
    first = db.get(TlsAuthority, "box").leaf_issued_at
    tls_names.add_seen_host(db, "a.lan")
    assert tls_ca.sync_leaf(db) == "debounced"
    tls_names.add_seen_host(db, "b.lan")
    assert tls_ca.sync_leaf(db) == "debounced"
    db.expire_all()
    assert db.get(TlsAuthority, "box").leaf_issued_at == first
    later = tls_ca._now() + timedelta(seconds=settings.tls_reissue_debounce_seconds + 5)
    assert tls_ca.sync_leaf(db, now=later) == "issued"
    assert tls_ca.sync_leaf(db, now=later) == "current"
    db.expire_all()
    assert set(__import__("json").loads(db.get(TlsAuthority, "box").leaf_names)) >= {"a.lan", "b.lan"}


def test_renews_before_expiry(db):
    tls_names.seed_auto_names(db)
    tls_ca.sync_leaf(db)
    soon = tls_ca._now() + timedelta(days=tls_ca.LEAF_DAYS - 5)
    assert tls_ca.sync_leaf(db, now=soon) == "issued"


def test_publish_writes_leaf_and_key(db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "tls_dir", str(tmp_path / "tls"))
    tls_names.seed_auto_names(db)
    tls_ca.sync_leaf(db)
    assert "BEGIN CERTIFICATE" in (tmp_path / "tls" / "box.crt").read_text()
    assert "PRIVATE KEY" in (tmp_path / "tls" / "box.key").read_text()


def test_api_never_returns_a_private_key_and_ask_endpoint(client, db, auth):
    tls_names.seed_auto_names(db)
    tls_ca.sync_leaf(db)
    bodies = [client.get("/api/https/ca.crt").text, client.get("/api/https", headers=auth).text]
    r = client.post("/api/https/names", json={"name": "localhost"}, headers=auth)
    assert r.status_code == 422
    bodies.append(r.text)
    r = client.post("/api/https/names", json={"name": "extra.example.org"}, headers=auth)
    assert r.status_code == 200
    bodies.append(r.text)
    assert client.delete("/api/https/names/ubuntu-srv", headers=auth).status_code == 422
    assert client.delete("/api/https/names/extra.example.org", headers=auth).status_code == 200
    for b in bodies:
        assert "PRIVATE KEY" not in b
    assert "BEGIN CERTIFICATE" in bodies[0]
    assert client.get("/api/https/ask", params={"domain": "ubuntu-srv"}).status_code == 200
    assert client.get("/api/https/ask", params={"domain": "bank.com"}).status_code == 404
    assert client.get("/api/https/ask", params={"domain": "ubuntu-srv"},
                      headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 403
