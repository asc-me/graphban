"""AL-93 + AL-91: signup modes and the operator-issued platform invite.

Platform invites are the missing onboarding path for a private beta: they authorize a
BRAND-NEW account to sign up and found its OWN org, as opposed to org invites which
seat a user in an existing org. The operator plane that issues them is hosted-only and
platform-admin-only, and 404s for everyone else.
"""
import pytest

SEED_PW = "graphban"
ADMIN_EMAIL = "alex@ascme-labs.com"


def _login(client, email, password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def operator(client, monkeypatch):
    """Hosted mode with alex on the platform-admin allowlist."""
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "platform_admin_emails", ADMIN_EMAIL)
    return _login(client, ADMIN_EMAIL)


@pytest.fixture(autouse=True)
def _clear_outbox():
    from app.services.email import outbox

    outbox.clear()
    yield
    outbox.clear()


def _token_from_outbox():
    from app.services.email import outbox

    return outbox[0].text.rsplit("/invite/", 1)[1].split()[0]


# ---- signup modes (AL-93) ------------------------------------------------------
def test_open_mode_allows_self_serve(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "signup_mode", "open")
    assert client.post("/api/auth/register", json={
        "name": "Free Agent", "handle": "freeagent", "email": "free@example.com",
        "password": "sup3rsecret",
    }).status_code == 201


def test_invite_only_blocks_uninvited(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "signup_mode", "invite_only")
    r = client.post("/api/auth/register", json={
        "name": "No One", "handle": "noone", "email": "noone@example.com",
        "password": "sup3rsecret",
    })
    assert r.status_code == 403
    assert "invite-only" in r.json()["detail"]


def test_closed_mode_blocks_even_invite_holders(client, operator, monkeypatch):
    """`closed` is the kill switch — a valid invite does not get you in."""
    from app.config import settings

    client.post("/api/admin/invites", json={"email": "new@example.com"}, headers=operator)
    token = _token_from_outbox()
    monkeypatch.setattr(settings, "signup_mode", "closed")
    r = client.post("/api/auth/register", json={
        "name": "New Co", "handle": "newco", "email": "new@example.com",
        "password": "sup3rsecret", "invite_token": token,
    })
    assert r.status_code == 403


def test_config_exposes_signup_mode(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "signup_mode", "invite_only")
    body = client.get("/api/config").json()
    assert body["signup_mode"] == "invite_only"
    assert "open_registration" not in body


# ---- operator plane gating (AL-91) ---------------------------------------------
def test_admin_plane_404s_for_non_admin(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "platform_admin_emails", "")  # nobody is an operator
    auth = _login(client, ADMIN_EMAIL)
    assert client.get("/api/admin/invites", headers=auth).status_code == 404
    assert client.post("/api/admin/invites", json={"email": "x@example.com"}, headers=auth).status_code == 404


def test_admin_plane_404s_on_self_host(client, monkeypatch):
    """Hosted-only: with HOSTED_MODE off the operator plane doesn't exist."""
    from app.config import settings

    monkeypatch.setattr(settings, "platform_admin_emails", ADMIN_EMAIL)
    auth = _login(client, ADMIN_EMAIL)
    assert client.get("/api/admin/invites", headers=auth).status_code == 404


def test_admin_plane_requires_auth(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    assert client.get("/api/admin/invites").status_code == 401


# ---- platform invite lifecycle -------------------------------------------------
def test_platform_invite_onboards_a_new_tenant(client, operator, monkeypatch):
    """The whole point: invite-only on, and a brand-new person still gets from an
    emailed link to their own org."""
    from app.config import settings
    from app.services.email import outbox

    monkeypatch.setattr(settings, "signup_mode", "invite_only")
    r = client.post("/api/admin/invites", json={"email": "founder@example.com"}, headers=operator)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "platform"
    assert r.json()["org_id"] is None
    assert len(outbox) == 1
    token = _token_from_outbox()

    # Preview tells the accept page this is the found-your-own-org flow.
    prev = client.get(f"/api/invites/{token}/preview").json()
    assert prev["kind"] == "platform" and prev["org_name"] == ""

    # Register past the invite-only gate, then found an org.
    assert client.post("/api/auth/register", json={
        "name": "Fou Nder", "handle": "founder", "email": "founder@example.com",
        "password": "sup3rsecret", "invite_token": token,
    }).status_code == 201
    founder = _login(client, "founder@example.com", "sup3rsecret")
    assert client.get("/api/orgs", headers=founder).json() == []  # no org yet
    org = client.post("/api/orgs", json={"name": "Founder Co"}, headers=founder)
    assert org.status_code == 201
    assert org.json()["role"] == "owner"


def test_platform_invite_plan_preset_applies_to_founded_org(client, operator, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "signup_mode", "invite_only")
    client.post("/api/admin/invites", json={"email": "partner@example.com", "plan": "team"},
                headers=operator)
    token = _token_from_outbox()
    client.post("/api/auth/register", json={
        "name": "Design Partner", "handle": "partner", "email": "partner@example.com",
        "password": "sup3rsecret", "invite_token": token,
    })
    partner = _login(client, "partner@example.com", "sup3rsecret")
    org = client.post("/api/orgs", json={"name": "Partner Co"}, headers=partner).json()
    assert org["plan"] == "team"  # seeded straight onto the invited tier


def test_platform_invite_rejects_unknown_plan(client, operator):
    r = client.post("/api/admin/invites",
                    json={"email": "x@example.com", "plan": "unobtainium"}, headers=operator)
    assert r.status_code == 422


def test_platform_invite_refused_for_existing_account(client, operator):
    """Platform invites are for net-new customers; an existing user needs the
    additional-org request flow (AL-92) instead."""
    r = client.post("/api/admin/invites", json={"email": "dana@ascme-labs.com"}, headers=operator)
    assert r.status_code == 409
    assert "additional-org" in r.json()["detail"]


def test_existing_user_cannot_redeem_platform_invite_via_accept(client, operator):
    """A signed-in account can't consume a platform invite through the org-join path."""
    client.post("/api/admin/invites", json={"email": "someone@example.com"}, headers=operator)
    token = _token_from_outbox()
    dana = _login(client, "dana@ascme-labs.com")
    r = client.post("/api/invites/accept", json={"token": token}, headers=dana)
    assert r.status_code == 400
    assert "platform invitation" in r.json()["detail"]


def test_platform_invite_email_must_match_on_register(client, operator):
    client.post("/api/admin/invites", json={"email": "a@example.com"}, headers=operator)
    token = _token_from_outbox()
    r = client.post("/api/auth/register", json={
        "name": "Imp Oster", "handle": "imposter", "email": "b@example.com",
        "password": "sup3rsecret", "invite_token": token,
    })
    assert r.status_code == 403


def test_platform_invite_reinvite_refreshes_not_duplicates(client, operator):
    from app.services.email import outbox

    first = client.post("/api/admin/invites", json={"email": "again@example.com"}, headers=operator).json()
    second = client.post("/api/admin/invites", json={"email": "again@example.com"}, headers=operator).json()
    assert first["id"] == second["id"]  # same invite, refreshed
    assert len(client.get("/api/admin/invites", headers=operator).json()) == 1
    assert len(outbox) == 2  # but the link was re-sent


def test_platform_invite_revoke_kills_the_link(client, operator, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "signup_mode", "invite_only")
    inv = client.post("/api/admin/invites", json={"email": "gone@example.com"}, headers=operator).json()
    token = _token_from_outbox()
    assert client.delete(f"/api/admin/invites/{inv['id']}", headers=operator).status_code == 204
    assert client.get(f"/api/invites/{token}/preview").status_code == 404
    assert client.post("/api/auth/register", json={
        "name": "Too Late", "handle": "toolate", "email": "gone@example.com",
        "password": "sup3rsecret", "invite_token": token,
    }).status_code == 404


def test_platform_invites_do_not_leak_into_org_invite_list(client, operator):
    """An operator's platform invites must never appear in a tenant's member list."""
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=operator).json()
    client.post("/api/admin/invites", json={"email": "outside@example.com"}, headers=operator)
    org_invites = client.get(f"/api/orgs/{org['id']}/invites", headers=operator).json()
    assert org_invites == []


def test_config_has_users_true_when_a_user_exists(client):
    """GRPH-1581: a box with users opens on Sign in."""
    body = client.get("/api/config").json()
    assert body["has_users"] is True
    assert set(body) == {"hosted_mode", "signup_mode", "has_users"}  # a bare flag, nothing about who


def test_config_has_users_false_on_a_zero_user_box():
    """GRPH-1581: no `client` fixture, so no lifespan seed — the DB really has zero users."""
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get("/api/config").json()  # no `with`: lifespan (the seed) not run
    assert body["has_users"] is False
