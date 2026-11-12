"""GRPH-1526: prds.created_by on every create path and the question recipient resolver."""
import pytest

from app.models import ApiKey, Item, Membership, Prd, Project, User
from app.services import prds as prd_svc
from app.services.question_recipients import question_recipients


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _user(db, uid):
    db.add(User(id=uid, name=uid, handle=uid, email=f"{uid}@x.test", password_hash="x"))
    db.flush()


def _project(db):
    db.add(Project(id="pq", name="PQ", tag="PQ"))
    db.flush()


def _member(db, uid, role="member", access="read"):
    db.add(Membership(user_id=uid, project_id="pq", role=role, access=access))
    db.flush()


def _setup(db, owner_access="read"):
    _project(db)
    for u in ("owner", "po", "other"):
        _user(db, u)
    _member(db, "po", "owner", "write")
    _member(db, "owner", "member", owner_access)
    prd = prd_svc.create_prd(db, title="T", project_id="pq", created_by="owner")
    item = Item(id="PQ-1", number=1, project_id="pq", title="x", prd_id=prd.id)
    db.add(item)
    db.commit()
    return prd, item


def test_prd_owner_is_the_recipient(db):
    _, item = _setup(db)
    assert question_recipients(db, item)["users"] == ["owner"]


def test_no_prd_owner_falls_back_to_project_owners(db):
    prd, item = _setup(db)
    prd.created_by = None
    db.commit()
    assert question_recipients(db, item)["users"] == ["po"]


def test_deleted_owner_falls_back(db):
    prd, item = _setup(db)
    prd.created_by = "ghost"
    db.commit()
    assert question_recipients(db, item)["users"] == ["po"]


def test_owner_without_read_access_falls_back(db):
    _, item = _setup(db, owner_access="none")
    assert question_recipients(db, item)["users"] == ["po"]


def test_user_without_read_access_is_never_returned(db):
    """Sabotage target: skipping the access check makes this fail."""
    prd, item = _setup(db, owner_access="none")
    m = db.query(Membership).filter_by(user_id="po").one()
    m.access = "none"
    db.commit()
    out = question_recipients(db, item)
    assert out["users"] == []
    assert "nobody with access" in out["reason"]


def test_no_prd_and_no_owners_is_empty_not_error(db):
    _project(db)
    db.add(Item(id="PQ-2", number=2, project_id="pq", title="x"))
    db.commit()
    item = db.get(Item, "PQ-2")
    assert question_recipients(db, item)["users"] == []


def test_rest_create_stores_user(client, auth, db):
    r = client.post("/api/prds", json={"title": "R", "project_id": "core"}, headers=auth)
    assert r.status_code == 201
    me = client.get("/api/auth/me", headers=auth).json()
    assert db.get(Prd, r.json()["id"]).created_by == me["id"]


def test_mcp_create_stores_key_user_and_survives_key_revocation(client, auth, db):
    key = client.post("/api/api-keys", json={"name": "creator"}, headers=auth).json()
    r = client.post("/api/mcp", headers={"X-API-Key": key["plaintext"]}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "create_prd", "arguments": {"title": "M", "project_id": "core"}},
    })
    prd_id = r.json()["result"]["structuredContent"]["id"]
    row = db.query(ApiKey).filter(ApiKey.name == "creator").one()
    prd = db.get(Prd, prd_svc.get_prd(db, prd_id).id)
    assert prd.created_by == row.user_id
    row.revoked = True
    db.commit()
    db.refresh(prd)
    assert prd.created_by == row.user_id
    # ...and the owner still resolves: who is alerted does not follow the key.
    db.add(Item(id="CORE-9001", number=9001, project_id="core", title="q", prd_id=prd.id))
    db.commit()
    out = question_recipients(db, db.get(Item, "CORE-9001"))
    assert out == {"users": [row.user_id], "reason": "prd owner"}


def test_only_project_owners_with_read_access_are_returned(db):
    """Sabotage target: dropping the can_read check on the owners loop lets `po2` in."""
    _, item = _setup(db, owner_access="none")
    _user(db, "po2")
    _member(db, "po2", "owner", "none")
    db.commit()
    out = question_recipients(db, item)
    assert out["users"] == ["po"]
    assert "project owners" in out["reason"]

