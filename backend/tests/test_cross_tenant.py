"""Phase 6 (AL-76): the cross-tenant isolation safety net.

The adversarial setup is deliberately the *hardest* case: the attacker (alex, org A)
also holds a stray per-project ``Membership`` row on the victim's project (org B) —
a leaked/legacy grant. In HOSTED_MODE the org gate (AL-74) must still deny every
surface, because a project can only be reached from inside its own tenant org. A
bare non-member would be trivially blocked; proving the leaked-membership case
proves the gate, not just the membership check.

Coverage: every project-scoped REST read/write (items, memory, PRDs, analytics,
requests, project settings), the org endpoints, the MCP dispatcher, and the public
surface — plus positive controls, a self-host regression, and a hosted E2E loop.
Runs on both engines via the existing two-engine gate.
"""
import pytest

SEED_PW = "graphban"


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": SEED_PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_org(client, headers, name):
    r = client.post("/api/orgs", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_project(client, headers, name):
    r = client.post("/api/projects", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_project(client, headers, pid):
    """Populate a project with one of each resource; return their ids."""
    item = client.post("/api/items", json={"title": "secret item", "project_id": pid}, headers=headers)
    assert item.status_code == 201, item.text
    shard = client.post("/api/memory/shards", json={"text": "secret memory", "project_id": pid}, headers=headers)
    assert shard.status_code == 201, shard.text
    prd = client.post("/api/prds", json={"title": "secret prd", "project_id": pid}, headers=headers)
    assert prd.status_code == 201, prd.text
    req = client.post("/api/requests", json={"type": "bug", "title": "secret req", "project_id": pid}, headers=headers)
    assert req.status_code == 201, req.text
    return {"item": item.json()["id"], "shard": shard.json()["id"],
            "prd": prd.json()["id"], "request": req.json()["id"]}


@pytest.fixture()
def tenants(client, monkeypatch):
    """Two fully-populated tenants + a leaked cross-org Membership for the attacker."""
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)

    alex = _login(client, "alex@ascme-labs.com")   # org A owner (u1)
    dana = _login(client, "dana@ascme-labs.com")    # org B owner (u2)

    org_a = _make_org(client, alex, "Org A")
    p_a = _make_project(client, alex, "Project A")
    ids_a = _seed_project(client, alex, p_a)

    org_b = _make_org(client, dana, "Org B")
    p_b = _make_project(client, dana, "Project B")
    ids_b = _seed_project(client, dana, p_b)

    # Leak alex a direct write Membership on org B's project. The org gate must still
    # deny — a per-project grant is not authority once orgs are in play.
    from app.db import SessionLocal
    from app.models import Membership

    db = SessionLocal()
    try:
        db.add(Membership(user_id="u1", project_id=p_b, role="member", access="write"))
        db.commit()
    finally:
        db.close()

    alex_key = client.post("/api/api-keys", json={"name": "k"}, headers=alex).json()["plaintext"]
    return {
        "alex": alex, "dana": dana, "alex_key": alex_key,
        "org_a": org_a, "org_b": org_b, "p_a": p_a, "p_b": p_b,
        "ids_a": ids_a, "ids_b": ids_b,
    }


def _blocked(status: int) -> bool:
    # Existence-hiding 404 for non-members; 403 where read-only access is honestly known.
    return status in (403, 404)


# ---- REST reads: org A cannot read org B by naming its project_id --------------
def test_cross_org_reads_blocked(client, tenants):
    a = tenants["alex"]
    pb = tenants["p_b"]
    reads = [
        ("GET", f"/api/items?project_id={pb}", None),
        ("GET", f"/api/memory/shards?project_id={pb}", None),
        ("GET", f"/api/memory/candidates?project_id={pb}", None),
        ("POST", "/api/memory/search", {"query": "secret", "project_id": pb}),
        ("GET", f"/api/memory/export?project_id={pb}", None),
        ("GET", f"/api/prds?project_id={pb}", None),
        ("GET", f"/api/dashboard?project_id={pb}", None),
        ("GET", f"/api/roadmap?project_id={pb}", None),
        ("GET", f"/api/links?project_id={pb}", None),
        ("GET", f"/api/requests?project_id={pb}", None),
        ("GET", f"/api/events?project_id={pb}", None),
        # Added 2026-08-21 (GRPH-436). Both were absent, and both are reads whose
        # `require_readable` call could be deleted with the whole suite still green.
        ("GET", f"/api/fleet/presence?project_id={pb}", None),
        ("GET", f"/api/projects/{pb}/counts", None),
    ]
    for method, path, body in reads:
        r = client.request(method, path, json=body, headers=a)
        assert _blocked(r.status_code), f"{method} {path} leaked: {r.status_code} {r.text[:200]}"


# ---- REST reads by resource id (deeper than project_id) ------------------------
def test_cross_org_resource_ids_blocked(client, tenants):
    a = tenants["alex"]
    ib = tenants["ids_b"]
    for path in (f"/api/items/{ib['item']}", f"/api/prds/{ib['prd']}", f"/api/prds/{ib['prd']}/coverage"):
        r = client.get(path, headers=a)
        assert _blocked(r.status_code), f"GET {path} leaked: {r.status_code}"


# ---- REST writes: org A cannot write into org B --------------------------------
def test_cross_org_writes_blocked(client, tenants):
    a = tenants["alex"]
    pb, ib = tenants["p_b"], tenants["ids_b"]
    writes = [
        ("POST", "/api/items", {"title": "x", "project_id": pb}),
        ("POST", "/api/memory/shards", {"text": "x", "project_id": pb}),
        ("POST", "/api/prds", {"title": "x", "project_id": pb}),
        ("POST", "/api/requests", {"type": "bug", "title": "x", "project_id": pb}),
        ("PATCH", f"/api/projects/{pb}", {"name": "hijacked"}),
        ("PATCH", f"/api/items/{ib['item']}", {"title": "hijacked"}),
        ("PATCH", f"/api/prds/{ib['prd']}", {"title": "hijacked"}),
    ]
    for method, path, body in writes:
        r = client.request(method, path, json=body, headers=a)
        assert _blocked(r.status_code), f"{method} {path} leaked: {r.status_code} {r.text[:200]}"
    # And nothing actually changed in org B (dana still sees the originals).
    d = tenants["dana"]
    proj = client.get(f"/api/items?project_id={pb}", headers=d).json()
    assert all(i["title"] != "hijacked" for i in proj)


# ---- Org endpoints: non-member can't see/administer another org ----------------
def test_cross_org_org_endpoints_blocked(client, tenants):
    a = tenants["alex"]
    ob = tenants["org_b"]
    assert _blocked(client.get(f"/api/orgs/{ob}/members", headers=a).status_code)
    assert _blocked(client.get(f"/api/orgs/{ob}/billing", headers=a).status_code)
    assert _blocked(client.post(f"/api/orgs/{ob}/invites", json={"email": "x@example.com"}, headers=a).status_code)
    # org B never appears in alex's own org list.
    assert ob not in [o["id"] for o in client.get("/api/orgs", headers=a).json()]


# ---- MCP: a key can't escape its owner's org, by any named project_id ----------
def _mcp(client, key, tool, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()


def test_cross_org_mcp_blocked(client, tenants):
    key, pb = tenants["alex_key"], tenants["p_b"]
    for tool, args in [
        ("get_backlog", {"project_id": pb}),
        ("search_items", {"query": "secret", "project_id": pb}),
        ("create_item", {"title": "x", "project_id": pb}),
        ("add_memory", {"text": "x", "project_id": pb}),
    ]:
        result = _mcp(client, key, tool, args)["result"]
        assert result.get("isError") is True, f"{tool} leaked: {result}"
        assert result["structuredContent"]["error"]["code"] == "unauthorized"


# ---- Public surface: raw project_id can't reach a tenant's project (AL-73) ------
def test_public_raw_project_id_cannot_write_tenant(client, tenants):
    pb, dana = tenants["p_b"], tenants["dana"]
    before = len(client.get(f"/api/requests?project_id={pb}", headers=dana).json())
    # Unauthenticated public submit naming the raw tenant project_id (no share token).
    client.post("/api/public/requests", json={"type": "bug", "title": "spam", "project_id": pb})
    after = len(client.get(f"/api/requests?project_id={pb}", headers=dana).json())
    assert after == before, "public submit leaked into a tenant project via raw project_id"


# ---- Positive controls: alex CAN reach her own org's project -------------------
def test_owner_can_reach_own_project(client, tenants):
    a, pa, ia = tenants["alex"], tenants["p_a"], tenants["ids_a"]
    assert client.get(f"/api/items?project_id={pa}", headers=a).status_code == 200
    assert client.get(f"/api/dashboard?project_id={pa}", headers=a).status_code == 200
    assert client.get(f"/api/prds/{ia['prd']}", headers=a).status_code == 200
    assert client.get(f"/api/orgs/{tenants['org_a']}/billing", headers=a).status_code == 200
    got = _mcp(client, tenants["alex_key"], "get_backlog", {"project_id": pa})["result"]
    assert got.get("isError") is not True


# ---- Global maintenance op is operator-only in hosted mode ---------------------
def test_backfill_operator_only_when_hosted(client, tenants):
    """A tenant can't trigger a cross-tenant re-embed; a platform operator can."""
    a = tenants["alex"]
    assert client.post("/api/memory/backfill", headers=a).status_code == 403


def test_backfill_open_on_self_host(client):
    auth = _login(client, "alex@ascme-labs.com")  # hosted_mode off (no `tenants` fixture)
    assert client.post("/api/memory/backfill", headers=auth).status_code == 200


# ---- Self-host regression: org layer inert, membership alone grants access ------
def test_self_host_membership_grants_access(client, monkeypatch):
    """HOSTED_MODE off: a plain per-project Membership is authority again, exactly
    as before the org layer existed."""
    from app.db import SessionLocal
    from app.models import Membership, Project

    dana = _login(client, "dana@ascme-labs.com")
    # dana makes a project (no org, self-host); grant alex a direct membership.
    pid = _make_project(client, dana, "Shared")
    db = SessionLocal()
    try:
        assert db.get(Project, pid).org_id is None  # self-host: no org attached
        db.add(Membership(user_id="u1", project_id=pid, role="member", access="write"))
        db.commit()
    finally:
        db.close()
    alex = _login(client, "alex@ascme-labs.com")
    assert client.get(f"/api/items?project_id={pid}", headers=alex).status_code == 200


# ---- E2E: the full hosted loop, signup→invite→org→project→MCP→plan --------------
def test_hosted_e2e_loop(client, monkeypatch):
    from app.config import settings
    from app.services.email import outbox

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "signup_mode", "invite_only")  # invite-only
    monkeypatch.setattr(settings, "platform_admin_emails", "alex@ascme-labs.com")
    outbox.clear()

    alex = _login(client, "alex@ascme-labs.com")
    org = _make_org(client, alex, "Launch Co")

    # invite → emailed token → new user registers past the closed gate → joins org
    client.post(f"/api/orgs/{org}/invites", json={"email": "bob@example.com", "role": "member"}, headers=alex)
    token = outbox[0].text.rsplit("/invite/", 1)[1].split()[0]
    assert client.post("/api/auth/register", json={
        "name": "Bob", "handle": "bob", "email": "bob@example.com",
        "password": "sup3rsecret", "invite_token": token,
    }).status_code == 201
    bob_login = client.post("/api/auth/login", json={"email": "bob@example.com", "password": "sup3rsecret"})
    bob = {"Authorization": f"Bearer {bob_login.json()['access_token']}"}
    assert org in [o["id"] for o in client.get("/api/orgs", headers=bob).json()]

    # project under the org → agent key → MCP loop
    pid = _make_project(client, alex, "Ship It")
    key = client.post("/api/api-keys", json={"name": "agent"}, headers=alex).json()["plaintext"]
    created = _mcp(client, key, "create_item", {"title": "first task", "project_id": pid})["result"]
    assert created.get("isError") is not True
    assert _mcp(client, key, "get_backlog", {"project_id": pid})["result"].get("isError") is not True

    # plan assignment (operator) → billing reflects it + usage
    assert client.put(f"/api/orgs/{org}/plan", json={"plan": "team"}, headers=alex).status_code == 200
    billing = client.get(f"/api/orgs/{org}/billing", headers=alex).json()
    assert billing["plan"] == "team"
    assert billing["usage"]["projects"] == 1
    assert billing["usage"]["seats"] == 2  # alex + bob


# ---- Sync surface: a code-graph credential can't push/pull across orgs (AL-220) --
# test_sync.py covers the single-tenant ingest; these extend the AL-76/AL-95 sweep to
# the /sync surface, the fleet-facing path a hosted Team endpoint (AL-219) will expose.
_SYNC_NODE = {"path": "svc.py", "kind": "file", "name": "svc",
              "summary": "a service", "content_hash": "h1"}


def test_sync_credential_ingests_only_into_its_own_project(client, tenants):
    """Positive control: alex's sync credential for her own project lands there."""
    a, pa = tenants["alex"], tenants["p_a"]
    key = client.post("/api/api-keys", json={"name": "spoke-a", "scopes": ["sync"],
                                             "project_id": pa}, headers=a).json()["plaintext"]
    r = client.post("/api/sync/code-graph", json={"nodes": [_SYNC_NODE]}, headers={"X-API-Key": key})
    assert r.status_code == 200 and r.json()["project_id"] == pa
    exported = client.get(f"/api/sync/export?project_id={pa}", headers=a).json()
    assert any(n["path"] == "svc.py" for n in exported["nodes"])


def test_sync_credential_cannot_ingest_into_another_org(client, tenants):
    """Alex holds a LEAKED write Membership on org B's project; the org gate must still
    keep her sync credential from pushing a graph into org B."""
    a, dana, pb = tenants["alex"], tenants["dana"], tenants["p_b"]
    before = client.get(f"/api/sync/export?project_id={pb}", headers=dana).json()["nodes"]

    created = client.post("/api/api-keys", json={"name": "spoke-b", "scopes": ["sync"],
                                                 "project_id": pb}, headers=a)
    if created.status_code == 201:
        # If the key can be minted at all, the ingest itself must refuse (empty sync target).
        key = created.json()["plaintext"]
        r = client.post("/api/sync/code-graph", json={"nodes": [_SYNC_NODE]},
                        headers={"X-API-Key": key})
        assert r.status_code == 403, f"sync ingest leaked into org B: {r.status_code} {r.text[:200]}"
    else:
        assert _blocked(created.status_code)  # minting a key for a foreign project refused outright

    after = client.get(f"/api/sync/export?project_id={pb}", headers=dana).json()["nodes"]
    assert len(after) == len(before), "a cross-org sync attempt added nodes to org B"


def test_cross_org_sync_rest_ops_blocked(client, tenants):
    """The JWT-authed sync triggers (push/purge/export/import) are project-gated too —
    naming another org's project_id can't push, purge, read, or import its graph."""
    a, dana, pb = tenants["alex"], tenants["dana"], tenants["p_b"]
    ops = [
        ("POST", "/api/sync/push", {"project_id": pb}),
        ("POST", "/api/sync/purge", {"project_id": pb}),
        ("GET", f"/api/sync/export?project_id={pb}", None),
        ("POST", "/api/sync/import", {"project_id": pb, "nodes": [_SYNC_NODE]}),
    ]
    for method, path, body in ops:
        r = client.request(method, path, json=body, headers=a)
        assert _blocked(r.status_code), f"{method} {path} leaked: {r.status_code} {r.text[:200]}"
    # And org B never received the import.
    assert client.get(f"/api/sync/export?project_id={pb}", headers=dana).json()["nodes"] == []


# ---- the agent code reads were never covered here (review, 2026-08-20) ----------


def test_cross_org_agent_code_reads_blocked(client, tenants):
    """`/api/agent/code/*` names a project in a query parameter, exactly like the reads
    above, and nothing in this file has ever tried it.

    That matters more than an ordinary gap because of what the fixture does: it hands the
    attacker a **direct write `Membership` on org B's project**. Per-project access is not
    authority once orgs are in play, so `_readable_pid`'s `require_readable` is the only
    thing standing between a named foreign project id and that project's data — and the
    guard was load-bearing and unasserted.

    Found by removing it and watching this file stay green.
    """
    for path in (
        f"/api/agent/code/health?project_id={tenants['p_b']}",
        f"/api/agent/code/map?project_id={tenants['p_b']}",
        f"/api/agent/code/analysis?project_id={tenants['p_b']}",
        f"/api/agent/code/neighbors?project_id={tenants['p_b']}&path=a.py",
    ):
        r = client.get(path, headers=tenants["alex"])
        assert _blocked(r.status_code), f"{path} returned {r.status_code}"


def test_cross_org_agent_code_health_blocked_for_an_api_key(client, tenants):
    """The same boundary on the credential GRPH-405 widened this read to accept.

    Widening *which* credentials may call it must not widen *what* any of them reach.
    """
    r = client.get(f"/api/agent/code/health?project_id={tenants['p_b']}",
                   headers={"X-API-Key": tenants["alex_key"]})
    assert _blocked(r.status_code), f"api key reached org B: {r.status_code}"
