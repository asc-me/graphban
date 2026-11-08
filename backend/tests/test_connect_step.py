"""Quick-start step ③ and the `last_used` signal behind it (GRPH-1362)."""
import sqlalchemy as sa

from app.security import apikey as apikey_mod


def _mint(client, auth, project_id, origin="quickstart"):
    r = client.post("/api/api-keys", json={"name": "alex-mbp", "project_id": project_id,
                                           "origin": origin}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _proj(client, auth, name):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


def _connect(client, auth, pid):
    steps = client.get(f"/api/projects/{pid}/setup", headers=auth).json()["steps"]
    return next(s for s in steps if s["name"] == "Connect your agent")


def _call(client, plaintext):
    return client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_context", "arguments": {}}}, headers={"X-API-Key": plaintext})


def _last_used(key_id):
    from app.db import SessionLocal
    from app.models import ApiKey
    db = SessionLocal()
    try:
        return db.get(ApiKey, key_id).last_used
    finally:
        db.close()


def test_pending_until_the_first_call_then_done_with_evidence(client, auth):
    pid = _proj(client, auth, "ConnA")
    key = _mint(client, auth, pid)
    assert _connect(client, auth, pid)["status"] == "pending"
    assert _call(client, key["plaintext"]).status_code == 200
    step = _connect(client, auth, pid)
    assert step["status"] == "done" and step["proof"] == "first_call"
    assert "alex-mbp" in step["detail"]


def test_first_call_stamps_immediately_later_calls_are_throttled(client, auth):
    pid = _proj(client, auth, "ConnB")
    key = _mint(client, auth, pid)
    assert _last_used(key["id"]) is None
    _call(client, key["plaintext"])
    first = _last_used(key["id"])
    assert first is not None
    _call(client, key["plaintext"])
    assert _last_used(key["id"]) == first  # inside the minute: no second write
    apikey_mod._last_stamped[key["id"]] -= apikey_mod.LAST_USED_THROTTLE_SECONDS + 1
    _call(client, key["plaintext"])
    assert _last_used(key["id"]) > first


def test_revoked_key_with_a_call_does_not_tick(client, auth):
    pid = _proj(client, auth, "ConnC")
    key = _mint(client, auth, pid)
    _call(client, key["plaintext"])
    assert _connect(client, auth, pid)["status"] == "done"
    client.delete(f"/api/api-keys/{key['id']}", headers=auth)
    assert _connect(client, auth, pid)["status"] == "pending"


def test_inactive_key_row_leaves_it_unticked(client, auth):
    # Sabotage fixture: the key HAS a last_used but its row is inactive.
    from app.db import SessionLocal
    from app.models import ApiKey
    pid = _proj(client, auth, "ConnD")
    key = _mint(client, auth, pid)
    _call(client, key["plaintext"])
    db = SessionLocal()
    try:
        db.execute(sa.update(ApiKey).where(ApiKey.id == key["id"]).values(revoked=True))
        db.commit()
    finally:
        db.close()
    assert _connect(client, auth, pid)["status"] == "pending"


def test_another_origin_or_project_never_ticks_it(client, auth):
    pid = _proj(client, auth, "ConnE")
    other = _proj(client, auth, "ConnF")
    web = _mint(client, auth, pid, origin="web")
    elsewhere = _mint(client, auth, other)
    _call(client, web["plaintext"])
    _call(client, elsewhere["plaintext"])
    assert _connect(client, auth, pid)["status"] == "pending"


def test_agent_view_keeps_four_steps(client, auth):
    pid = _proj(client, auth, "ConnG")
    key = _mint(client, auth, pid)
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "setup_project", "arguments": {}}}, headers={"X-API-Key": key["plaintext"]})
    assert len(r.json()["result"]["structuredContent"]["steps"]) == 4
