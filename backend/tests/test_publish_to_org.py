"""Per-project publish-to-org switches (GRPH-1674)."""
import pytest
from sqlalchemy import select

from app.models import Event, Membership, Project, User
from app.services import box_push, code_sync


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _project(client, auth):
    return client.post("/api/projects", headers=auth, json={"name": "Pub One"}).json()


def test_new_project_has_both_flags_on(client, auth):
    p = _project(client, auth)
    assert p["publish_to_org"] == {"specs": True, "item_digests": True}


def test_toggle_each_flag_independently_and_record_events(client, auth, db):
    p = _project(client, auth)
    r = client.put(f"/api/projects/{p['id']}/publish-to-org", headers=auth, json={"specs": False})
    assert r.status_code == 200
    assert r.json()["publish_to_org"] == {"specs": False, "item_digests": True}
    r = client.put(f"/api/projects/{p['id']}/publish-to-org", headers=auth,
                   json={"item_digests": False})
    assert r.json()["publish_to_org"] == {"specs": False, "item_digests": False}
    events = db.scalars(select(Event).where(Event.action == "publish_to_org",
                                            Event.project_id == p["id"])).all()
    assert sorted((e.meta["flag"], e.meta["value"]) for e in events) == [
        ("item_digests", False), ("specs", False)]
    assert all(e.meta["by"] for e in events)


def test_non_admin_member_is_refused(client, auth, db):
    p = _project(client, auth)
    me = db.scalars(select(User).where(User.email == "alex@ascme-labs.com")).one()
    m = db.scalars(select(Membership).where(Membership.project_id == p["id"],
                                            Membership.user_id == me.id)).one()
    m.role = "member"
    db.commit()
    r = client.put(f"/api/projects/{p['id']}/publish-to-org", headers=auth, json={"specs": False})
    assert r.status_code == 403
    assert db.get(Project, p["id"]).publish_specs is True


def test_admin_may_change_a_flag(client, auth, db):
    p = _project(client, auth)
    me = db.scalars(select(User).where(User.email == "alex@ascme-labs.com")).one()
    m = db.scalars(select(Membership).where(Membership.project_id == p["id"],
                                            Membership.user_id == me.id)).one()
    m.role = "admin"
    db.commit()
    r = client.put(f"/api/projects/{p['id']}/publish-to-org", headers=auth, json={"specs": False})
    assert r.status_code == 200


def test_publish_enabled_needs_link_and_flag(client, auth, db, monkeypatch):
    p = _project(client, auth)
    pid = p["id"]
    monkeypatch.setattr(code_sync, "link_status", lambda _db: {"linked": False})
    assert box_push.publish_enabled(db, pid, "specs") is False  # unlinked: nothing, flags on
    monkeypatch.setattr(code_sync, "link_status", lambda _db: {"linked": True})
    assert box_push.publish_enabled(db, pid, "specs") is True
    assert box_push.publish_enabled(db, pid, "item_digests") is True
    client.put(f"/api/projects/{pid}/publish-to-org", headers=auth, json={"specs": False})
    db.expire_all()
    assert box_push.publish_enabled(db, pid, "specs") is False
    assert box_push.publish_enabled(db, pid, "item_digests") is True
