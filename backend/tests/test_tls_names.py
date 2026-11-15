"""The box certificate's name allowlist (GRPH-1634)."""
import pytest

from app.db import SessionLocal
from app.models import TlsName
from app.services import tls_names


@pytest.fixture()
def db(client):
    s = SessionLocal()
    s.query(TlsName).delete()
    s.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def fake_net(monkeypatch):
    monkeypatch.setattr(tls_names, "interface_addresses", lambda: ["100.101.102.103"])
    # Fake only the names under test: the patch is process-wide, and the Postgres driver
    # resolves `localhost` through the same function.
    real = tls_names.socket.getaddrinfo
    fake = {"box.example.org": "100.101.102.103", "bank.com": "93.184.216.34"}

    def getaddrinfo(name, *a, **k):
        if name in fake:
            return [(2, 1, 6, "", (fake[name], 0))]
        return real(name, *a, **k)

    monkeypatch.setattr(tls_names.socket, "getaddrinfo", getaddrinfo)


def names(db):
    return {n.name for n in tls_names.list_names(db)}


def test_public_name_never_added(db):
    assert tls_names.add_seen_host(db, "bank.com") is False
    assert tls_names.add_seen_host(db, "bank.com:443") is False
    assert names(db) == set()


@pytest.mark.parametrize("host", ["ubuntu-srv.lan", "10.0.0.5", "nas", "Printer.Local:8080",
                                  "x.home.arpa", "svc.internal", "169.254.1.2", "fd00::1",
                                  "[fd00::2]:443"])
def test_local_names_added(db, host):
    assert tls_names.add_seen_host(db, host) is True
    assert tls_names.allowed(db, host)


def test_name_resolving_to_box_added(db):
    assert tls_names.add_seen_host(db, "box.example.org") is True
    row = db.get(TlsName, "box.example.org")
    assert (row.source, row.origin) == ("auto", "seen as Host")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "[::1]:80", "foo.localhost"])
def test_loopback_never_added(db, host):
    assert tls_names.add_seen_host(db, host) is False
    with pytest.raises(tls_names.NameRefused):
        tls_names.add_operator(db, host)
    assert names(db) == set()


def test_seed_auto_names_has_hostname_and_addresses_no_loopback(db, monkeypatch):
    monkeypatch.setattr(tls_names.socket, "gethostname", lambda: "Ubuntu-Srv")
    tls_names.seed_auto_names(db)
    assert names(db) == {"ubuntu-srv", "100.101.102.103"}
    assert db.get(TlsName, "100.101.102.103").origin == "network address"


def test_33rd_name_refused_and_removal_rules(db):
    for i in range(32):
        tls_names.add_operator(db, f"host{i}.lan")
    with pytest.raises(tls_names.NameRefused, match="maximum of 32"):
        tls_names.add_operator(db, "one-more.lan")
    assert tls_names.add_seen_host(db, "another.lan") is False  # full, silently
    assert tls_names.remove_operator(db, "host3.lan") is True
    tls_names.add_seen_host(db, "auto.lan")
    with pytest.raises(tls_names.NameRefused, match="automatic"):
        tls_names.remove_operator(db, "auto.lan")
    assert tls_names.allowed(db, "auto.lan")


@pytest.mark.parametrize("host", ["192.0.2.10", "198.18.0.1", "100.64.0.1", "8.8.8.8",
                                  "2001:db8::1"])
def test_non_rfc1918_addresses_not_added_from_host(db, host):
    assert tls_names.add_seen_host(db, host) is False
