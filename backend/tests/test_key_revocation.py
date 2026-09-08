"""Revoking a credential (GRPH-788).

The bug this file exists for: `DELETE /api-keys/{id}` hard-deleted the row, and PRD-38's
`agent_calls.api_key_id` is a NOT NULL foreign key with no `ondelete` — so the route 500'd
for any key that had ever made a call, which is every key anyone would want to revoke. An
UNUSED key deleted fine, so the whole failure presented backwards, and the Settings trash
button surfaced nothing.

Every test here goes through a REAL call rather than inserting an `agent_calls` row by hand.
The constraint is only reachable when the telemetry actually wrote something, and a
hand-built row would let the test pass against a version of the app that had stopped
recording calls at all.
"""


def _mint(client, auth, **body):
    r = client.post("/api/api-keys", json={"name": "agent", **body}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _call(client, plaintext, tool="get_context"):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": {}}},
        headers={"X-API-Key": plaintext},
    )


def _calls_recorded(key_id: str) -> int:
    from app.db import SessionLocal
    from app.models import AgentCall

    db = SessionLocal()
    try:
        return db.query(AgentCall).filter(AgentCall.api_key_id == key_id).count()
    finally:
        db.close()


def test_a_key_that_has_been_used_can_be_revoked(client, auth):
    """The regression. This 500'd."""
    key = _mint(client, auth, project_id="core")
    assert _call(client, key["plaintext"]).status_code == 200

    # The control: without this the test would pass on a build that records nothing, and the
    # constraint it exists to exercise would never be touched.
    assert _calls_recorded(key["id"]) > 0, "no agent_calls row — the FK is not being exercised"

    assert client.delete(f"/api/api-keys/{key['id']}", headers=auth).status_code == 204


def test_a_revoked_key_authenticates_no_one(client, auth):
    """Soft is only acceptable if it is a real revocation."""
    key = _mint(client, auth, project_id="core")
    assert _call(client, key["plaintext"]).status_code == 200

    client.delete(f"/api/api-keys/{key['id']}", headers=auth)

    dead = _call(client, key["plaintext"])
    assert dead.status_code == 401, dead.text


def test_the_row_survives_so_its_call_history_does(client, auth):
    """The reason it is soft rather than a cascade: PRD-38's telemetry is the thing the
    foreign key protects, and a delete could only orphan or destroy it."""
    key = _mint(client, auth, project_id="core")
    _call(client, key["plaintext"])
    before = _calls_recorded(key["id"])

    client.delete(f"/api/api-keys/{key['id']}", headers=auth)

    assert _calls_recorded(key["id"]) == before
    listed = client.get("/api/api-keys", headers=auth).json()
    row = next((k for k in listed if k["id"] == key["id"]), None)
    assert row is not None, "a revoked key vanished from the registry"
    assert row["revoked"] is True


def test_an_unused_key_revokes_the_same_way(client, auth):
    """It always worked; it must keep working, and now via the same path."""
    key = _mint(client, auth, project_id="core")
    assert _calls_recorded(key["id"]) == 0

    assert client.delete(f"/api/api-keys/{key['id']}", headers=auth).status_code == 204
    row = next(k for k in client.get("/api/api-keys", headers=auth).json() if k["id"] == key["id"])
    assert row["revoked"] is True


def test_revoking_twice_is_not_an_error(client, auth):
    key = _mint(client, auth, project_id="core")
    assert client.delete(f"/api/api-keys/{key['id']}", headers=auth).status_code == 204
    assert client.delete(f"/api/api-keys/{key['id']}", headers=auth).status_code == 204


def test_someone_elses_key_is_still_not_found(client, auth, decoy):
    """The ownership check is unchanged, and a soft path must not soften it."""
    from app.db import SessionLocal
    from app.models import ApiKey, User

    db = SessionLocal()
    try:
        other = db.query(User).filter(User.email != "alex@ascme-labs.com").first()
        assert other is not None, "no second user to test ownership against"
        from app.security.apikey import generate_api_key

        row, _ = generate_api_key(db, other.id, "theirs", ["read"], None, 30)
        theirs = row.id
        db.commit()
    finally:
        db.close()

    assert client.delete(f"/api/api-keys/{theirs}", headers=auth).status_code == 404

    db = SessionLocal()
    try:
        assert db.get(ApiKey, theirs).revoked is False, "another user's key was revoked"
    finally:
        db.close()
