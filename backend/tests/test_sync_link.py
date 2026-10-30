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


# ---- GRPH-1223: link/unlink is a box-admin act -------------------------------------------

def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_a_member_cannot_unlink_the_box_and_the_link_remains(client, auth):
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_x"}, headers=auth)
    ops = _login(client, "ops@ascme-labs.com")  # seeded `member` on every project
    r = client.delete("/api/sync/link", headers=ops)
    assert r.status_code == 403
    assert r.json()["rule_id"] == "sync.link_needs_box_admin"
    assert _status(client, auth)["linked"] is True
    # nor can they re-point it
    r = client.post("/api/sync/link", json={"cloud_url": "evil.dev", "api_key": "gb_sk_y"}, headers=ops)
    assert r.status_code == 403 and r.json()["rule_id"] == "sync.link_needs_box_admin"
    assert _status(client, auth)["cloud_url"] == "https://cloud.a.dev"
    # the operator's unlink goes through
    assert client.delete("/api/sync/link", headers=auth).json()["linked"] is False


def test_the_refusal_is_recorded_against_its_rule(client, auth):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Event

    client.delete("/api/sync/link", headers=_login(client, "ops@ascme-labs.com"))
    db = SessionLocal()
    try:
        [ev] = list(db.scalars(select(Event).where(Event.action == "refusal")))
        assert ev.meta["rule_id"] == "sync.link_needs_box_admin"
    finally:
        db.close()


def test_an_admin_the_operator_appointed_may_link(client, auth):
    dana = _login(client, "dana@ascme-labs.com")  # seeded `admin` on the operator's projects
    r = client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_x"}, headers=dana)
    assert r.status_code == 200 and r.json()["linked"] is True


def test_owning_a_project_you_created_does_not_make_you_a_box_admin(client, auth):
    ops = _login(client, "ops@ascme-labs.com")
    assert client.post("/api/projects", json={"name": "Mine"}, headers=ops).status_code == 201
    r = client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_x"}, headers=ops)
    assert r.status_code == 403


def test_on_a_hosted_box_only_a_platform_operator_may_link(client, auth, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    r = client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_x"}, headers=auth)
    assert r.status_code == 403 and r.json()["rule_id"] == "sync.link_needs_box_admin"


# ---- GRPH-1223: the org overview ---------------------------------------------------------

_CLOUD = {
    "org": {"name": "Ascme Labs", "host": "ascme-labs", "plan": "team"},
    "role": "admin",
    "counts": {"members": 18, "teams": 4, "policy_rules": 6, "deployments": 2},
    "deployments": [
        {"name": "graphban.ascme.internal", "projects": 5, "here": True,
         "last_sync_at": "2026-10-30T09:40:00+00:00"},
        {"name": "graphban-eu.ascme.internal", "projects": 3, "here": False,
         "last_sync_at": "2026-10-30T09:36:00+00:00"},
    ],
}


def _org(client, auth):
    r = client.get("/api/sync/org", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _cloud(monkeypatch, body):
    """Stub the cloud: `body` is what it answers, None is a cloud that cannot be reached."""
    from app.services import code_sync

    calls = []

    def fake(url, key):
        calls.append((url, key))
        return body() if callable(body) else body

    monkeypatch.setattr(code_sync, "fetch_org_overview", fake)
    return calls


def test_an_unlinked_box_says_so_and_nothing_else(client, auth):
    assert _org(client, auth) == {"linked": False}


def test_a_linked_box_shows_the_clouds_org(client, auth, monkeypatch):
    calls = _cloud(monkeypatch, _CLOUD)
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_k"}, headers=auth)
    o = _org(client, auth)
    assert o["linked"] is True and o["reachable"] is True and o["unreachable_since"] is None
    assert o["org"] == {"name": "Ascme Labs", "host": "ascme-labs", "plan": "team"}
    assert o["role"] == "admin"
    assert o["counts"] == {"members": 18, "teams": 4, "policy_rules": 6, "deployments": 2}
    assert [(d["name"], d["projects"], d["here"]) for d in o["deployments"]] == [
        ("graphban.ascme.internal", 5, True), ("graphban-eu.ascme.internal", 3, False)]
    assert calls == [("https://cloud.a.dev", "gb_sk_k")]
    _org(client, auth)
    assert len(calls) == 1  # cached briefly: the window does not hit the cloud on every read


def test_an_unreachable_cloud_keeps_identity_and_shows_no_counts(client, auth, monkeypatch):
    from app.services import code_sync

    up = {"v": True}
    _cloud(monkeypatch, lambda: _CLOUD if up["v"] else None)
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_k"}, headers=auth)
    assert _org(client, auth)["reachable"] is True

    up["v"] = False
    monkeypatch.setattr(code_sync, "_ORG_TTL", 0.0)  # expire the cached answer
    o = _org(client, auth)
    assert o["linked"] is True and o["reachable"] is False and o["unreachable_since"]
    assert o["org"]["name"] == "Ascme Labs" and o["role"] == "admin"   # last known identity
    assert o["counts"] is None                                          # never stale numbers
    assert all(d["projects"] is None for d in o["deployments"])
    assert next(d for d in o["deployments"] if not d["here"])["last_sync_at"] is None
    first = o["unreachable_since"]
    assert _org(client, auth)["unreachable_since"] == first  # since the FIRST failure

    up["v"] = True
    back = _org(client, auth)
    assert back["reachable"] is True and back["counts"]["members"] == 18


def test_a_cloud_never_reached_still_answers_unreachable(client, auth, monkeypatch):
    _cloud(monkeypatch, None)
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_k",
                                        "org": "acme"}, headers=auth)
    o = _org(client, auth)
    assert o["reachable"] is False and o["counts"] is None and o["unreachable_since"]
    assert o["org"]["name"] == "acme" and o["deployments"] == []


def test_the_cloud_side_overview_is_resolved_from_the_sync_credential(client, auth, monkeypatch):
    """End to end against the real cloud route: the box's fetch is routed into this app."""
    from app.config import settings
    from app.services import code_sync

    monkeypatch.setattr(settings, "hosted_mode", True)
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=auth).json()
    proj = client.post("/api/projects", json={"name": "Core2"}, headers=auth).json()
    r = client.post("/api/api-keys", headers=auth, json={
        "name": "box-one", "scopes": ["read", "sync"], "project_id": proj["id"]})
    key = r.json()["plaintext"]
    assert client.post("/api/sync/code-graph", json={"nodes": []},
                       headers={"X-API-Key": key}).status_code == 200

    r = client.get("/api/sync/org-overview", headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org"]["name"] == "Acme" and body["org"]["plan"] == org["plan"]
    assert body["role"] == "owner"
    assert body["counts"]["members"] == 1 and body["counts"]["deployments"] == 1
    [d] = body["deployments"]
    assert d["name"] == "box-one" and d["here"] is True and d["projects"] == 1
    assert d["last_sync_at"]

    # a key that is not a sync credential sees no org
    plain = client.post("/api/api-keys", headers=auth, json={
        "name": "agent", "scopes": ["read"], "project_id": proj["id"]}).json()["plaintext"]
    assert client.get("/api/sync/org-overview", headers={"X-API-Key": plain}).status_code == 404

    # and the box-side fetch parses what the cloud route serves
    def via_app(url, k):
        resp = client.get("/api/sync/org-overview", headers={"X-API-Key": k})
        return resp.json() if resp.status_code == 200 else None

    monkeypatch.setattr(code_sync, "fetch_org_overview", via_app)
    monkeypatch.setattr(settings, "hosted_mode", False)
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": key}, headers=auth)
    o = _org(client, auth)
    assert o["reachable"] is True and o["org"]["name"] == "Acme" and o["counts"]["teams"] == 0


def test_the_overview_says_whether_the_viewer_may_unlink(client, auth, monkeypatch):
    _cloud(monkeypatch, _CLOUD)
    client.post("/api/sync/link", json={"cloud_url": "cloud.a.dev", "api_key": "gb_sk_k"}, headers=auth)
    assert _org(client, auth)["can_unlink"] is True
    # `role` is the linking credential's; a member viewing the same link may not unlink it
    member = _org(client, _login(client, "ops@ascme-labs.com"))
    assert member["role"] == "admin" and member["can_unlink"] is False
