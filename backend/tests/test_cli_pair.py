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


def test_used_code_cannot_be_approved_again(client, auth):
    p = _start(client)
    assert client.post("/api/cli/pair/approve", json={"user_code": p["user_code"]},
                       headers=auth).status_code == 204
    assert _redeem(client, p["device_code"]).json()["status"] == "approved"
    assert client.post("/api/cli/pair/approve", json={"user_code": p["user_code"]},
                       headers=auth).status_code == 404


def test_an_api_key_cannot_approve(client, auth):
    """Approval is a human's session, never an agent key — the authority gate."""
    plaintext = client.post("/api/api-keys", json={"name": "k", "project_id": "core"},
                            headers=auth).json()["plaintext"]
    p = _start(client)
    for h in ({"X-API-Key": plaintext}, {"Authorization": f"Bearer {plaintext}"}):
        r = client.post("/api/cli/pair/approve", json={"user_code": p["user_code"]}, headers=h)
        assert r.status_code in (401, 403), r.text
    assert _redeem(client, p["device_code"]).json() == {"status": "pending"}


def test_redeem_spends_once_even_if_the_row_was_already_spent(client, auth, db):
    """The conditional UPDATE refuses a row whose `used_at` was set between read and write."""
    from app.services import cli_pair as svc
    p = _start(client)
    client.post("/api/cli/pair/approve", json={"user_code": p["user_code"]}, headers=auth)
    row = db.query(CliPair).one()
    real_live = svc._live

    def live_then_spent(r):
        out = real_live(r)
        db.execute(CliPair.__table__.update().values(used_at=utcnow()))
        db.commit()
        return out
    svc._live = live_then_spent
    try:
        with pytest.raises(svc.InvalidPair):
            svc.redeem(db, device_code=p["device_code"])
    finally:
        svc._live = real_live
    assert row.id
