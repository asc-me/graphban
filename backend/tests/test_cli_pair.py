"""Device-code sign-in for `gban login --pair` (GRPH-1366).

The property worth testing is whose identity the CLI ends up with: the approving session's,
never anything the requester supplied.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import CliPair, utcnow


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _start(client):
    r = client.post("/api/cli/pair")
    assert r.status_code == 201, r.text
    return r.json()


def _redeem(client, device_code):
    return client.post("/api/cli/pair/token", json={"device_code": device_code})


def _me(client, access_token):
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert r.status_code == 200
    return r.json()


def test_unapproved_code_yields_no_credential(client):
    p = _start(client)
    r = _redeem(client, p["device_code"])
    assert r.status_code == 200
    assert r.json() == {"status": "pending"}


def test_approved_code_yields_credential_for_approver_once(client, auth):
    p = _start(client)
    a = client.post("/api/cli/pair/approve", json={"user_code": p["user_code"].lower()}, headers=auth)
    assert a.status_code == 204, a.text
    r = _redeem(client, p["device_code"])
    assert r.json()["status"] == "approved"
    assert _me(client, r.json()["access_token"])["email"] == "alex@ascme-labs.com"
    assert _redeem(client, p["device_code"]).status_code == 400  # single-use


def test_identity_is_the_approvers_not_a_guess(client, auth):
    other = client.post("/api/auth/register", json={
        "name": "Other Person", "handle": "otherp", "email": "other@example.com",
        "password": "correct horse battery"})
    assert other.status_code == 201, other.text
    other_h = {"Authorization": f"Bearer {other.json()['access_token']}"}
    p = _start(client)
    assert client.post("/api/cli/pair/approve", json={"user_code": p["user_code"]},
                       headers=other_h).status_code == 204
    # A second approver cannot swap the identity.
    assert client.post("/api/cli/pair/approve", json={"user_code": p["user_code"]},
                       headers=auth).status_code == 404
    r = _redeem(client, p["device_code"]).json()
    assert _me(client, r["access_token"])["email"] == "other@example.com"


def test_approve_requires_a_session(client):
    p = _start(client)
    assert client.post("/api/cli/pair/approve", json={"user_code": p["user_code"]}).status_code in (401, 403)


def test_expired_code_cannot_be_approved_or_redeemed(client, auth, db):
    p = _start(client)
    row = db.query(CliPair).one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert client.post("/api/cli/pair/approve", json={"user_code": p["user_code"]},
                       headers=auth).status_code == 404
    assert _redeem(client, p["device_code"]).status_code == 400


def test_unknown_device_code_is_refused(client):
    assert _redeem(client, "nope").status_code == 400
