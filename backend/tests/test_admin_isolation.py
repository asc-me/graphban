"""AL-95: adversarial sweep of the operator plane + audit assertions.

The admin plane is the ONE deliberate cross-tenant surface in the product, so it gets
the same treatment AL-76 gave tenant isolation — where that sweep caught two real leaks.

Three invariants, each asserted against every admin route *discovered from the live
OpenAPI schema* rather than a hand-maintained list, so a route added later is covered
automatically and can't quietly ship ungated:

1. Non-admins get 404 everywhere (existence-hiding, not 403).
2. No tenant CONTENT crosses the plane — only metadata.
3. Every operator mutation lands in the audit ledger, attributed to the acting admin.

Plus the inverse check: being a platform admin must NOT grant access to tenant data
through the normal tenant APIs.
"""
import pytest

SEED_PW = "graphban"
ADMIN_EMAIL = "alex@ascme-labs.com"

# Distinctive strings planted in tenant content; none may ever appear in an admin response.
MARKERS = {
    "item": "ZZMARKERITEMZZ",
    "shard": "ZZMARKERMEMORYZZ",
    "prd": "ZZMARKERPRDZZ",
    "request": "ZZMARKERREQUESTZZ",
}


def _login(client, email, password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def admin_routes():
    """Every /api/admin/* route, from the live schema. A new admin route is swept the
    moment it exists — the point of deriving this instead of hardcoding it."""
    from app.main import app

    out = []
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/api/admin"):
            continue
        for method in ops:
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                # Path params: any placeholder works — a non-admin must 404 regardless
                # of whether the resource exists.
                concrete = path.replace("{invite_id}", "inv_x").replace("{request_id}", "oreq_x")
                out.append((method.upper(), concrete))
    return out


def test_schema_actually_exposes_admin_routes():
    """Guard the guard: if this returns nothing, the sweeps below are vacuous."""
    assert len(admin_routes()) >= 5


@pytest.fixture()
def hosted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    return settings


@pytest.fixture()
def operator(client, hosted, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "platform_admin_emails", ADMIN_EMAIL)
    return _login(client, ADMIN_EMAIL)


# ---- 1. gating: every route, every method -------------------------------------
def test_no_admin_route_is_reachable_by_a_tenant(client, hosted, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "platform_admin_emails", "")  # nobody is an operator
    auth = _login(client, "dana@ascme-labs.com")
    for method, path in admin_routes():
        r = client.request(method, path, json={}, headers=auth)
        assert r.status_code == 404, f"{method} {path} reachable by tenant: {r.status_code}"


def test_no_admin_route_exists_on_self_host(client, monkeypatch):
    """HOSTED_MODE off: the operator plane is absent even for an allowlisted email."""
    from app.config import settings

    monkeypatch.setattr(settings, "platform_admin_emails", ADMIN_EMAIL)
    auth = _login(client, ADMIN_EMAIL)
    for method, path in admin_routes():
        r = client.request(method, path, json={}, headers=auth)
        assert r.status_code == 404, f"{method} {path} exists on self-host: {r.status_code}"


def test_no_admin_route_is_reachable_anonymously(client, hosted):
    for method, path in admin_routes():
        r = client.request(method, path, json={})
        assert r.status_code in (401, 404), f"{method} {path} anon: {r.status_code}"


# ---- 2. the metadata boundary --------------------------------------------------
@pytest.fixture()
def tenant_with_content(client, operator):
    """A tenant stuffed with marked content, owned by someone other than the operator.

    Also leaves a pending platform invite and a pending org request, so EVERY admin GET
    returns populated data when the content sweep runs — an empty list would make those
    assertions trivially true."""
    dana = _login(client, "dana@ascme-labs.com")
    client.post("/api/orgs", json={"name": "Dana Co"}, headers=dana)
    pid = client.post("/api/projects", json={"name": "Dana Project"}, headers=dana).json()["id"]
    client.post("/api/items", json={"title": MARKERS["item"], "project_id": pid}, headers=dana)
    client.post("/api/memory/shards", json={"text": MARKERS["shard"], "project_id": pid}, headers=dana)
    client.post("/api/prds", json={"title": MARKERS["prd"], "project_id": pid}, headers=dana)
    client.post("/api/requests", json={"type": "bug", "title": MARKERS["request"], "project_id": pid},
                headers=dana)
    # Populate the operator-plane collections too.
    client.post("/api/admin/invites", json={"email": "pending@example.com"}, headers=operator)
    client.post("/api/orgs/requests", json={"reason": "a second workspace"}, headers=dana)
    return {"dana": dana, "project_id": pid}


def _populated_admin_gets(client, operator) -> int:
    n = 0
    for method, path in admin_routes():
        if method == "GET" and client.get(path, headers=operator).status_code == 200:
            n += 1
    return n


def test_content_sweep_actually_inspects_populated_responses(client, operator, tenant_with_content):
    """Guard against a vacuous sweep: every admin GET must return data to inspect."""
    assert _populated_admin_gets(client, operator) == 6
    for path in ("/api/admin/orgs", "/api/admin/users", "/api/admin/invites",
                 "/api/admin/activity", "/api/admin/org-requests"):
        assert len(client.get(path, headers=operator).json()) > 0, f"{path} was empty"


def test_no_admin_response_contains_tenant_content(client, operator, tenant_with_content):
    """The boundary that keeps the Phase 6 guarantee honest: operators see metadata,
    never what a customer wrote."""
    for method, path in admin_routes():
        if method != "GET":
            continue
        r = client.get(path, headers=operator)
        if r.status_code != 200:
            continue
        body = r.text
        for kind, marker in MARKERS.items():
            assert marker not in body, f"GET {path} leaked tenant {kind} content"


def test_admin_responses_carry_no_credential_material(client, operator, tenant_with_content):
    for method, path in admin_routes():
        if method != "GET":
            continue
        r = client.get(path, headers=operator)
        if r.status_code != 200:
            continue
        for secret in ("password_hash", "token_version", "hashed_key", "api_key"):
            assert secret not in r.text, f"GET {path} exposed {secret}"


def test_platform_admin_is_not_a_backdoor_into_tenant_apis(client, operator, tenant_with_content):
    """The inverse invariant: operator status grants NOTHING through the normal tenant
    endpoints. Alex is a platform admin but not a member of Dana's org."""
    pid = tenant_with_content["project_id"]
    for path in (
        f"/api/items?project_id={pid}",
        f"/api/memory/shards?project_id={pid}",
        f"/api/prds?project_id={pid}",
        f"/api/requests?project_id={pid}",
        f"/api/dashboard?project_id={pid}",
    ):
        r = client.get(path, headers=operator)
        assert r.status_code in (403, 404), f"platform admin reached tenant data via {path}"


# ---- 3. auditing ---------------------------------------------------------------
def _events(action: str):
    from app.db import SessionLocal
    from app.models import Event

    db = SessionLocal()
    try:
        return [
            {"actor_id": e.actor_id, "actor_type": e.actor_type, "action": e.action}
            for e in db.query(Event).filter(Event.action == action).all()
        ]
    finally:
        db.close()


def test_invite_lifecycle_is_audited(client, operator):
    inv = client.post("/api/admin/invites", json={"email": "audited@example.com"},
                      headers=operator).json()
    created = _events("create_platform_invite")
    assert created and created[0]["actor_type"] == "user" and created[0]["actor_id"] == "u1"

    client.delete(f"/api/admin/invites/{inv['id']}", headers=operator)
    assert _events("revoke_platform_invite"), "revoking a platform invite was not audited"


def test_plan_assignment_is_audited(client, operator):
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=operator).json()
    client.put(f"/api/orgs/{org['id']}/plan", json={"plan": "team"}, headers=operator)
    evs = _events("set_org_plan")
    assert evs and evs[0]["actor_id"] == "u1"


def test_org_request_decision_is_audited(client, operator):
    dana = _login(client, "dana@ascme-labs.com")
    client.post("/api/orgs", json={"name": "Dana Co"}, headers=dana)
    rid = client.post("/api/orgs/requests", json={"reason": "second workspace"},
                      headers=dana).json()["id"]
    client.post(f"/api/admin/org-requests/{rid}", json={"approve": True}, headers=operator)
    evs = _events("decide_org_request")
    assert evs and evs[0]["actor_id"] == "u1", "operator decision was not attributed"
