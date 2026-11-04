"""POST /api/api-keys/{id}/rotate (GRPH-1310): one transaction, same shape, refusals."""
from app.db import SessionLocal
from app.models import ApiKey


def _mint(client, auth, **body):
    r = client.post("/api/api-keys", json={"name": "k", **body}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _set(key_id, **cols):
    db = SessionLocal()
    try:
        row = db.get(ApiKey, key_id)
        for k, v in cols.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()


def test_rotate_replaces_and_revokes(client, auth):
    pid = client.post("/api/projects", json={"name": "RotP"}, headers=auth).json()["id"]
    old = _mint(client, auth, name="agent", project_id=pid, tool_tiers=["fleet"], origin="web")
    _set(old["id"], roles=["worker", "reviewer"], posture="single")
    r = client.post(f"/api/api-keys/{old['id']}/rotate", json={"expires_in_days": 30}, headers=auth)
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["plaintext"] and new["id"] != old["id"]
    db = SessionLocal()
    try:
        live = db.query(ApiKey).filter(ApiKey.name == "agent", ApiKey.revoked.is_(False)).all()
        assert [k.id for k in live] == [new["id"]]
        k = live[0]
        assert (k.scopes, k.roles, k.tool_tiers, k.posture, k.project_id, k.origin) == (
            ["read", "write"], ["worker", "reviewer"], ["fleet"], "single", pid, "web")
        assert k.expires_at is not None
        assert db.get(ApiKey, old["id"]).revoked is True
    finally:
        db.close()


def test_rotate_refusals(client, auth):
    pid = client.post("/api/projects", json={"name": "RotR"}, headers=auth).json()["id"]
    sync = _mint(client, auth, project_id=pid, scopes=["read", "write", "sync"])
    wave = _mint(client, auth)
    _set(wave["id"], fleet_wave="W1")
    gone = _mint(client, auth)
    client.delete(f"/api/api-keys/{gone['id']}", headers=auth)
    setup = _mint(client, auth, origin="gban_setup")
    swamp = _mint(client, auth, origin="gban_swamp_setup")
    for k in (sync, wave, gone, setup, swamp):
        r = client.post(f"/api/api-keys/{k['id']}/rotate", json={}, headers=auth)
        assert r.status_code == 409, (k["id"], r.text)
    assert "gban setup" in client.post(f"/api/api-keys/{setup['id']}/rotate", json={}, headers=auth).text
    assert client.post("/api/api-keys/nope/rotate", json={}, headers=auth).status_code == 404


def test_rotate_other_users_row_is_404(client, auth):
    k = _mint(client, auth)
    db = SessionLocal()
    try:
        from app.models import User
        db.add(User(id="other-user", email="o@example.com", name="O", handle="o", password_hash="x"))
        db.commit()
    finally:
        db.close()
    _set(k["id"], user_id="other-user")
    assert client.post(f"/api/api-keys/{k['id']}/rotate", json={}, headers=auth).status_code == 404


def test_rotate_failure_leaves_nothing_behind(client, auth, monkeypatch):
    old = _mint(client, auth, name="atomic")
    from sqlalchemy.orm import Session

    real = Session.commit

    def boom(self, *a, **kw):
        # Fail only once the old row has been marked revoked — i.e. AFTER the replacement exists.
        if any(isinstance(o, ApiKey) and o.revoked for o in self.dirty):
            raise RuntimeError("commit failed")
        return real(self, *a, **kw)

    monkeypatch.setattr(Session, "commit", boom)
    try:
        client.post(f"/api/api-keys/{old['id']}/rotate", json={}, headers=auth)
    except RuntimeError:
        pass
    monkeypatch.undo()
    db = SessionLocal()
    try:
        rows = db.query(ApiKey).filter(ApiKey.name == "atomic").all()
        assert len(rows) == 1 and rows[0].revoked is False
    finally:
        db.close()
