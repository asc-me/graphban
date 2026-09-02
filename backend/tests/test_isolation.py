"""Phase 6 (AL-70): cross-tenant read isolation is enforced, not just displayed.

Every project-scoped READ endpoint must refuse a project the caller can't read —
and refuse it with a 404 so a non-member can't even probe whether the project
exists. Seeded memberships (seed.py): alex = write core/web/infra; ops = read
core, write infra, NONE on web; kate = read core/web. All seeded content
(items, requests, shards, PRDs, links) lives in `core`.

Runs on both SQLite and Postgres via the two-engine CI gate.
"""
import uuid

import pytest


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _register(client):
    """A brand-new tenant: a real account with zero project memberships."""
    handle = "stranger_" + uuid.uuid4().hex[:6]
    r = client.post(
        "/api/auth/register",
        json={"name": "Stranger", "email": f"{handle}@example.com",
              "handle": handle, "password": "graphban"},
    )
    assert r.status_code in (200, 201), r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# Project-scoped GET reads that take ?project_id and must be gated.
READ_GETS = [
    "/api/items",
    "/api/requests",
    "/api/prds",
    "/api/dashboard",
    "/api/roadmap",
    "/api/links",
    "/api/memory/shards",
    "/api/memory/candidates",
    "/api/memory/candidate-clusters",
    "/api/memory/export",
]

# Reads addressed by a core-owned resource id (not a project_id query).
PRD_BY_ID = [
    "/api/prds/PRD-1",
    "/api/prds/PRD-1/coverage",
    "/api/prds/PRD-1/evaluate",
    "/api/prds/PRD-1/versions",
]


@pytest.mark.parametrize("path", READ_GETS)
def test_non_member_cannot_read_foreign_project(client, path):
    """ops has no membership on `web` → every scoped read of web 404s."""
    ops = _login(client, "ops@ascme-labs.com")
    r = client.get(f"{path}?project_id=web", headers=ops)
    assert r.status_code == 404, f"{path} leaked web to a non-member: {r.status_code}"


@pytest.mark.parametrize("path", READ_GETS)
def test_member_can_read_own_project(client, path):
    """The gate must not over-block: ops reads `core` (read membership) fine."""
    ops = _login(client, "ops@ascme-labs.com")
    r = client.get(f"{path}?project_id=core", headers=ops)
    assert r.status_code == 200, f"{path} wrongly denied a reader: {r.status_code} {r.text}"


@pytest.mark.parametrize("path", READ_GETS)
def test_existence_hiding_forbidden_and_missing_are_indistinguishable(client, path):
    """A non-member must not tell a real-but-forbidden project (`web`) apart from a
    nonexistent one (`ghost`): both 404 with the same shape."""
    ops = _login(client, "ops@ascme-labs.com")
    forbidden = client.get(f"{path}?project_id=web", headers=ops)
    missing = client.get(f"{path}?project_id=ghost-does-not-exist", headers=ops)
    assert forbidden.status_code == missing.status_code == 404


def test_fresh_tenant_sees_nothing(client):
    """A newly-registered account with zero memberships can read no project's data
    — including core-owned resources addressed directly by id."""
    stranger = _register(client)
    for path in READ_GETS:
        r = client.get(f"{path}?project_id=core", headers=stranger)
        assert r.status_code == 404, f"{path} leaked core to a fresh tenant"
    for path in PRD_BY_ID:
        r = client.get(path, headers=stranger)
        assert r.status_code == 404, f"{path} leaked a core PRD to a fresh tenant"
    r = client.post(
        "/api/memory/search",
        json={"query": "anything", "top_k": 5, "project_id": "core"},
        headers=stranger,
    )
    assert r.status_code == 404


def test_memory_search_refuses_foreign_project(client):
    """POST /memory/search carries project_id in the body — same gate applies."""
    ops = _login(client, "ops@ascme-labs.com")
    r = client.post(
        "/api/memory/search",
        json={"query": "postgres", "top_k": 5, "project_id": "web"},
        headers=ops,
    )
    assert r.status_code == 404


def test_prd_by_id_refused_cross_tenant(client):
    """A core PRD read by id must 404 for a non-core member (fresh tenant), and
    resolve for a core reader (ops) — proving the id path is gated, not just list."""
    stranger = _register(client)
    ops = _login(client, "ops@ascme-labs.com")
    for path in PRD_BY_ID:
        assert client.get(path, headers=stranger).status_code == 404
        assert client.get(path, headers=ops).status_code == 200


# ---- AL-71: agent / code-graph reads are gated too ----

def test_agent_chat_cannot_search_foreign_project(client):
    """POST /agent/chat searches memory by project_id — a non-member naming `web`
    must be refused, not served web's shards."""
    ops = _login(client, "ops@ascme-labs.com")
    r = client.post("/api/agent/chat", json={"message": "postgres", "project_id": "web"}, headers=ops)
    assert r.status_code == 404


def test_code_graph_reads_refuse_foreign_project(client):
    """The code-graph reads resolve+gate a project — ops (no web) is refused web."""
    ops = _login(client, "ops@ascme-labs.com")
    assert client.get("/api/agent/code/map?project_id=web", headers=ops).status_code == 404
    assert client.get("/api/agent/code/neighbors?path=x&project_id=web", headers=ops).status_code == 404
    assert client.post(
        "/api/agent/code", json={"message": "what is here", "project_id": "web"}, headers=ops
    ).status_code == 404


def test_fresh_tenant_code_reads_see_nothing(client):
    """A zero-membership tenant can't reach any project's code graph, even by
    omitting project_id (the fallback is bounded to the caller's own projects)."""
    stranger = _register(client)
    assert client.get("/api/agent/code/map", headers=stranger).status_code == 404
    assert client.get("/api/agent/code/map?project_id=core", headers=stranger).status_code == 404


# ---- AL-71: global (project-less) memory honors share_global_memory ----

def test_global_memory_excluded_when_project_opts_out(client, auth):
    """A global (project-less) shard is visible to a project by default, but a
    project that sets share_global_memory=False must not see it (AL-71)."""
    # A published global shard: project_id explicitly null → NULL, written by alex.
    r = client.post(
        "/api/memory/shards",
        json={"text": "GLOBAL-NEEDLE cross-project lesson", "scope": "global", "project_id": None},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    assert r.json()["project_id"] is None, "shard should be global (project-less)"

    def core_sees_needle() -> bool:
        hits = client.post(
            "/api/memory/search",
            json={"query": "GLOBAL-NEEDLE cross-project lesson", "top_k": 10, "project_id": "core"},
            headers=auth,
        ).json()
        return any("GLOBAL-NEEDLE" in h["shard"]["text"] for h in hits)

    assert core_sees_needle(), "default (share_global_memory=True) should surface global shards"

    # Opt core out of global memory; the needle must disappear from its search.
    up = client.patch("/api/projects/core", json={"share_global_memory": False}, headers=auth)
    assert up.status_code == 200, up.text
    assert not core_sees_needle(), "opted-out project still saw a global shard"


def test_hosted_mode_forbids_global_shard_creation(client, auth, monkeypatch):
    """In hosted mode there is no cross-tenant "global" memory: creating a
    project-less shard is refused, so isolation can't be bypassed (AL-71)."""
    from app.config import settings
    from app.db import SessionLocal
    from app.models import OrgMembership, Organization, Project

    # Attach core to an org alex belongs to, so it stays writable under the hosted
    # org gate (AL-74) — this test is about the global-shard rule, not org access.
    db = SessionLocal()
    try:
        db.add_all([Organization(id="orgH", name="Hosted Org")])
        db.flush()
        db.add(OrgMembership(org_id="orgH", user_id="u1", role="owner"))
        db.get(Project, "core").org_id = "orgH"
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(settings, "hosted_mode", True)
    r = client.post(
        "/api/memory/shards",
        json={"text": "should be rejected", "scope": "global", "project_id": None},
        headers=auth,
    )
    assert r.status_code == 400, r.text
    # A project-scoped shard is still fine.
    ok = client.post(
        "/api/memory/shards",
        json={"text": "scoped is fine", "scope": "item", "project_id": "core"},
        headers=auth,
    )
    assert ok.status_code == 201, ok.text
