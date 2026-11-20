"""GRPH-1788 (P93 S2): PATCH /auth/me and GET /auth/me/handle-check."""
import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import HandleHold, OrgMembership, User, utcnow
from app.services import ratelimit
from tests.test_cross_tenant import tenants  # noqa: F401  (fixture re-export)


def _add_user(uid, handle, org_id=None):
    db = SessionLocal()
    try:
        db.add(User(id=uid, name=uid, handle=handle, email=f"{uid}@x.test", password_hash="x"))
        db.flush()
        if org_id:
            db.add(OrgMembership(org_id=org_id, user_id=uid, handle=handle))
        db.commit()
    finally:
        db.close()


def _patch(client, headers, **body):
    return client.patch("/api/auth/me", json=body, headers=headers)


def test_handle_taken_in_my_org_names_the_org(client, tenants):  # noqa: F811
    _add_user("u_z", "zed-one", tenants["org_a"])
    r = _patch(client, tenants["alex"], handle="zed-one")
    assert r.status_code == 422
    assert r.json()["detail"] == {"field": "handle", "code": "handle_taken", "org": "Org A"}


def test_handle_only_in_a_foreign_org_saves(client, tenants):  # noqa: F811
    _add_user("u_y", "far-away", tenants["org_b"])
    r = _patch(client, tenants["alex"], handle="far-away")
    assert r.status_code == 200, r.text
    assert r.json()["handle"] == "far-away"


def test_hold_and_membership_are_byte_identical_and_leak_no_user(client, tenants):  # noqa: F811
    _add_user("u_z", "zed-one", tenants["org_a"])
    first = _patch(client, tenants["alex"], handle="zed-one")
    db = SessionLocal()
    try:
        m = db.query(OrgMembership).filter_by(user_id="u_z").one()
        db.delete(m)
        from datetime import timedelta
        db.add(HandleHold(org_id=tenants["org_a"], handle="zed-one", user_id="u_z",
                          expires_at=utcnow() + timedelta(days=5)))
        db.commit()
    finally:
        db.close()
    second = _patch(client, tenants["alex"], handle="zed-one")
    assert first.status_code == second.status_code == 422
    assert first.content == second.content
    assert "u_z" not in first.text


def test_reserved_and_invalid_codes(client, tenants):  # noqa: F811
    assert _patch(client, tenants["alex"], handle="admin").json()["detail"]["code"] == "handle_reserved"
    assert _patch(client, tenants["alex"], handle="alice_new").json()["detail"]["code"] == "handle_invalid"


def test_successful_change_writes_holds_and_moves_the_seat(client, tenants):  # noqa: F811
    old = client.get("/api/auth/me", headers=tenants["alex"]).json()["handle"]
    r = _patch(client, tenants["alex"], handle="fresh-one")
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        holds = db.query(HandleHold).filter_by(user_id="u1", handle=old).all()
        assert {h.org_id for h in holds} == {tenants["org_a"]}
        seat = db.query(OrgMembership).filter_by(user_id="u1", org_id=tenants["org_a"]).one()
        assert seat.handle == "fresh-one"
    finally:
        db.close()


def test_a_legacy_bad_handle_does_not_block_a_name_change(client, tenants):  # noqa: F811
    db = SessionLocal()
    try:
        db.get(User, "u1").handle = "Legacy_Bad"
        db.commit()
    finally:
        db.close()
    r = _patch(client, tenants["alex"], name="New Name", handle="Legacy_Bad")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "New Name" and r.json()["handle"] == "Legacy_Bad"


def test_name_required_and_all_or_nothing(client, tenants):  # noqa: F811
    r = _patch(client, tenants["alex"], name="  ")
    assert r.json()["detail"]["code"] == "name_required"
    before = client.get("/api/auth/me", headers=tenants["alex"]).json()
    r = _patch(client, tenants["alex"], name="Changed", avatar="#112233", avatar_pattern="nope")
    assert r.status_code == 422
    assert r.json()["detail"] == {"field": "avatar_pattern", "code": "avatar_pattern_invalid"}
    assert client.get("/api/auth/me", headers=tenants["alex"]).json() == before


def test_avatar2_equal_to_avatar_is_null(client, tenants):  # noqa: F811
    r = _patch(client, tenants["alex"], avatar="#112233", avatar2="#112233")
    assert r.status_code == 200 and r.json()["avatar2"] is None
    r = _patch(client, tenants["alex"], avatar2="#445566", avatar_pattern="none")
    assert r.json()["avatar2"] == "#445566"
    assert _patch(client, tenants["alex"], avatar2="red").json()["detail"]["field"] == "avatar2"


def test_me_has_api_key_summary(client, tenants):  # noqa: F811
    body = client.get("/api/auth/me", headers=tenants["alex"]).json()
    assert body["api_keys"]["count"] >= 1
    assert "avatar_pattern" in body and "avatar2" in body


def test_handle_check_requires_sign_in_and_is_rate_limited(client, tenants, monkeypatch):  # noqa: F811
    assert client.get("/api/auth/me/handle-check?handle=abc").status_code == 401
    monkeypatch.setattr(ratelimit, "HANDLE_CHECK_PER_MIN", 2)
    codes = [client.get("/api/auth/me/handle-check?handle=abc-d", headers=tenants["alex"]).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_handle_check_only_covers_my_orgs(client, tenants):  # noqa: F811
    _add_user("u_z", "zed-one", tenants["org_a"])
    _add_user("u_y", "far-away", tenants["org_b"])
    mine = client.get("/api/auth/me/handle-check?handle=zed-one", headers=tenants["alex"]).json()
    assert mine == {"handle": "zed-one", "available": False, "code": "handle_taken", "org": "Org A"}
    other = client.get("/api/auth/me/handle-check?handle=far-away", headers=tenants["alex"]).json()
    assert other["available"] is True


def test_signup_duplicate_message_stays_generic(client, monkeypatch):
    monkeypatch.setattr(settings, "signup_mode", "open")
    r = client.post("/api/auth/register", json={
        "name": "X Y", "email": "alex@ascme-labs.com", "handle": "whoever-1",
        "password": "longenoughpw1"})
    assert r.status_code == 409
    assert r.json()["detail"] == "could not create account with those details"
