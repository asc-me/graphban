"""AL-74b: the hosted-only org router, invites, and onboarding wiring.

Everything here runs against a monkeypatched ``settings.hosted_mode = True`` — the
org surface 404s otherwise (see the first test). Seeded users (alex/dana/…) all log
in with the seed password; new invitees are created through the register flow.
"""
import pytest

SEED_PW = "graphban"


def _login(client, email, password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def hosted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    return settings


@pytest.fixture(autouse=True)
def _clear_outbox():
    from app.services.email import outbox

    outbox.clear()
    yield
    outbox.clear()


def _token_of(client, invite: dict) -> str:
    """The accept link is what the API exposes; the raw token never is."""
    return invite["accept_url"].rsplit("/", 1)[-1]


def _make_org(client, headers, name="Acme"):
    r = client.post("/api/orgs", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ---- surface gating -----------------------------------------------------------
def test_org_surface_404s_on_self_host(client):
    """With HOSTED_MODE off, the whole org surface is invisible (404), even to an
    authenticated user — orgs are SaaS-only."""
    auth = _login(client, "alex@ascme-labs.com")
    assert client.get("/api/orgs", headers=auth).status_code == 404
    assert client.post("/api/orgs", json={"name": "X"}, headers=auth).status_code == 404


def test_org_surface_requires_auth(client, hosted):
    assert client.get("/api/orgs").status_code == 401


# ---- org CRUD -----------------------------------------------------------------
def test_create_and_list_orgs(client, hosted):
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth, "Acme")
    assert org["role"] == "owner"
    assert org["plan"] == "free"

    listed = client.get("/api/orgs", headers=auth).json()
    assert [o["id"] for o in listed] == [org["id"]]
    assert listed[0]["role"] == "owner"


# ---- projects under an org ----------------------------------------------------
def test_project_created_under_sole_org_is_reachable(client, hosted):
    """A single-org user needn't name the org; the project inherits it and stays
    reachable through the AL-74 gate."""
    auth = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, auth)
    r = client.post("/api/projects", json={"name": "Rocket"}, headers=auth)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    # Reachable: the creator is both a project member and an org member.
    assert client.get(f"/api/items?project_id={pid}", headers=auth).status_code == 200


def test_multi_org_project_requires_org_id(client, hosted, monkeypatch):
    # Founding a second org normally needs approval (AL-92); this test is about project
    # org_id resolution, so grant the entitlement on the default plan rather than
    # threading the whole request/approve flow through it.
    from app.services import quotas

    monkeypatch.setitem(
        quotas.PLANS, "free",
        quotas.Plan(**{**quotas.PLANS["free"].__dict__, "may_found_additional_orgs": True}),
    )
    auth = _login(client, "alex@ascme-labs.com")
    _make_org(client, auth, "Acme")
    org2 = _make_org(client, auth, "Beta")
    # Ambiguous → must specify.
    r = client.post("/api/projects", json={"name": "Rocket"}, headers=auth)
    assert r.status_code == 422
    # Explicit org_id works.
    r = client.post("/api/projects", json={"name": "Rocket", "org_id": org2["id"]}, headers=auth)
    assert r.status_code == 201


def test_cannot_create_project_in_foreign_org(client, hosted):
    """dana can't drop a project into alex's org."""
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    dana = _login(client, "dana@ascme-labs.com")
    r = client.post("/api/projects", json={"name": "Sneaky", "org_id": org["id"]}, headers=dana)
    assert r.status_code == 404  # org existence hidden from non-members


# ---- invites ------------------------------------------------------------------
def test_invite_emails_and_registration_joins_org(client, hosted, monkeypatch):
    """Full onboarding path: owner invites an email → an email with an accept link is
    sent → the invitee registers with the token (even though open registration is
    OFF) and lands inside the org as the invited role."""
    from app.config import settings
    from app.services.email import outbox

    monkeypatch.setattr(settings, "signup_mode", "invite_only")
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")

    r = client.post(f"/api/orgs/{org['id']}/invites",
                    json={"email": "newbie@example.com", "role": "admin"}, headers=alex)
    assert r.status_code == 201, r.text
    invite = r.json()
    assert "/invite/" in invite["accept_url"]
    assert len(outbox) == 1
    sent = outbox[0]
    assert sent.to == "newbie@example.com"
    token = sent.text.rsplit("/invite/", 1)[1].split()[0]

    # Registration is closed, but the invite is authorization enough.
    assert client.post("/api/auth/register", json={
        "name": "New Bie", "handle": "newbie", "email": "newbie@example.com",
        "password": "sup3rsecret", "invite_token": token,
    }).status_code == 201

    newbie = _login(client, "newbie@example.com", "sup3rsecret")
    orgs = client.get("/api/orgs", headers=newbie).json()
    assert [o["id"] for o in orgs] == [org["id"]]
    assert orgs[0]["role"] == "admin"


def test_registration_still_closed_without_invite(client, hosted, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "signup_mode", "invite_only")
    r = client.post("/api/auth/register", json={
        "name": "No One", "handle": "noone", "email": "noone@example.com", "password": "sup3rsecret",
    })
    assert r.status_code == 403


def test_register_invite_email_must_match(client, hosted):
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    from app.services.email import outbox

    client.post(f"/api/orgs/{org['id']}/invites", json={"email": "a@example.com"}, headers=alex)
    token = outbox[0].text.rsplit("/invite/", 1)[1].split()[0]
    # Registering a mismatched email with someone else's token is refused.
    r = client.post("/api/auth/register", json={
        "name": "Imp Oster", "handle": "imposter", "email": "b@example.com",
        "password": "sup3rsecret", "invite_token": token,
    })
    assert r.status_code == 403


def test_existing_user_accepts_invite(client, hosted):
    """An already-registered user (dana) joins via POST /invites/accept."""
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    from app.services.email import outbox

    client.post(f"/api/orgs/{org['id']}/invites",
                json={"email": "dana@ascme-labs.com", "role": "member"}, headers=alex)
    token = outbox[0].text.rsplit("/invite/", 1)[1].split()[0]

    dana = _login(client, "dana@ascme-labs.com")
    r = client.post("/api/invites/accept", json={"token": token}, headers=dana)
    assert r.status_code == 200
    assert r.json()["role"] == "member"
    assert org["id"] in [o["id"] for o in client.get("/api/orgs", headers=dana).json()]


def test_accept_wrong_email_refused(client, hosted):
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    from app.services.email import outbox

    client.post(f"/api/orgs/{org['id']}/invites",
                json={"email": "someone@example.com"}, headers=alex)
    token = outbox[0].text.rsplit("/invite/", 1)[1].split()[0]
    dana = _login(client, "dana@ascme-labs.com")  # dana's email ≠ invited email
    assert client.post("/api/invites/accept", json={"token": token}, headers=dana).status_code == 403


def test_member_cannot_invite(client, hosted):
    """A plain member has no authority to invite (owner/admin only)."""
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    from app.services.email import outbox

    client.post(f"/api/orgs/{org['id']}/invites",
                json={"email": "dana@ascme-labs.com", "role": "member"}, headers=alex)
    token = outbox[0].text.rsplit("/invite/", 1)[1].split()[0]
    dana = _login(client, "dana@ascme-labs.com")
    client.post("/api/invites/accept", json={"token": token}, headers=dana)
    # dana is now a member — inviting should be forbidden.
    r = client.post(f"/api/orgs/{org['id']}/invites",
                    json={"email": "x@example.com"}, headers=dana)
    assert r.status_code == 403


def test_non_member_cannot_invite_or_list(client, hosted):
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    dana = _login(client, "dana@ascme-labs.com")  # never joined
    assert client.get(f"/api/orgs/{org['id']}/members", headers=dana).status_code == 404
    assert client.post(f"/api/orgs/{org['id']}/invites",
                       json={"email": "x@example.com"}, headers=dana).status_code == 404


# ---- invite preview + lifecycle ----------------------------------------------
def test_invite_preview_is_unauthenticated(client, hosted):
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    from app.services.email import outbox

    client.post(f"/api/orgs/{org['id']}/invites", json={"email": "p@example.com"}, headers=alex)
    token = outbox[0].text.rsplit("/invite/", 1)[1].split()[0]

    r = client.get(f"/api/invites/{token}/preview")  # no auth header
    assert r.status_code == 200
    body = r.json()
    assert body["org_name"] == "Acme"
    assert body["email"] == "p@example.com"
    assert client.get("/api/invites/bogus-token/preview").status_code == 404


def test_revoke_invite_kills_the_link(client, hosted):
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    r = client.post(f"/api/orgs/{org['id']}/invites",
                    json={"email": "dana@ascme-labs.com"}, headers=alex)
    invite_id = r.json()["id"]
    from app.services.email import outbox

    token = outbox[0].text.rsplit("/invite/", 1)[1].split()[0]

    assert client.delete(f"/api/orgs/{org['id']}/invites/{invite_id}", headers=alex).status_code == 204
    dana = _login(client, "dana@ascme-labs.com")
    assert client.post("/api/invites/accept", json={"token": token}, headers=dana).status_code == 404
    assert client.get(f"/api/invites/{token}/preview").status_code == 404


def test_expired_invite_refused(client, hosted):
    from datetime import timedelta

    from app.db import SessionLocal
    from app.models import OrgInvite, utcnow

    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    r = client.post(f"/api/orgs/{org['id']}/invites",
                    json={"email": "dana@ascme-labs.com"}, headers=alex)
    invite_id = r.json()["id"]

    db = SessionLocal()
    try:
        inv = db.get(OrgInvite, invite_id)
        inv.expires_at = utcnow() - timedelta(days=1)
        db.commit()
        token = inv.token
    finally:
        db.close()

    dana = _login(client, "dana@ascme-labs.com")
    assert client.post("/api/invites/accept", json={"token": token}, headers=dana).status_code == 410
    assert client.get(f"/api/invites/{token}/preview").status_code == 410


# ---- PRD-21 screen B: what the Users & access table reads ----------------------
def test_member_listing_carries_project_access_and_last_write(client, hosted):
    """The org-admin table answers "who is here, what can they reach, are they working".
    Role alone answers none of the second two."""
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    client.post("/api/projects", json={"name": "Rocket"}, headers=alex)

    rows = client.get(f"/api/orgs/{org['id']}/members", headers=alex).json()
    me = next(r for r in rows if r["user"]["email"] == "alex@ascme-labs.com")
    assert me["role"] == "owner"
    assert [a["name"] for a in me["access"]] == ["Rocket"]
    assert me["access"][0]["level"] == "write"
    assert me["access"][0]["tag"]  # the tag is what the project URL is built from
    # Creating the org and the project are both recorded writes.
    assert me["last_write_at"] is not None


def test_no_project_access_is_an_empty_list_never_null(client, hosted):
    """"Reaches nothing" and "we did not look" must not render the same way, so access is
    always a list — an absent grant is an empty one, not a null."""
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    inv = client.post(f"/api/orgs/{org['id']}/invites",
                      json={"email": "dana@ascme-labs.com"}, headers=alex).json()
    dana = _login(client, "dana@ascme-labs.com")
    client.post("/api/invites/accept", json={"token": _token_of(client, inv)}, headers=dana)

    rows = client.get(f"/api/orgs/{org['id']}/members", headers=alex).json()
    dana_row = next(r for r in rows if r["user"]["email"] == "dana@ascme-labs.com")
    assert dana_row["access"] == []


def test_member_access_does_not_leak_another_orgs_projects(client, hosted):
    """Access is filtered to the projects of THIS org. A membership in someone else's
    project must not appear on this org's roster."""
    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Acme")
    rows = client.get(f"/api/orgs/{org['id']}/members", headers=alex).json()
    me = next(r for r in rows if r["user"]["email"] == "alex@ascme-labs.com")
    # The seeded demo projects belong to no org, so none of them may show up here.
    assert me["access"] == []
