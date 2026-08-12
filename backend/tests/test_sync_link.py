"""AL-141: web-managed instance↔cloud link for the Sync/Link settings page.

Covers the link/unlink/status endpoints, the credential-never-leaves-the-server invariant,
encryption at rest, and that a web link (not just env) actually drives the push resolution.
"""


def _status(client, auth):
    r = client.get("/api/sync/status", headers=auth)
    assert r.status_code == 200
    return r.json()


def test_status_starts_unlinked_and_lists_readable_projects(client, auth):
    s = _status(client, auth)
    assert s["linked"] is False and s["source"] == "" and s["credential_set"] is False
    # the seeded default project is visible with a derived sync state
    ids = {p["project_id"] for p in s["projects"]}
    assert "core" in ids
    core = next(p for p in s["projects"] if p["project_id"] == "core")
    assert core["writable"] is True and "status" in core and "pending" in core


def test_link_then_status_reflects_it_without_leaking_the_key(client, auth):
    r = client.post("/api/sync/link",
                    # Deliberately the OLD hostname: both hosts serve the same instance, and
                    # this pins that an operator who linked before the rename still works.
                    json={"cloud_url": "cloud.agentldgr.dev", "api_key": "gb_sk_secret", "org": "acme"},
                    headers=auth)
    assert r.status_code == 200
    s = r.json()
    assert s["linked"] is True and s["source"] == "web"
    assert s["cloud_url"] == "https://cloud.agentldgr.dev"  # scheme + trailing-slash normalized
    assert s["org"] == "acme" and s["credential_set"] is True
    assert s["linked_at"]
    # the raw key must never appear in any status field
    assert "gb_sk_secret" not in r.text
    assert _status(client, auth)["linked"] is True


def test_relink_with_blank_key_keeps_the_stored_credential(client, auth):
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_one"}, headers=auth)
    # re-link to a new URL without resending the key — the write-only round-trip keeps it
    r = client.post("/api/sync/link", json={"cloud_url": "cloud.b.dev", "api_key": ""}, headers=auth)
    assert r.status_code == 200 and r.json()["credential_set"] is True
    assert r.json()["cloud_url"] == "https://cloud.b.dev"


def test_first_link_requires_a_key(client, auth):
    r = client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": ""}, headers=auth)
    assert r.status_code == 422 and "key" in r.json()["detail"].lower()


def test_unlink_clears_the_link(client, auth):
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_x"}, headers=auth)
    r = client.delete("/api/sync/link", headers=auth)
    assert r.status_code == 200 and r.json()["linked"] is False and r.json()["credential_set"] is False


def test_sync_graph_off_shows_as_paused(client, auth):
    client.patch("/api/platform?project_id=core", json={"sync_graph": False}, headers=auth)
    core = next(p for p in _status(client, auth)["projects"] if p["project_id"] == "core")
    assert core["sync_graph"] is False and core["status"] == "paused"


def test_web_link_drives_push_resolution_over_env(client, auth):
    """The DB link must be the target `push`/`purge` resolve to — not only the env link."""
    from app.db import SessionLocal
    from app.services import code_sync

    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_key"}, headers=auth)
    db = SessionLocal()
    try:
        url, key = code_sync._target(db, "", "")  # no explicit creds → resolves the web link
        assert url == "https://cloud.a.dev" and key == "gb_sk_key"
    finally:
        db.close()


def test_push_without_a_link_is_409(client, auth):
    # a pure local-only instance never pushes (D2) — the endpoint says so, not a 500
    r = client.post("/api/sync/push", json={"project_id": "core"}, headers=auth)
    assert r.status_code == 409


def test_key_is_encrypted_at_rest_when_a_key_is_configured(client, auth, monkeypatch):
    from app.config import settings
    from app.db import SessionLocal
    from app.models import SyncLink
    from app.security import secrets

    monkeypatch.setattr(settings, "secret_encryption_key", "unit-test-secret")
    secrets._fernet.cache_clear()
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_plain"}, headers=auth)
    db = SessionLocal()
    try:
        link = db.get(SyncLink, "instance")
        assert link.api_key_enc.startswith("enc::")           # stored ciphertext, not plaintext
        assert "gb_sk_plain" not in link.api_key_enc
        assert secrets.decrypt(link.api_key_enc) == "gb_sk_plain"  # round-trips for push
    finally:
        db.close()
        secrets._fernet.cache_clear()
