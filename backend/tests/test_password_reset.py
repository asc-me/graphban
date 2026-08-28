"""A forgotten password has a way back, and the way back closes behind you (GRPH-359).

Before this, a forgotten password was permanent: `password_hash` was written at account
creation and, after PR #170, changeable only by someone who could already authenticate. On a
hosted product onboarding paying orgs the first support ticket is a customer locked out and an
operator with no way to help.

**Most of these tests are about the ways this feature is worse than not having it.** A reset
flow that confirms which addresses have accounts is an enumeration oracle. One whose link works
twice is a credential sitting in an inbox forever. One that leaves old sessions alive keeps
whoever took the password signed in. The happy path is one test; the rest are those.
"""
from __future__ import annotations

import pytest

from app.models import PasswordReset, User, utcnow
from app.services import password_reset as reset_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def account(client):
    """A registered user, created through the real endpoint."""
    email, password = "locked.out@example.com", "correct horse battery"
    r = client.post("/api/auth/register",
                    json={"name": "Locked Out", "handle": "lockedout", "email": email,
                          "password": password})
    assert r.status_code == 201, r.text
    return {"email": email, "password": password, "tokens": r.json()}


def _request(client, email):
    return client.post("/api/auth/password-reset", json={"email": email})


def _confirm(client, token, new_password="a brand new password"):
    return client.post("/api/auth/password-reset/confirm",
                       json={"token": token, "new_password": new_password})


def _mint(db, email, ip=""):
    """The token, as the email would carry it."""
    return reset_svc.request_reset(db, email=email, base_url="https://example.test", ip=ip)


# ── the acceptance ────────────────────────────────────────────────────────────

def test_a_locked_out_user_gets_back_in(client, db, account):
    """THE point of the feature, end to end through the real routes."""
    assert _request(client, account["email"]).status_code == 202

    token = _mint(db, account["email"])
    assert token, "no reset was minted for a registered address"

    r = _confirm(client, token, "a brand new password")
    assert r.status_code == 200, r.text
    assert r.json()["access_token"], "the user was not signed in after resetting"

    ok = client.post("/api/auth/login",
                     json={"email": account["email"], "password": "a brand new password"})
    assert ok.status_code == 200, "the new password does not work"
    old = client.post("/api/auth/login",
                      json={"email": account["email"], "password": account["password"]})
    assert old.status_code != 200, "the OLD password still works after a reset"


def test_every_prior_session_is_dead(client, db, account):
    """`token_version` is the AL-59 revocation machinery and it is the reason to reset, not a
    side effect: a forgotten password usually means it was not private."""
    before = account["tokens"]["access_token"]
    assert client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {before}"}).status_code == 200

    _confirm(client, _mint(db, account["email"]))

    after = client.get("/api/auth/me", headers={"Authorization": f"Bearer {before}"})
    assert after.status_code == 401, (
        "a session issued before the reset still works — whoever had the old password is "
        "still signed in")


# ── the ways it would be worse than nothing ───────────────────────────────────

def test_the_response_does_not_say_whether_the_account_exists(client, account):
    """AN ENUMERATION ORACLE IS WORSE THAN THE MISSING FEATURE. Anyone could otherwise ask
    this endpoint whether a given person has an account here."""
    known = _request(client, account["email"])
    unknown = _request(client, "nobody-at-all@example.com")

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json(), (
        f"the responses differ: {known.json()} vs {unknown.json()}")


def test_no_reset_is_minted_for_an_unknown_address(client, db):
    """The other half of the same property, checked on the DATABASE rather than the response —
    a route that answered identically while creating a row would leak through timing and
    would leave rows to correlate."""
    assert _mint(db, "nobody-at-all@example.com") is None
    assert db.query(PasswordReset).count() == 0


def test_a_link_works_once(client, db, account):
    """A reset link that still works after use is a credential in an inbox forever."""
    token = _mint(db, account["email"])
    assert _confirm(client, token, "first new password").status_code == 200

    again = _confirm(client, token, "second new password")
    assert again.status_code == 400, "the same link reset the password twice"

    still = client.post("/api/auth/login",
                        json={"email": account["email"], "password": "first new password"})
    assert still.status_code == 200, "the second use changed the password anyway"


def test_an_expired_link_is_refused(client, db, account):
    from datetime import timedelta

    token = _mint(db, account["email"])
    row = db.query(PasswordReset).one()
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()

    assert _confirm(client, token).status_code == 400


def test_requesting_a_second_link_retires_the_first(client, db, account):
    """Single-use is defeated by a second REQUEST, not only by a second use: two live links
    means an older mail still works after the user has used the newest one."""
    first = _mint(db, account["email"])
    second = _mint(db, account["email"])
    assert first != second

    assert _confirm(client, first).status_code == 400, "the superseded link still worked"
    assert _confirm(client, second).status_code == 200


def test_a_failed_confirmation_does_not_burn_the_link(client, db, account):
    """`used_at` is set on SUCCESS. A user who fumbles the new password — too short, caught by
    the schema — must not find their link spent when they try again."""
    token = _mint(db, account["email"])

    assert _confirm(client, token, "short").status_code == 422
    assert _confirm(client, token, "a long enough password").status_code == 200


def test_the_token_is_not_stored_in_the_clear(client, db, account):
    """A reset token is a credential for an existing account, so a database read, a backup or
    a log line holding this row must not yield a working link."""
    token = _mint(db, account["email"])
    row = db.query(PasswordReset).one()

    assert row.token_hash != token
    assert token not in row.token_hash
    assert len(row.token_hash) == 64, "not a sha256 digest"


def test_the_route_does_not_hand_back_the_token(client, account):
    """`request_reset` returns the token so tests need not parse it out of email prose. A
    route that echoed it would publish every reset link to whoever can call the endpoint —
    which is everyone, because it is unauthenticated."""
    body = _request(client, account["email"]).json()

    assert "token" not in str(body).lower(), f"the response carries a token: {body}"


def test_reset_attempts_do_not_lock_the_account_out_of_login(client, account):
    """A SEPARATE bucket from login. Sharing it would mean a few reset attempts close the one
    door still open to someone who has just remembered their password."""
    from app.config import settings

    for _ in range(settings.login_rate_per_min + 2):
        _request(client, account["email"])

    r = client.post("/api/auth/login",
                    json={"email": account["email"], "password": account["password"]})
    assert r.status_code == 200, "reset requests exhausted the LOGIN bucket"


def test_the_reset_endpoint_is_itself_limited(client, account):
    """The complement: unlimited would make it a mail cannon pointed at any address."""
    from app.config import settings

    codes = [_request(client, account["email"]).status_code
             for _ in range(settings.login_rate_per_min + 5)]

    assert 429 in codes, f"no limit fired across {len(codes)} requests: {set(codes)}"
