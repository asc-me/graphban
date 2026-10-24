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
from sqlalchemy import select

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
        ("GET", f"/api/live?project_id={pb}", None),
        ("GET", f"/api/projects/{pb}/counts", None),
    ]
    for method, path, body in reads:
        r = client.request(method, path, json=body, headers=a)
        assert _blocked(r.status_code), f"{method} {path} leaked: {r.status_code} {r.text[:200]}"


# ---- every project-scoped read, enumerated from the app ------------------------
#
# The list above is hand-written, and that is the whole problem with it: it named 11 of the
# 36 GET routes this app serves that take a `project_id`. `/api/fleet/presence` and
# `/api/projects/{id}/counts` were both absent, so `require_readable` could be deleted from
# either one with this entire file green (GRPH-436, measured 2026-08-21). A route that joins
# the app but not the list is protected by nobody noticing.
#
# So the routes come from the app itself. The hand-written list stays: it pins exact query
# shapes and covers `POST /api/memory/search`, which is a read wearing a POST and is not
# reachable by enumerating GETs. The two are not redundant — one asserts specific requests
# keep working, the other asserts nothing was left out.

# WRITES ARE NOT ENUMERATED HERE, and the reason is a measurement rather than a shrug.
# 14 write routes take a `project_id` as a query or path parameter; 23 more carry it in the
# REQUEST BODY (`POST /api/items`, `POST /api/api-keys`, and most of the agent surface).
# Enumerating parameters alone would therefore cover 14 of 37 while reading exactly like a
# complete sweep — the same absence-reads-as-clean shape this block exists to close, rebuilt
# one level up. Probing the other 23 needs a valid body per schema, which is real work and is
# not being smuggled in under a test file. `test_cross_org_writes_blocked` remains a hand
# list, and remains incomplete, and now says so.

# Routes whose `project_id` is not the only thing they need. A 422 is neither blocked nor
# leaked, so it must never count as a pass: an unprobeable route is not a safe route.
PROBE_PARAMS = {
    "/api/agent/code/for": {"ref_id": "x"},
    "/api/agent/code/neighbors": {"path": "a/b.py"},
    "/api/public/duplicates": {"q": "hello"},
}

# Deliberately EMPTY, and that is a finding rather than an oversight: every one of the 36
# blocks a cross-org caller today, `/api/public/*` included — those are scoped by what a
# project has published, not by being unauthenticated. If a route ever needs to be here it
# needs a reason here too, so the exemption is a reviewable act and not a hole.
CROSS_TENANT_EXEMPT: dict[str, str] = {}


def _project_scoped_gets(client) -> dict[str, list[str]]:
    """Every GET the app serves that takes a `project_id`, from its OpenAPI schema.

    Asked of the SCHEMA, not of `app.routes`. On this FastAPI version the routers mount as
    `_IncludedRouter` objects, so walking `app.routes` finds four routes and reports a clean
    sweep of nothing — which is how the first version of this passed while testing zero
    endpoints.
    """
    found = {}
    for path, ops in client.app.openapi()["paths"].items():
        op = ops.get("get")
        if not op:
            continue
        params = {p["name"]: p for p in op.get("parameters", [])}
        if "project_id" not in params:
            continue
        found[path] = sorted(n for n, p in params.items()
                             if p.get("required") and n != "project_id")
    return found


def test_the_enumeration_finds_the_routes_it_claims_to(client):
    """The control. Every assertion below is a loop over this set, so an enumerator that
    silently returns nothing would turn the whole check into a pass. Pinned by count and by
    naming the two routes whose absence from the hand-written list started this."""
    found = _project_scoped_gets(client)

    assert len(found) >= 30, f"only {len(found)} project-scoped GETs found — enumeration broke"
    assert "/api/fleet/presence" in found
    assert "/api/projects/{project_id}/counts" in found


def test_every_project_scoped_read_blocks_another_org(client, tenants):
    """The point of the file, asked of every route rather than of a remembered list."""
    alex, pb = tenants["alex"], tenants["p_b"]
    leaked, unprobeable = [], []

    for path, needs in sorted(_project_scoped_gets(client).items()):
        if path in CROSS_TENANT_EXEMPT:
            continue
        query = {"project_id": pb} if "{project_id}" not in path else {}
        query.update(PROBE_PARAMS.get(path, {}))
        url = path.replace("{project_id}", pb)
        if query:
            url += "?" + "&".join(f"{k}={v}" for k, v in query.items())

        r = client.get(url, headers=alex)
        if r.status_code == 422:
            unprobeable.append(f"{path} (needs {needs})")
        elif not _blocked(r.status_code):
            leaked.append(f"{path} -> {r.status_code} {r.text[:120]}")

    assert not leaked, f"cross-org reads that did not block: {leaked}"
    # A 422 is not a pass. The route needs an entry in PROBE_PARAMS so it can actually be
    # asked the question — otherwise it sits here looking covered while never being called.
    assert not unprobeable, (
        f"these could not be probed, so they are untested: {unprobeable}. "
        "Add the required params to PROBE_PARAMS.")


def test_an_exemption_must_carry_a_reason(client):
    """An exemption map that fills up with bare entries is the hand-written list again."""
    for path, reason in CROSS_TENANT_EXEMPT.items():
        assert path in _project_scoped_gets(client), f"{path} is exempt from nothing"
        assert len(reason) > 20, f"{path} is exempt without a reason"


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
    assert client.get(f"/api/orgs/{tenants['org_a']}/members", headers=a).status_code == 200
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

    # org roster still reflects membership after the full loop
    members = client.get(f"/api/orgs/{org}/members", headers=alex).json()
    assert len(members) == 2  # alex + bob


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


# ---- an item cannot reference another project's PRD (GRPH-457) -------------------


def _prd(client, auth, project_id, title="Spec"):
    return client.post("/api/prds", json={"title": title, "body": "## D1\n\nwork",
                                          "project_id": project_id}, headers=auth).json()


def test_an_item_cannot_reference_a_prd_in_another_project(client, tenants):
    """`resolve_prd` is global, so a cross-project key used to resolve cleanly and freeze
    into the row. Worse than a dangling reference, because it RESOLVES — the item reads as
    correctly linked on every surface while `prd_coverage`, which is project-scoped, never
    counts it under the PRD it names.
    """
    theirs = _prd(client, tenants["dana"], tenants["p_b"], "Org B spec")
    assert theirs.get("id"), theirs

    r = client.post("/api/items", headers=tenants["alex"],
                    json={"title": "borrowed", "project_id": tenants["p_a"],
                          "prd_id": theirs["id"]})
    assert r.status_code >= 400, f"cross-project PRD reference was accepted: {r.text}"


def test_a_same_project_reference_still_resolves_and_freezes(client, tenants):
    """The half that stops the refusal passing for the wrong reason.

    Making resolution project-scoped in a way that renders the key UNRESOLVABLE would
    also satisfy the test above — the value would fall through to the store-as-given
    branch and the refusal would look like it worked. So this half has to prove
    resolution still *does something*, which means the key it passes cannot equal the
    stored id. At issue time those are the same string (``keys.mint`` renders the id),
    so identity alone would prove nothing. Retagging is what separates them: after the
    rename the PRD is addressed as ``NEW-P<n>`` while its frozen id stays ``OLD-P<n>``.

    That is GRPH-319's write-resolution behaviour, and it must survive this change.
    """
    from app.db import SessionLocal
    from app.models import Item, Prd
    from app.services import projects as projects_svc

    mine = _prd(client, tenants["alex"], tenants["p_a"], "Org A spec")
    frozen = mine["id"]

    db = SessionLocal()
    try:
        projects_svc.retag_project(db, tenants["p_a"], "ZQX")
        number = db.scalar(select(Prd.number).where(Prd.id == frozen))
    finally:
        db.close()

    renamed_key = f"ZQX-P{number}"
    assert renamed_key != frozen, "retag did not separate the key from the stored id"

    r = client.post("/api/items", headers=tenants["alex"],
                    json={"title": "linked", "project_id": tenants["p_a"],
                          "prd_id": renamed_key})
    assert r.status_code == 201, r.text

    db = SessionLocal()
    try:
        row = db.scalar(select(Item).where(Item.title == "linked"))
        assert row is not None
        assert row.prd_id == frozen, (
            f"stored {row.prd_id!r}; the live key {renamed_key!r} must freeze to the "
            f"issue-time id {frozen!r}, not be written through as given"
        )
    finally:
        db.close()


def test_an_unresolvable_reference_is_still_stored_as_given(client, tenants):
    """GRPH-319's degradation, unchanged: a dangling reference is recoverable, a silently
    discarded link is not. The new refusal must not have swallowed this branch."""
    r = client.post("/api/items", headers=tenants["alex"],
                    json={"title": "dangling", "project_id": tenants["p_a"],
                          "prd_id": "NOPE-P999"})
    assert r.status_code == 201, r.text


def test_an_item_cannot_be_updated_to_reference_another_project_s_prd(client, tenants):
    """The second door. `create_item` and `update_item` both freeze a prd_id, so a gate on
    only the create path leaves the same bad row reachable in two moves instead of one."""
    theirs = _prd(client, tenants["dana"], tenants["p_b"], "Org B spec")
    mine = client.post("/api/items", headers=tenants["alex"],
                       json={"title": "clean", "project_id": tenants["p_a"]}).json()

    r = client.patch(f"/api/items/{mine['id']}", headers=tenants["alex"],
                     json={"prd_id": theirs["id"]})
    assert r.status_code >= 400, f"cross-project PRD reference accepted on update: {r.text}"


# ---- Item links across projects (GRPH-1063) --------------------------------------
# A link may join two projects the caller can read, inside ONE organization: self-host with no
# orgs is one org of its own, hosted is the org, a linked box proxies to the cloud's org. A
# blocker on the far side blocks whoever is looking, and is shown — never dropped — to a reader
# who cannot see its project.

HIDDEN = "(an item in a project you cannot see)"


def _item(client, headers, pid, title, status="next"):
    r = client.post("/api/items", json={"title": title, "project_id": pid, "status": status},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _key(client, headers, **body):
    r = client.post("/api/api-keys", json={"name": "x", **body}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["plaintext"]


def _ok(res):
    assert "result" in res and res["result"].get("isError") is not True, res
    return res["result"]["structuredContent"]


def _refusal(res):
    result = res["result"]
    assert result.get("isError") is True, f"a link was accepted: {result}"
    err = result["structuredContent"]["error"]
    assert err["code"] == "unauthorized", err
    return err["message"]


def _link_rows():
    from app.db import SessionLocal
    from app.models import Link

    db = SessionLocal()
    try:
        return [(lk.a, lk.b) for lk in db.scalars(select(Link)).all()]
    finally:
        db.close()


def _grant(user_id, project_id, access):
    from app.db import SessionLocal
    from app.models import Membership

    db = SessionLocal()
    try:
        db.add(Membership(user_id=user_id, project_id=project_id, role="member", access=access))
        db.commit()
    finally:
        db.close()


def test_hosted_two_projects_in_one_org_link_and_block(client, tenants):
    alex, pa = tenants["alex"], tenants["p_a"]
    pa2 = _make_project(client, alex, "Project A2")
    dependent = _item(client, alex, pa, "waits on A2")
    blocker = _item(client, alex, pa2, "A2 change")

    _ok(_mcp(client, tenants["alex_key"], "link_items",
             {"a": dependent, "b": blocker, "type": "dependency"}))

    rows = _ok(_mcp(client, tenants["alex_key"], "get_backlog", {"project_id": pa}))["results"]
    row = next(r for r in rows if r["id"] == dependent)
    assert row["ready"] is False and row["blocked_by"] == [blocker]


def test_hosted_cross_org_link_refused_for_a_member_of_both_orgs(client, tenants):
    """The hard case: alex is seated in BOTH orgs and can read org B's project, so nothing but
    the org rule stands between her and the link."""
    from app.db import SessionLocal
    from app.models import OrgMembership

    db = SessionLocal()
    try:
        db.add(OrgMembership(org_id=tenants["org_b"], user_id="u1", role="member"))
        db.commit()
    finally:
        db.close()
    key = _key(client, tenants["alex"])
    mine, theirs = tenants["ids_a"]["item"], tenants["ids_b"]["item"]
    # Positive control: she really can read org B, so the refusal below is the org rule.
    assert _ok(_mcp(client, key, "get_item_details", {"id": theirs}))["id"] == theirs
    before = _link_rows()

    for a, b in [(mine, theirs), (theirs, mine)]:
        msg = _refusal(_mcp(client, key, "link_items", {"a": a, "b": b}))
        assert "another organization" in msg, msg
        _refusal(_mcp(client, key, "unlink_items", {"a": a, "b": b}))
    assert _link_rows() == before, "a refused cross-org link left a row behind"


def test_hosted_cross_org_refusal_is_the_same_whether_the_far_item_exists(client, tenants):
    key, mine, theirs = tenants["alex_key"], tenants["ids_a"]["item"], tenants["ids_b"]["item"]
    ghost = theirs.rsplit("-", 1)[0] + "-99999"

    real = _refusal(_mcp(client, key, "link_items", {"a": mine, "b": theirs}))
    absent = _refusal(_mcp(client, key, "link_items", {"a": mine, "b": ghost}))
    assert real.replace(theirs, "<b>") == absent.replace(ghost, "<b>"), (real, absent)
    assert _refusal(_mcp(client, key, "unlink_items", {"a": mine, "b": theirs})).replace(
        theirs, "<b>") == real.replace(theirs, "<b>")


@pytest.fixture()
def selfhost_pair(client):
    """Self-host, no org rows at all: P1 holds a dependent, P2 its blocker. kate may write P1
    and has no access to P2; alex may write both."""
    from app.db import SessionLocal
    from app.models import Organization

    alex = _login(client, "alex@ascme-labs.com")
    p1 = _make_project(client, alex, "Box side")
    p2 = _make_project(client, alex, "Control plane")
    dependent = _item(client, alex, p1, "dependent")
    blocker = _item(client, alex, p2, "far blocker", status="in_progress")
    _grant(_user_id("kate@ascme-labs.com"), p1, "write")

    db = SessionLocal()
    try:
        assert db.scalar(select(Organization.id)) is None, "this case must run with no org rows"
    finally:
        db.close()
    alex_key = _key(client, alex)
    _ok(_mcp(client, alex_key, "link_items", {"a": dependent, "b": blocker}))
    return {"alex": alex, "alex_key": alex_key, "p1": p1, "p2": p2,
            "dependent": dependent, "blocker": blocker,
            "kate_key": _key(client, _login(client, "kate@ascme-labs.com"), project_id=p1)}


def _user_id(email):
    from app.db import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        return db.scalar(select(User.id).where(User.email == email))
    finally:
        db.close()


def test_selfhost_links_two_projects_with_no_orgs(client, selfhost_pair):
    s = selfhost_pair
    details = _ok(_mcp(client, s["alex_key"], "get_item_details", {"id": s["dependent"]}))
    assert {"type": "dependency", "a": s["dependent"], "b": s["blocker"], "reason": ""} \
        in details["links"]
    # The edge shows on BOTH projects' link reads, not only the one it is stored in.
    for pid in (s["p1"], s["p2"]):
        edges = client.get(f"/api/links?project_id={pid}", headers=s["alex"]).json()
        assert any(e["a"] == s["dependent"] and e["b"] == s["blocker"]
                   and e["b_status"] == "in_progress" for e in edges), (pid, edges)


def test_a_far_blocker_the_reader_cannot_see_is_a_placeholder_not_a_gap(client, selfhost_pair):
    s = selfhost_pair
    kate = s["kate_key"]
    details = _ok(_mcp(client, kate, "get_item_details", {"id": s["dependent"]}))
    assert details["links"] == [{"type": "dependency", "a": s["dependent"], "b": HIDDEN,
                                 "reason": ""}]
    assert "far blocker" not in str(details) and s["blocker"] not in str(details)

    row = next(r for r in _ok(_mcp(client, kate, "get_backlog", {}))["results"]
               if r["id"] == s["dependent"])
    assert row["ready"] is False and row["blocked_by"] == [HIDDEN]

    edges = client.get(f"/api/links?project_id={s['p1']}",
                       headers=_login(client, "kate@ascme-labs.com")).json()
    assert [(e["b"], e["b_status"]) for e in edges] == [(HIDDEN, None)]


def test_a_hidden_far_blocker_still_blocks_every_claim_path(client, selfhost_pair):
    s = selfhost_pair
    kate = s["kate_key"]
    assert _ok(_mcp(client, kate, "claim_next", {}))["claimed"] is False
    assert _ok(_mcp(client, kate, "next_cluster", {}))["claimed"] == 0
    assert _ok(_mcp(client, kate, "claim_cluster", {}))["claimed"] is False

    # Control: finishing the far blocker is what frees it — so the refusal above was the link.
    r = client.patch(f"/api/items/{s['blocker']}", json={"status": "done"}, headers=s["alex"])
    assert r.status_code < 400 or r.status_code == 409, r.text
    if r.status_code >= 400:  # owner-close gate: finish it the way an attestation would
        from app.db import SessionLocal
        from app.models import Item
        from app.services import keys

        db = SessionLocal()
        try:
            db.get(Item, keys.resolve_item(db, s["blocker"])).status = "done"
            db.commit()
        finally:
            db.close()
    claimed = _ok(_mcp(client, kate, "claim_next", {}))
    assert claimed["claimed"] is True and claimed["item"]["id"] == s["dependent"]


def test_unlink_applies_the_same_scope_rule(client, selfhost_pair):
    s = selfhost_pair
    # kate cannot read the blocker's project: the unlink is refused and the edge survives.
    _refusal(_mcp(client, s["kate_key"], "unlink_items",
                  {"a": s["dependent"], "b": s["blocker"]}))
    assert _ok(_mcp(client, s["alex_key"], "unlink_items",
                    {"a": s["dependent"], "b": s["blocker"]}))["removed"] == 1


def test_linking_needs_read_on_the_far_side_and_write_on_the_source(client, selfhost_pair):
    s = selfhost_pair
    other = _item(client, s["alex"], s["p1"], "second dependent")
    before = _link_rows()
    _refusal(_mcp(client, s["kate_key"], "link_items", {"a": other, "b": s["blocker"]}))
    assert _link_rows() == before


def test_a_linked_box_forwards_link_writes_and_never_writes_one_locally(
        client, monkeypatch, selfhost_pair):
    from app.services import assistant_tools as at
    from app.services import mcp_proxy

    assert "link_items" not in mcp_proxy.LOCAL_TOOLS
    assert "unlink_items" not in mcp_proxy.LOCAL_TOOLS

    sent = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": "{}"}], "structuredContent": {"from": "cloud"}}}

    monkeypatch.setattr(mcp_proxy.httpx, "post",
                        lambda url, json=None, **_: sent.append(json["params"]["name"]) or _Resp())
    monkeypatch.setattr(mcp_proxy.settings, "sync_cloud_url", "https://cloud")
    monkeypatch.setattr(mcp_proxy.settings, "sync_api_key", "gb_sk_link")

    s = selfhost_pair
    other = _item(client, s["alex"], s["p1"], "linked-box dependent")
    before = _link_rows()
    for tool in ("link_items", "unlink_items"):
        _mcp(client, s["alex_key"], tool, {"a": other, "b": s["blocker"]})
    assert sent == ["link_items", "unlink_items"]

    # The one local REST path that writes a link — the assistant — refuses rather than writing.
    from app.db import SessionLocal
    from app.providers.toolcall import ToolCall

    db = SessionLocal()
    try:
        ctx = at.ToolContext(db=db, user_id=_user_id("alex@ascme-labs.com"), project_id=s["p1"],
                             entity_type="item", entity_id=other)
        res = at.dispatch(ctx, ToolCall(id="c1", name="link_items",
                                        input={"target_id": s["blocker"]}))
    finally:
        db.close()
    assert "linked cloud" in res.content, res
    assert _link_rows() == before, "a linked box wrote a link the cloud never arbitrated"


def test_an_item_still_cannot_reference_a_prd_in_another_project_on_one_deployment(
        client, selfhost_pair):
    """GRPH-457 is unchanged by cross-project LINKS: same deployment, no orgs, readable both
    sides — a PRD reference still stays inside its project."""
    s = selfhost_pair
    prd = _prd(client, s["alex"], s["p2"], "Control-plane spec")
    assert prd.get("id"), prd
    r = client.post("/api/items", headers=s["alex"],
                    json={"title": "borrowed", "project_id": s["p1"], "prd_id": prd["id"]})
    assert r.status_code >= 400, f"cross-project PRD reference was accepted: {r.text}"


@pytest.fixture()
def far_wait(client):
    """Self-host, no orgs: a blocked dependent in P1 waits on a typed human wait filed in P2."""
    from app.db import SessionLocal
    from app.services import items as items_svc

    alex = _login(client, "alex@ascme-labs.com")
    p1 = _make_project(client, alex, "Box side")
    p2 = _make_project(client, alex, "Control plane")
    dependent = _item(client, alex, p1, "needs the far merge", status="blocked")
    wait = _item(client, alex, p2, "merge the far change", status="blocked")
    db = SessionLocal()
    try:
        items_svc.update_item(db, wait, tags=["wait:merge"])
    finally:
        db.close()
    _ok(_mcp(client, _key(client, alex), "link_items", {"a": dependent, "b": wait}))
    return {"dependent": dependent, "wait": wait}


def _status(item_id):
    from app.db import SessionLocal
    from app.services import items as items_svc

    db = SessionLocal()
    try:
        return items_svc.get_item(db, item_id).status
    finally:
        db.close()


def test_a_wait_in_another_project_still_gates_review(client, far_wait):
    """SABOTAGE (grok, PR #979): `unfinished_wait_deps` looked waits up in its own project only,
    so a wait filed in another project was invisible and the original moved to review."""
    from app.db import SessionLocal
    from app.services import items as items_svc

    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="typed human wait"):
            items_svc.update_item(db, far_wait["dependent"], status="review")
    finally:
        db.close()
    assert _status(far_wait["dependent"]) == "blocked"


def test_done_on_a_far_wait_releases_its_dependent(client, far_wait):
    """SABOTAGE (grok, PR #979): `release_waiters` walked the wait's own project context, which
    never holds a dependent in another project, so that dependent stayed blocked forever."""
    from app.db import SessionLocal
    from app.services import items as items_svc

    db = SessionLocal()
    try:
        items_svc.update_item(db, far_wait["wait"], status="done")
    finally:
        db.close()
    assert _status(far_wait["dependent"]) == "next", "the far wait cleared; its dependent did not"


def test_an_idempotent_link_replay_does_not_reveal_a_far_key(client, selfhost_pair):
    """Idempotency keys are global. A cached `link_items` reply is served before the scope rule
    runs, so it must be rendered under the REPLAYING caller's read scope (grok, PR #979)."""
    s = selfhost_pair
    other = _item(client, s["alex"], s["p1"], "second dependent")
    args = {"a": other, "b": s["blocker"], "idempotency_key": "replay-979"}
    first = _ok(_mcp(client, s["alex_key"], "link_items", args))
    assert first["b"] == s["blocker"]

    replay = _ok(_mcp(client, s["kate_key"], "link_items", args))
    assert replay["id"] == first["id"]
    assert replay["b"] == HIDDEN and s["blocker"] not in str(replay), replay
