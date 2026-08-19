"""AL-94: the operator console's API surface.

Everything here is gated twice (hosted + platform-admin allowlist) and 404s otherwise,
and returns METADATA ONLY — orgs, plans, usage, identity. The exhaustive
no-tenant-content / audit matrix lives in AL-95; these cover the endpoints themselves.
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
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "platform_admin_emails", ADMIN_EMAIL)
    return _login(client, ADMIN_EMAIL)


# ---- gating --------------------------------------------------------------------
ADMIN_GETS = ["/api/admin/me", "/api/admin/orgs", "/api/admin/users",
              "/api/admin/invites", "/api/admin/org-requests"]


def test_every_admin_route_404s_for_non_admin(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "platform_admin_emails", "")  # nobody is an operator
    auth = _login(client, ADMIN_EMAIL)
    for path in ADMIN_GETS:
        assert client.get(path, headers=auth).status_code == 404, path


def test_every_admin_route_404s_on_self_host(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "platform_admin_emails", ADMIN_EMAIL)
    auth = _login(client, ADMIN_EMAIL)
    for path in ADMIN_GETS:
        assert client.get(path, headers=auth).status_code == 404, path


def test_admin_routes_require_auth(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    for path in ADMIN_GETS:
        assert client.get(path).status_code == 401, path


def test_whoami_identifies_the_operator(client, operator):
    body = client.get("/api/admin/me", headers=operator).json()
    assert body["is_platform_admin"] is True
    assert body["email"] == ADMIN_EMAIL


# ---- orgs ----------------------------------------------------------------------
def test_org_listing_reports_owner_plan_and_usage(client, operator):
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=operator).json()
    client.post("/api/projects", json={"name": "Rocket"}, headers=operator)

    rows = client.get("/api/admin/orgs", headers=operator).json()
    row = next(r for r in rows if r["id"] == org["id"])
    assert row["name"] == "Acme"
    assert row["owner_email"] == ADMIN_EMAIL
    assert row["plan"] == "free"
    assert row["usage"]["projects"] == 1
    assert row["usage"]["seats"] == 1
    assert row["limits"]["max_projects"] > 0


def test_org_listing_spans_tenants(client, operator):
    """The operator sees every tenant — that's the point of the plane."""
    client.post("/api/orgs", json={"name": "Acme"}, headers=operator)
    dana = _login(client, "dana@ascme-labs.com")
    client.post("/api/orgs", json={"name": "Beta Co"}, headers=dana)
    names = {r["name"] for r in client.get("/api/admin/orgs", headers=operator).json()}
    assert {"Acme", "Beta Co"} <= names


def test_org_listing_exposes_no_tenant_content(client, operator):
    """Metadata only — the isolation boundary. No item/memory/prd fields anywhere."""
    client.post("/api/orgs", json={"name": "Acme"}, headers=operator)
    pid = client.post("/api/projects", json={"name": "Rocket"}, headers=operator).json()["id"]
    client.post("/api/items", json={"title": "secret item", "project_id": pid}, headers=operator)
    client.post("/api/memory/shards", json={"text": "secret memory", "project_id": pid},
                headers=operator)

    blob = client.get("/api/admin/orgs", headers=operator).text
    assert "secret item" not in blob
    assert "secret memory" not in blob


def test_plan_assignment_reflects_in_org_listing(client, operator):
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=operator).json()
    client.put(f"/api/orgs/{org['id']}/plan", json={"plan": "team"}, headers=operator)
    rows = client.get("/api/admin/orgs", headers=operator).json()
    assert next(r for r in rows if r["id"] == org["id"])["plan"] == "team"


# ---- users ---------------------------------------------------------------------
def test_user_listing_is_identity_plus_org_count(client, operator):
    client.post("/api/orgs", json={"name": "Acme"}, headers=operator)
    rows = client.get("/api/admin/users", headers=operator).json()
    me = next(r for r in rows if r["email"] == ADMIN_EMAIL)
    assert me["handle"] == "ascme"
    assert me["org_count"] == 1
    # No credential material ever leaves the plane.
    assert "password_hash" not in me and "token_version" not in me


# ---- PRD-21: what the four operator screens read -------------------------------
def _token(invite: dict) -> str:
    """The accept link is what the plane exposes; the raw token never is."""
    return invite["accept_url"].rsplit("/", 1)[-1]


def test_whoami_reports_deployment_policy_not_a_guess(client, operator):
    """The Licensing screen states the signup mode and the expiry window. Both are env
    config, so they come from the server rather than being inferred client-side —
    otherwise the console would describe a policy it isn't actually running."""
    from app.config import settings

    body = client.get("/api/admin/me", headers=operator).json()
    assert body["signup_mode"] == settings.signup_mode
    assert body["invite_expiry_days"] == settings.invite_expiry_days


def test_org_listing_carries_its_members(client, operator):
    """The org drawer lists who is seated. Identity + role, never anything to act on."""
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=operator).json()
    row = next(r for r in client.get("/api/admin/orgs", headers=operator).json()
               if r["id"] == org["id"])
    assert row["owner_handle"] == "ascme"
    assert [m["handle"] for m in row["members"]] == ["ascme"]
    assert row["members"][0]["role"] == "owner"
    assert row["usage"]["seats"] == len(row["members"])  # no invite outstanding yet


def test_a_pending_invite_holds_a_seat_no_member_occupies(client, operator):
    """`seat_count` is memberships PLUS pending invites, so the two numbers diverge the
    moment anyone is invited. The console has to reconcile them out loud — a member list
    shorter than the seat bar beside it, with nothing said, reads as a miscount."""
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=operator).json()
    client.post(f"/api/orgs/{org['id']}/invites",
                json={"email": "dana@ascme-labs.com", "role": "member"}, headers=operator)

    row = next(r for r in client.get("/api/admin/orgs", headers=operator).json()
               if r["id"] == org["id"])
    assert row["usage"]["seats"] == 2
    assert len(row["members"]) == 1


def test_org_members_are_ranked_by_role_not_by_join_order(client, operator):
    """The console reads the head of this list as "who to contact", so the order has to
    be role rank. The member here joins BEFORE the admin — sorted by join time the list
    would come back owner/member/admin, which is the arrangement this rules out."""
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=operator).json()
    for email, role in (("dana@ascme-labs.com", "member"), ("ops@ascme-labs.com", "admin")):
        inv = client.post(f"/api/orgs/{org['id']}/invites", json={"email": email, "role": role},
                          headers=operator).json()
        client.post("/api/invites/accept", json={"token": _token(inv)},
                    headers=_login(client, email))

    row = next(r for r in client.get("/api/admin/orgs", headers=operator).json()
               if r["id"] == org["id"])
    assert [m["role"] for m in row["members"]] == ["owner", "admin", "member"]


def test_user_listing_names_the_orgs_not_just_a_count(client, operator):
    """A support lookup is "which tenants is this person in", so the count alone is the
    absence — it says a number without saying which."""
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=operator).json()
    me = next(r for r in client.get("/api/admin/users", headers=operator).json()
              if r["email"] == ADMIN_EMAIL)
    assert me["org_count"] == 1
    assert [(o["id"], o["role"]) for o in me["orgs"]] == [(org["id"], "owner")]


def test_last_write_is_null_for_an_account_that_has_written_nothing(client, operator):
    """NULL means "no write on record" — distinct from idle, because reads leave no
    ledger row at all. Rendering the two the same way is the defect this separates."""
    rows = client.get("/api/admin/users", headers=operator).json()
    quiet = next(r for r in rows if r["email"] == "dana@ascme-labs.com")
    assert quiet["last_write_at"] is None

    client.post("/api/admin/invites", json={"email": "founder@example.com"}, headers=operator)
    acting = next(r for r in client.get("/api/admin/users", headers=operator).json()
                  if r["email"] == ADMIN_EMAIL)
    assert acting["last_write_at"] is not None


# ---- licensing -----------------------------------------------------------------
def test_invite_listing_hides_history_by_default(client, operator):
    inv = client.post("/api/admin/invites", json={"email": "founder@example.com"},
                      headers=operator).json()
    client.delete(f"/api/admin/invites/{inv['id']}", headers=operator)
    assert client.get("/api/admin/invites", headers=operator).json() == []


def test_invite_history_keeps_the_revoked_row(client, operator):
    """Revoking closes a link; it must not erase that the link was ever issued."""
    inv = client.post("/api/admin/invites", json={"email": "founder@example.com"},
                      headers=operator).json()
    client.delete(f"/api/admin/invites/{inv['id']}", headers=operator)
    rows = client.get("/api/admin/invites?history=true", headers=operator).json()
    row = next(r for r in rows if r["id"] == inv["id"])
    assert row["status"] == "revoked"
    assert row["invited_by_handle"] == "ascme"


def test_invite_carries_the_org_it_actually_produced(client, operator, monkeypatch):
    """"Redeemed" and "redeemed into something" are two facts. The org name appears
    only once the account the invite seeded has actually founded one."""
    from app.config import settings

    monkeypatch.setattr(settings, "signup_mode", "invite_only")
    inv = client.post("/api/admin/invites", json={"email": "founder@harbor.dev", "plan": "team"},
                      headers=operator).json()
    assert client.post("/api/auth/register", json={
        "name": "Fou Nder", "handle": "harborfounder", "email": "founder@harbor.dev",
        "password": "sup3rsecret", "invite_token": _token(inv),
    }).status_code == 201
    founder = _login(client, "founder@harbor.dev", "sup3rsecret")

    # Accepted, but nothing founded yet — the field stays empty rather than guessing.
    row = next(r for r in client.get("/api/admin/invites?history=true", headers=operator).json()
               if r["id"] == inv["id"])
    assert row["status"] == "accepted"
    assert row["redeemed_org_name"] == ""

    org = client.post("/api/orgs", json={"name": "Harbor"}, headers=founder).json()
    row = next(r for r in client.get("/api/admin/invites?history=true", headers=operator).json()
               if r["id"] == inv["id"])
    assert row["redeemed_org_id"] == org["id"]
    assert row["redeemed_org_name"] == "Harbor"


# ---- the operator ledger -------------------------------------------------------
def test_activity_records_operator_actions(client, operator):
    client.post("/api/admin/invites", json={"email": "founder@example.com"}, headers=operator)
    rows = client.get("/api/admin/activity", headers=operator).json()
    assert rows[0]["action"] == "create_platform_invite"
    assert rows[0]["actor_label"]


def test_activity_is_the_operator_ledger_not_a_tenant_feed(client, operator):
    """The panel is sourced from an allowlist of plane actions, not from "events with no
    project" — so tenant work never leaks across the boundary, however it was recorded."""
    dana = _login(client, "dana@ascme-labs.com")
    client.post("/api/orgs", json={"name": "Dana Co"}, headers=dana)
    pid = client.post("/api/projects", json={"name": "Dana Project"}, headers=dana).json()["id"]
    client.post("/api/items", json={"title": "ZZTENANTWORKZZ", "project_id": pid}, headers=dana)

    r = client.get("/api/admin/activity", headers=operator)
    assert "ZZTENANTWORKZZ" not in r.text
    assert all(a["action"] in ("create_platform_invite", "revoke_platform_invite",
                               "decide_org_request", "set_org_plan") for a in r.json())
