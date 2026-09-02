"""Gitops delivery contract — four-state at the CALLER, not only inside resolve()."""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.schemas import ProjectOut
from app.services import gitops as gitops_svc

FIELDS = gitops_svc.FIELDS
UNMEASURED = {"value": None, "source": "unmeasured"}
# Literal, not gitops.LINKED_PATCH_DETAIL — coupling the test to the callee constant
# leaves a changed message green (GRPH-617 bounce).
LINKED_403_DETAIL = "gitops on a linked instance is owned by the org admin"
TOKENS = list(gitops_svc.NAMING_TOKENS)


def _mcp_key(client, auth, project_id="core"):
    return client.post(
        "/api/api-keys",
        json={"name": "gitops", "scopes": ["read", "write"], "project_id": project_id},
        headers=auth,
    ).json()["plaintext"]


def _sync_key(client, auth, project_id="core"):
    return client.post(
        "/api/api-keys",
        json={"name": "sync-gitops", "scopes": ["read", "write", "sync"], "project_id": project_id},
        headers=auth,
    ).json()["plaintext"]


def _rpc(client, key, tool, arguments=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments or {}}},
        headers={"X-API-Key": key},
    ).json()


def _ctx(client, key, project_id=None):
    args = {} if project_id is None else {"project_id": project_id}
    body = _rpc(client, key, "get_context", args)
    assert "result" in body, body
    result = body["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def _gitops(ctx):
    assert "gitops" in ctx
    g = ctx["gitops"]
    for f in FIELDS:
        assert f in g, f"omitted field {f} looks like no requirements"
    return g


def _field_values(g):
    return {f: g[f]["value"] for f in FIELDS}


def _assert_unmeasured_fields(g):
    for f in FIELDS:
        assert g[f] == UNMEASURED, f"{f} was {g[f]}"


def _get(client, auth, project_id="core"):
    return client.get(f"/api/projects/{project_id}/gitops", headers=auth)


def _patch(client, auth, body, project_id="core"):
    return client.patch(f"/api/projects/{project_id}/gitops", json=body, headers=auth)


def _cloud_body(*, base=None, source="unmeasured", writable=True, state="local"):
    fields = {f: dict(UNMEASURED) for f in FIELDS}
    if base is not None:
        fields["base_branch"] = {"value": base, "source": source}
    return {
        "project_id": "cloud-proj",
        "org_id": "cloud-org",
        "fields": fields,
        "control": {"state": state, "writable": writable, "message": ""},
        "was": None,
        "version_from": dict(UNMEASURED),
    }


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _mock_cloud(monkeypatch, payload=None, *, status=200, error=None, hits=None):
    def fake_get(url, *a, **k):
        if hits is not None:
            hits.append(url)
        if error is not None:
            raise error
        return _Resp(status, payload)

    monkeypatch.setattr(gitops_svc.httpx, "get", fake_get)


def _link_env(monkeypatch, url="https://cloud.example", key="gb_sk_link"):
    from app.config import settings
    monkeypatch.setattr(settings, "sync_cloud_url", url)
    monkeypatch.setattr(settings, "sync_api_key", key)


def _link_web(client, auth, url="https://cloud.example", key="gb_sk_link"):
    r = client.post("/api/sync/link", json={"cloud_url": url, "api_key": key}, headers=auth)
    assert r.status_code == 200, r.text


def _set_local(db, project_id="core", **cols):
    from app.models import Project
    p = db.get(Project, project_id)
    for k, v in cols.items():
        setattr(p, gitops_svc.COL[k], v)
    db.commit()


# ---- schema / ProjectOut -----------------------------------------------------------------


def test_project_out_has_no_gitops_fields():
    leaked = [k for k in ProjectOut.model_fields if k.startswith("gitops_")]
    assert leaked == []


def test_get_projects_does_not_serve_gitops_columns(client, auth):
    rows = client.get("/api/projects", headers=auth).json()
    core = next(p for p in rows if p["id"] == "core")
    assert "gitops_base_branch" not in core
    assert not any(k.startswith("gitops_") for k in core)


# ---- state 1: unlinked, nothing set ------------------------------------------------------


def test_state1_get_context_is_unmeasured_not_main(client, auth):
    ctx = _ctx(client, _mcp_key(client, auth))
    g = _gitops(ctx)
    _assert_unmeasured_fields(g)
    assert "control" not in g
    assert "main" not in _field_values(g).values()
    assert g["tokens"] == TOKENS
    assert g["version_from"] == UNMEASURED
    assert "model" not in g, "the preset name is a Settings label, not an agent input"
    assert "plan" not in g, "the checklist is Settings, not CORE"
    assert g["note"] == gitops_svc.NOTE_UNMEASURED


def test_state1_rest_get_is_unmeasured_editable(client, auth):
    r = _get(client, auth)
    assert r.status_code == 200
    body = r.json()
    for f in FIELDS:
        assert body["fields"][f] == UNMEASURED
    assert body["control"]["state"] == "local"
    assert body["control"]["writable"] is True
    assert body["control"]["message"] == ""
    assert body["was"] is None
    assert body["projects"] == []
    assert body["plan"] is None
    assert "main" not in json.dumps(body["fields"])


# ---- state 2: unlinked, local test -------------------------------------------------------


def test_state2_local_test_at_get_context_and_get(client, auth):
    r = _patch(client, auth, {"base_branch": "test"})
    assert r.status_code == 200
    assert r.json()["fields"]["base_branch"] == {"value": "test", "source": "project"}

    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    assert g["base_branch"] == {"value": "test", "source": "project"}
    assert g["no_push_to_base"] == UNMEASURED
    assert "control" not in g
    assert "main" not in _field_values(g).values()

    body = _get(client, auth).json()
    assert body["fields"]["base_branch"]["value"] == "test"
    assert body["control"]["state"] == "local"
    assert body["control"]["writable"] is True
    assert "control" not in g


# ---- two local projects ------------------------------------------------------------------


def test_two_local_projects_do_not_leak(client, auth):
    other = client.post("/api/projects", json={"name": "Otherbox"}, headers=auth).json()
    assert _patch(client, auth, {"base_branch": "test"}, project_id="core").status_code == 200

    g_b = _gitops(_ctx(client, _mcp_key(client, auth, project_id=other["id"]), other["id"]))
    assert g_b["base_branch"] == UNMEASURED
    assert "test" not in _field_values(g_b).values()
    assert "main" not in _field_values(g_b).values()

    body = _get(client, auth, other["id"]).json()
    assert body["fields"]["base_branch"]["value"] is None
    assert body["project_id"] == other["id"]


# ---- overlay omit vs null; empty does not copy-down --------------------------------------


def test_empty_patch_does_not_copy_down_or_wipe(client, auth):
    from app.db import SessionLocal
    from app.models import Organization, Project

    db = SessionLocal()
    try:
        db.add(Organization(id="org_house", name="House"))
        db.flush()
        p = db.get(Project, "core")
        p.org_id = "org_house"
        p.gitops_base_branch = None
        db.get(Organization, "org_house").gitops_base_branch = "stage"
        db.commit()
    finally:
        db.close()

    r = _patch(client, auth, {})
    assert r.status_code == 200
    body = r.json()
    assert body["fields"]["base_branch"] == {"value": "stage", "source": "org"}

    db = SessionLocal()
    try:
        assert db.get(Project, "core").gitops_base_branch is None
    finally:
        db.close()


def test_null_clears_omit_does_not(client, auth):
    assert _patch(client, auth, {"base_branch": "stage"}).status_code == 200
    assert _patch(client, auth, {"no_push_to_base": True}).status_code == 200
    left = _patch(client, auth, {"base_branch": None}).json()
    assert left["fields"]["base_branch"] == UNMEASURED
    assert left["fields"]["no_push_to_base"]["value"] is True

    still = _patch(client, auth, {}).json()
    assert still["fields"]["no_push_to_base"]["value"] is True


def test_blank_string_stores_null(client, auth):
    r = _patch(client, auth, {"base_branch": "  "})
    assert r.status_code == 200
    assert r.json()["fields"]["base_branch"] == UNMEASURED


def test_no_push_to_base_null_is_unmeasured(client, auth):
    assert _patch(client, auth, {"no_push_to_base": True}).status_code == 200
    r = _patch(client, auth, {"no_push_to_base": None})
    assert r.status_code == 200
    assert r.json()["fields"]["no_push_to_base"] == UNMEASURED


# ---- validation (call the route, not only the helper) ------------------------------------


def test_glob_on_base_branch_is_422_naming_tokens(client, auth):
    r = _patch(client, auth, {"base_branch": "release-*"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    text = detail if isinstance(detail, str) else json.dumps(detail)
    for tok in TOKENS:
        assert tok in text


@pytest.mark.parametrize("pattern", ["feat/*", "feat/{foo}", "x?"])
def test_bad_pattern_is_422(client, auth, pattern):
    r = _patch(client, auth, {"branch_name_pattern": pattern})
    assert r.status_code == 422
    text = json.dumps(r.json())
    assert "item_id" in text


def test_token_on_base_branch_is_422(client, auth):
    r = _patch(client, auth, {"base_branch": "{version}"})
    assert r.status_code == 422
    assert "item_id" in json.dumps(r.json())


def test_closed_tokens_on_pattern_ok(client, auth):
    r = _patch(client, auth, {"branch_name_pattern": "feat/{item_id}-{slug}"})
    assert r.status_code == 200
    assert r.json()["fields"]["branch_name_pattern"]["value"] == "feat/{item_id}-{slug}"


def test_unknown_reviewer_bar_and_version_scheme_422(client, auth):
    r = _patch(client, auth, {"reviewer_bar": "merge_queue"})
    assert r.status_code == 422 and "sign_off" in json.dumps(r.json())
    r = _patch(client, auth, {"version_from": "invented"})
    assert r.status_code == 422
    for s in gitops_svc.VERSION_SCHEMES:
        assert s in json.dumps(r.json())


# ---- GitHub is not a default branch ------------------------------------------------------


def test_github_connected_does_not_infer_main(client, auth):
    from app.db import SessionLocal
    from app.services import platform as platform_svc

    db = SessionLocal()
    try:
        platform_svc.connect_github(db, "core", account="acme", repo="app")
    finally:
        db.close()

    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    _assert_unmeasured_fields(g)
    assert g["base_branch"]["value"] != "main"
    assert "main" not in _field_values(g).values()
    assert _get(client, auth).json()["fields"]["base_branch"] == UNMEASURED


# ---- version_from git_tag does not invent 1.0.0 ------------------------------------------


def test_version_from_git_tag_does_not_invent_a_number(client, auth):
    r = _patch(client, auth, {"version_from": "git_tag"})
    assert r.status_code == 200
    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    assert g["version_from"] == {"value": "git_tag", "source": "project"}
    dumped = json.dumps(g)
    assert "1.0.0" not in dumped
    assert "1.0" not in dumped or g["version_from"]["value"] != "1.0"


# ---- linked 403 (web and env); exact JSON ------------------------------------------------


@pytest.mark.parametrize("how", ["web", "env"])
def test_linked_patch_is_403_real_json(client, auth, monkeypatch, how):
    assert _patch(client, auth, {"base_branch": "test"}).status_code == 200
    if how == "web":
        _link_web(client, auth)
    else:
        _link_env(monkeypatch)
    _mock_cloud(monkeypatch, _cloud_body())
    r = _patch(client, auth, {"base_branch": "stage"})
    assert r.status_code == 403
    assert r.json() == {"detail": LINKED_403_DETAIL}
    from app.db import SessionLocal
    from app.models import Project
    db = SessionLocal()
    try:
        assert db.get(Project, "core").gitops_base_branch == "test"
    finally:
        db.close()


# ---- four-state at get_context AND GET; live_view rewrites cloud control -----------------


@pytest.mark.parametrize("how", ["web", "env"])
def test_state3_linked_unset_not_local_test(client, auth, monkeypatch, how):
    assert _patch(client, auth, {"base_branch": "test"}).status_code == 200
    if how == "web":
        _link_web(client, auth)
    else:
        _link_env(monkeypatch)
    _mock_cloud(monkeypatch, _cloud_body(writable=True, state="local"))

    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    _assert_unmeasured_fields(g)
    assert g["control"] == "linked_unset"
    assert g["base_branch"]["value"] != "test"
    assert "control" in g

    body = _get(client, auth).json()
    assert body["fields"]["base_branch"] == UNMEASURED
    assert body["control"]["state"] == "linked_unset"
    assert body["control"]["writable"] is False
    assert body["control"]["message"] == "Linked; the org has not set a git process."
    assert "controlled by the org admin" not in body["control"]["message"].lower()
    assert body["was"]["base_branch"] == "test"


@pytest.mark.parametrize("how", ["web", "env"])
def test_state4_linked_set_is_org_not_local(client, auth, monkeypatch, how):
    assert _patch(client, auth, {"base_branch": "test"}).status_code == 200
    if how == "web":
        _link_web(client, auth)
    else:
        _link_env(monkeypatch)
    _mock_cloud(monkeypatch, _cloud_body(base="stage", source="org", writable=True, state="local"))

    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    assert g["base_branch"] == {"value": "stage", "source": "org"}
    assert g["control"] == "linked_set"
    assert g["base_branch"]["value"] != "test"

    body = _get(client, auth).json()
    assert body["fields"]["base_branch"] == {"value": "stage", "source": "org"}
    assert body["control"]["state"] == "linked_set"
    assert body["control"]["writable"] is False
    assert body["control"]["message"] == "Controlled by the org admin."
    assert body["was"]["base_branch"] == "test"


def test_live_view_rewrites_cloud_control(client, auth, monkeypatch):
    _link_web(client, auth)
    _mock_cloud(monkeypatch, _cloud_body(base="stage", source="org", writable=True, state="local"))
    body = _get(client, auth).json()
    assert body["control"]["writable"] is False
    assert body["control"]["state"] == "linked_set"
    assert body["control"]["message"] == "Controlled by the org admin."


# ---- unreachable ≠ state 1; timeout is not AI provider; 404 is not linked_unset ----------


def test_timeout_is_linked_unreachable_not_ai_provider(client, auth, monkeypatch):
    assert _patch(client, auth, {"base_branch": "test"}).status_code == 200
    _link_env(monkeypatch)
    _mock_cloud(monkeypatch, error=httpx.TimeoutException("timed out"))

    body = _rpc(client, _mcp_key(client, auth), "get_context")
    dumped = json.dumps(body)
    assert "result" in body
    assert not body["result"].get("isError")
    assert "AI provider" not in dumped
    assert "unavailable" not in dumped
    g = _gitops(body["result"]["structuredContent"] if "structuredContent" in body["result"]
                else body["result"])
    _assert_unmeasured_fields(g)
    assert g["control"] == "linked_unreachable"
    assert g["note"] == gitops_svc.NOTE_UNREACHABLE
    assert g["base_branch"]["value"] != "test"
    assert "control" in g
    state1 = {"base_branch": UNMEASURED, "control": None}
    assert g.get("control") != state1["control"]

    rest = _get(client, auth).json()
    assert rest["control"]["state"] == "linked_unreachable"
    assert rest["fields"]["base_branch"]["value"] is None
    assert rest["control"]["message"] == gitops_svc.MESSAGES["linked_unreachable"]
    assert rest["was"]["base_branch"] == "test"
    assert rest["control"]["writable"] is False


def test_cloud_404_is_linked_unreachable_not_unset(client, auth, monkeypatch):
    _link_web(client, auth)
    _mock_cloud(monkeypatch, status=404, payload={"detail": "nope"})
    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    assert g["control"] == "linked_unreachable"
    assert g["control"] != "linked_unset"
    body = _get(client, auth).json()
    assert body["control"]["state"] == "linked_unreachable"


def test_linked_unreachable_patch_still_403(client, auth, monkeypatch):
    _link_env(monkeypatch)
    _mock_cloud(monkeypatch, error=httpx.TimeoutException("x"))
    r = _patch(client, auth, {"base_branch": "stage"})
    assert r.status_code == 403
    assert r.json() == {"detail": LINKED_403_DETAIL}


# ---- GET /api/sync/gitops is resolve_local, no outbound ----------------------------------


def test_sync_gitops_uses_this_db_and_does_not_outbound(client, auth, monkeypatch):
    assert _patch(client, auth, {"base_branch": "test"}).status_code == 200
    _link_web(client, auth)
    hits = []
    _mock_cloud(monkeypatch, error=AssertionError("must not outbound"), hits=hits)
    key = _sync_key(client, auth)
    r = client.get("/api/sync/gitops", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json()["fields"]["base_branch"]["value"] == "test"
    assert r.json()["control"]["state"] == "local"
    assert hits == []


def test_sync_gitops_403_without_sync_scope(client, auth):
    key = _mcp_key(client, auth)
    r = client.get("/api/sync/gitops", headers={"X-API-Key": key})
    assert r.status_code == 403


def test_sync_gitops_ignores_query_project_id(client, auth):
    other = client.post("/api/projects", json={"name": "Else"}, headers=auth).json()
    assert _patch(client, auth, {"base_branch": "core-only"}).status_code == 200
    key = _sync_key(client, auth, project_id="core")
    r = client.get(f"/api/sync/gitops?project_id={other['id']}", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json()["project_id"] == "core"
    assert r.json()["fields"]["base_branch"]["value"] == "core-only"


# ---- global key pid None -----------------------------------------------------------------


def test_global_key_get_context_still_has_gitops(client):
    from app.db import SessionLocal
    from app.models import User
    from app.security.passwords import hash_password

    db = SessionLocal()
    try:
        db.add(User(
            id="u_glob", name="Glob", handle="globk", email="glob@example.com",
            initials="G", password_hash=hash_password("graphban"),
        ))
        db.commit()
    finally:
        db.close()
    token = client.post(
        "/api/auth/login", json={"email": "glob@example.com", "password": "graphban"},
    ).json()["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}
    key = client.post(
        "/api/api-keys", json={"name": "g", "scopes": ["read", "write"], "project_id": None},
        headers=hdr,
    ).json()["plaintext"]
    ctx = _ctx(client, key)
    assert ctx["project_id"] is None
    g = _gitops(ctx)
    _assert_unmeasured_fields(g)
    assert "control" not in g


# ---- org GET is resolve_org; roster includes unreadable ----------------------------------


def _hosted_org(client, auth, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "hosted_mode", True)
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=auth).json()
    a = client.post(
        "/api/projects", json={"name": "App", "org_id": org["id"]}, headers=auth,
    ).json()
    b = client.post(
        "/api/projects", json={"name": "Lib", "org_id": org["id"]}, headers=auth,
    ).json()
    return org, a, b


def test_org_patch_empty_does_not_wipe_null_clears(client, auth, monkeypatch):
    """Sabotage the org PATCH caller: {} must not wipe house process; null clears."""
    from app.db import SessionLocal
    from app.models import Organization

    org, _a, _b = _hosted_org(client, auth, monkeypatch)
    assert client.patch(
        f"/api/orgs/{org['id']}/gitops", json={"base_branch": "stage"}, headers=auth,
    ).status_code == 200

    r = client.patch(f"/api/orgs/{org['id']}/gitops", json={}, headers=auth)
    assert r.status_code == 200
    assert r.json()["fields"]["base_branch"] == {"value": "stage", "source": "org"}
    db = SessionLocal()
    try:
        assert db.get(Organization, org["id"]).gitops_base_branch == "stage"
    finally:
        db.close()

    r = client.patch(
        f"/api/orgs/{org['id']}/gitops", json={"base_branch": None}, headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["fields"]["base_branch"] == UNMEASURED
    db = SessionLocal()
    try:
        assert db.get(Organization, org["id"]).gitops_base_branch is None
    finally:
        db.close()


def test_org_get_is_resolve_org_not_first_project(client, auth, monkeypatch):
    org, a, b = _hosted_org(client, auth, monkeypatch)
    assert client.patch(
        f"/api/orgs/{org['id']}/gitops", json={"base_branch": "stage"}, headers=auth,
    ).status_code == 200
    assert _patch(client, auth, {"base_branch": "main"}, project_id=a["id"]).status_code == 200

    r = client.get(f"/api/orgs/{org['id']}/gitops", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["fields"]["base_branch"] == {"value": "stage", "source": "org"}
    assert body["fields"]["base_branch"]["value"] != "main"
    assert body["project_id"] is None
    assert body["org_id"] == org["id"]
    ids = {p["id"] for p in body["projects"]}
    assert {a["id"], b["id"]} <= ids


def test_org_roster_includes_unreadable_projects(client, auth, monkeypatch):
    from app.db import SessionLocal
    from app.models import Membership

    org, a, b = _hosted_org(client, auth, monkeypatch)
    db = SessionLocal()
    try:
        row = db.query(Membership).filter_by(user_id="u1", project_id=b["id"]).one()
        db.delete(row)
        db.commit()
    finally:
        db.close()

    body = client.get(f"/api/orgs/{org['id']}/gitops", headers=auth).json()
    ids = [p["id"] for p in body["projects"]]
    assert b["id"] in ids
    assert a["id"] in ids
    names = [p["name"] for p in body["projects"]]
    assert names == sorted(names)


# ---- hosted GET org-member; writable rank; stranger 404; member PATCH 403 ----------------


def test_hosted_project_gitops_authz(client, auth, monkeypatch):
    from app.db import SessionLocal
    from app.models import Membership, OrgMembership

    org, a, b = _hosted_org(client, auth, monkeypatch)
    db = SessionLocal()
    try:
        row = db.query(Membership).filter_by(user_id="u1", project_id=b["id"]).one()
        db.delete(row)
        db.add(OrgMembership(org_id=org["id"], user_id="u2", role="member"))
        db.commit()
    finally:
        db.close()

    # org admin (alex) without a project seat on B: GET 200, writable true
    r = _get(client, auth, b["id"])
    assert r.status_code == 200
    assert r.json()["control"]["writable"] is True

    dana = client.post(
        "/api/auth/login", json={"email": "dana@ascme-labs.com", "password": "graphban"},
    ).json()
    dana_h = {"Authorization": f"Bearer {dana['access_token']}"}
    r = _get(client, dana_h, a["id"])
    assert r.status_code == 200
    assert r.json()["control"]["writable"] is False
    r = _patch(client, dana_h, {"base_branch": "x"}, project_id=a["id"])
    assert r.status_code == 403

    kate = client.post(
        "/api/auth/login", json={"email": "kate@ascme-labs.com", "password": "graphban"},
    ).json()
    kate_h = {"Authorization": f"Bearer {kate['access_token']}"}
    assert _get(client, kate_h, a["id"]).status_code == 404


def test_org_member_get_writable_false_patch_403(client, auth, monkeypatch):
    from app.db import SessionLocal
    from app.models import OrgMembership

    org, a, _b = _hosted_org(client, auth, monkeypatch)
    db = SessionLocal()
    try:
        db.add(OrgMembership(org_id=org["id"], user_id="u2", role="member"))
        db.commit()
    finally:
        db.close()
    dana = client.post(
        "/api/auth/login", json={"email": "dana@ascme-labs.com", "password": "graphban"},
    ).json()
    h = {"Authorization": f"Bearer {dana['access_token']}"}
    r = client.get(f"/api/orgs/{org['id']}/gitops", headers=h)
    assert r.status_code == 200
    assert r.json()["control"]["writable"] is False
    r = client.patch(f"/api/orgs/{org['id']}/gitops", json={"base_branch": "x"}, headers=h)
    assert r.status_code == 403


def test_stranger_org_gitops_404(client, auth, monkeypatch):
    org, _a, _b = _hosted_org(client, auth, monkeypatch)
    kate = client.post(
        "/api/auth/login", json={"email": "kate@ascme-labs.com", "password": "graphban"},
    ).json()
    h = {"Authorization": f"Bearer {kate['access_token']}"}
    assert client.get(f"/api/orgs/{org['id']}/gitops", headers=h).status_code == 404


def test_update_gitops_is_audited(client, auth):
    assert _patch(client, auth, {"base_branch": "stage"}).status_code == 200
    ev = client.get("/api/events", params={"project_id": "core"}, headers=auth).json()
    top = next(e for e in ev["results"] if e["action"] == "update_gitops")
    assert top["target_id"] == "core"
    assert "base_branch" in (top.get("meta") or {}).get("fields", ["base_branch"])


# ---- get_context still attaches gitops when the handler is the caller --------------------


def test_get_context_omitting_a_field_would_fail(client, auth):
    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    assert set(FIELDS) <= set(g)
    assert g["tokens"] == TOKENS
    assert "version_from" in g


def test_get_context_description_pins_unmeasured_sentences(client, auth):
    """Length pins cannot catch flavour replacing the unmeasured / not-main sentences."""
    from app.mcp_server import TOOLS

    desc = next(t["description"] for t in TOOLS if t["name"] == "get_context")
    assert "unmeasured" in desc
    assert "not 'use main'" in desc
    assert "linked_unreachable" in desc

    key = _mcp_key(client, auth)
    listed = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"X-API-Key": key},
    ).json()["result"]["tools"]
    listed_desc = next(t["description"] for t in listed if t["name"] == "get_context")
    assert listed_desc == desc


# ---- GRPH-617: sync gitops is GET only; a PATCH handler stays green without this ----------


def test_sync_gitops_rejects_patch_and_is_not_in_openapi(client, auth):
    """Owned threat: a sync key must not write org policy. GET-only is not a pin
    unless PATCH is asserted absent at the CALL (OpenAPI + HTTP)."""
    key = _sync_key(client, auth)
    r = client.patch(
        "/api/sync/gitops", json={"base_branch": "stage"}, headers={"X-API-Key": key},
    )
    assert r.status_code in (404, 405), r.text

    spec = client.get("/openapi.json").json()
    gitops = spec["paths"].get("/api/sync/gitops") or spec["paths"].get("/sync/gitops")
    assert gitops is not None, "GET /api/sync/gitops must be documented"
    assert "get" in gitops
    assert "patch" not in gitops, "a PATCH route on /api/sync/gitops is a write of org policy"


# ---- GRPH-618: resolve logs and org PATCH events at the CALL -----------------------------


def test_resolve_logs_project_state_and_link_source_on_get(client, auth, caplog):
    """Deleting the logger.info lines in resolve() must fail this — GET is the CALL."""
    caplog.set_level(logging.INFO, logger="graphban.gitops")
    assert _get(client, auth).status_code == 200
    msgs = [r.getMessage() for r in caplog.records if "gitops.resolve" in r.getMessage()]
    assert msgs, "gitops.resolve must log on GET /api/projects/{id}/gitops"
    last = msgs[-1]
    assert "project_id=core" in last
    assert "state=local" in last
    assert "linked_source=" in last


def test_fetch_failure_warning_does_not_log_the_key(client, auth, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="graphban.gitops")
    _link_web(client, auth)
    _mock_cloud(monkeypatch, error=httpx.TimeoutException("timed out"))
    assert _get(client, auth).status_code == 200
    dumped = " ".join(r.getMessage() for r in caplog.records)
    assert "gitops cloud fetch" in dumped
    assert "X-API-Key" not in dumped
    assert "api_key" not in dumped.lower()


def test_org_patch_update_gitops_is_audited(client, auth, monkeypatch):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Event

    org, _a, _b = _hosted_org(client, auth, monkeypatch)
    r = client.patch(
        f"/api/orgs/{org['id']}/gitops", json={"base_branch": "stage"}, headers=auth,
    )
    assert r.status_code == 200
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(Event).where(
                Event.action == "update_gitops",
                Event.target_type == "org",
                Event.target_id == org["id"],
            )
        ).all()
        assert rows, "org PATCH must record update_gitops"
        meta = rows[0].meta or {}
        assert "base_branch" in meta.get("fields", [])
        assert "stage" not in json.dumps(meta), "event meta is field names, not values"
    finally:
        db.close()


def test_linked_403_is_not_an_update_gitops_event(client, auth, monkeypatch):
    assert _patch(client, auth, {"base_branch": "test"}).status_code == 200
    before = client.get("/api/events", params={"project_id": "core"}, headers=auth).json()
    n = sum(1 for e in before["results"] if e["action"] == "update_gitops")
    _link_web(client, auth)
    r = _patch(client, auth, {"base_branch": "stage"})
    assert r.status_code == 403
    after = client.get("/api/events", params={"project_id": "core"}, headers=auth).json()
    n_after = sum(1 for e in after["results"] if e["action"] == "update_gitops")
    assert n_after == n, "linked 403 must not record update_gitops"


# ---- named models (PRD-32 slice 1). Pin the CALL, not apply_model() ----------------------


PRS_TO_BASE = {
    "no_push_to_base": True,
    "branch_name_pattern": "feat/{item_id}-{slug}",
    "pr_title_pattern": "{item_id} {slug}",
    "reviewer_bar": "both",
}


def _assert_prs_measured(body, *, source="project", base="main"):
    assert body["fields"]["base_branch"] == {"value": base, "source": source}
    for k, v in PRS_TO_BASE.items():
        assert body["fields"][k] == {"value": v, "source": source}, k
    assert body["version_from"] == {"value": "calver", "source": source}
    assert body["model"] == {"value": "prs_to_base", "source": source}


def test_patch_model_writes_the_six_fields_at_get_and_get_context(client, auth):
    """THE CALL. Deleting the model branch in apply_patch leaves GET/get_context
    unmeasured while this PATCH still returns 200 — a unit test of apply_model()
    would not see that."""
    r = _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"})
    assert r.status_code == 200, r.text
    _assert_prs_measured(r.json())

    body = _get(client, auth).json()
    _assert_prs_measured(body)

    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    assert g["base_branch"] == {"value": "main", "source": "project"}
    assert g["no_push_to_base"] == {"value": True, "source": "project"}
    assert g["branch_name_pattern"] == {"value": "feat/{item_id}-{slug}", "source": "project"}
    assert g["pr_title_pattern"] == {"value": "{item_id} {slug}", "source": "project"}
    assert g["reviewer_bar"] == {"value": "both", "source": "project"}
    assert g["version_from"] == {"value": "calver", "source": "project"}
    assert "model" not in g


def test_omit_model_and_base_branch_stays_unmeasured_when_github_is_connected(client, auth):
    from app.db import SessionLocal
    from app.services import platform as platform_svc

    db = SessionLocal()
    try:
        platform_svc.connect_github(db, "core", account="acme", repo="app")
    finally:
        db.close()

    r = _patch(client, auth, {})
    assert r.status_code == 200
    body = r.json()
    assert body["fields"]["base_branch"] == UNMEASURED
    assert body["model"] == UNMEASURED
    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    assert g["base_branch"] == UNMEASURED
    assert "main" not in _field_values(g).values()


def test_model_without_base_branch_is_422_and_does_not_write_main(client, auth):
    from app.db import SessionLocal
    from app.services import platform as platform_svc

    db = SessionLocal()
    try:
        platform_svc.connect_github(db, "core", account="acme", repo="app")
    finally:
        db.close()

    r = _patch(client, auth, {"model": "prs_to_base"})
    assert r.status_code == 422, r.text
    detail = json.dumps(r.json())
    assert "prs_to_base" in detail
    assert "base_branch" in detail
    body = _get(client, auth).json()
    assert body["fields"]["base_branch"] == UNMEASURED
    assert body["model"] == UNMEASURED
    assert body["fields"]["base_branch"]["value"] != "main"


def test_empty_base_branch_with_a_model_is_422(client, auth):
    r = _patch(client, auth, {"model": "push_to_base", "base_branch": ""})
    assert r.status_code == 422
    assert "push_to_base" in json.dumps(r.json())
    assert _get(client, auth).json()["model"] == UNMEASURED


def test_hand_edit_clears_the_model_id(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    r = _patch(client, auth, {"branch_name_pattern": "hotfix/{item_id}"})
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == UNMEASURED
    assert body["fields"]["branch_name_pattern"]["value"] == "hotfix/{item_id}"
    assert body["fields"]["base_branch"]["value"] == "main"
    assert body["fields"]["reviewer_bar"]["value"] == "both"


def test_clearing_the_model_does_not_wipe_the_six_fields(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    r = _patch(client, auth, {"model": None})
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == UNMEASURED
    assert body["fields"]["base_branch"]["value"] == "main"
    assert body["fields"]["reviewer_bar"]["value"] == "both"
    assert body["fields"]["no_push_to_base"]["value"] is True
    assert body["version_from"]["value"] == "calver"


def test_push_to_base_leaves_patterns_and_version_unmeasured(client, auth):
    r = _patch(client, auth, {"model": "push_to_base", "base_branch": "develop"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == {"value": "push_to_base", "source": "project"}
    assert body["fields"]["base_branch"]["value"] == "develop"
    assert body["fields"]["no_push_to_base"] == {"value": False, "source": "project"}
    assert body["fields"]["branch_name_pattern"] == UNMEASURED
    assert body["fields"]["pr_title_pattern"] == UNMEASURED
    assert body["fields"]["reviewer_bar"] == {"value": "sign_off", "source": "project"}
    assert body["version_from"] == UNMEASURED


def test_prs_to_integration_writes_the_same_fields_as_prs_to_base(client, auth):
    r = _patch(client, auth, {"model": "prs_to_integration", "base_branch": "stage"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == {"value": "prs_to_integration", "source": "project"}
    assert body["fields"]["base_branch"]["value"] == "stage"
    assert body["fields"]["no_push_to_base"]["value"] is True
    assert body["version_from"]["value"] == "calver"


def test_a_product_version_is_not_a_gitops_model(client, auth):
    r = _patch(client, auth, {"model": "2026.09.1", "base_branch": "main"})
    assert r.status_code == 422
    for m in gitops_svc.GITOPS_MODELS:
        assert m in json.dumps(r.json())
    assert _get(client, auth).json()["model"] == UNMEASURED
    from app.db import SessionLocal
    from app.models import Project
    db = SessionLocal()
    try:
        assert db.get(Project, "core").gitops_model is None
        assert db.get(Project, "core").gitops_base_branch is None
    finally:
        db.close()


def test_org_patch_model_writes_house_fields(client, auth, monkeypatch):
    org, _a, _b = _hosted_org(client, auth, monkeypatch)
    r = client.patch(
        f"/api/orgs/{org['id']}/gitops",
        json={"model": "prs_to_base", "base_branch": "stage"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    _assert_prs_measured(r.json(), source="org", base="stage")
    got = client.get(f"/api/orgs/{org['id']}/gitops", headers=auth).json()
    _assert_prs_measured(got, source="org", base="stage")


def test_linked_model_patch_is_still_403(client, auth, monkeypatch):
    _link_web(client, auth)
    _mock_cloud(monkeypatch, _cloud_body())
    r = _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"})
    assert r.status_code == 403
    assert r.json() == {"detail": LINKED_403_DETAIL}


def test_gitops_models_are_not_product_versions():
    for m in gitops_svc.GITOPS_MODELS:
        assert m in ("push_to_base", "prs_to_base", "prs_to_integration")
        assert not m[:4].isdigit()
    assert "base_branch" not in gitops_svc.PRESETS["prs_to_base"]
    assert gitops_svc.PRESETS["prs_to_base"] == gitops_svc.PRESETS["prs_to_integration"]


# ---- migration plan (PRD-32 slice 2). Pin PATCH → items, not file_migration_plan() ------


def _items(client, auth, project_id="core"):
    return client.get("/api/items", params={"project_id": project_id}, headers=auth).json()


def _plan_parents(client, auth, project_id="core"):
    return [
        i for i in _items(client, auth, project_id)
        if any(str(t).startswith("gitops-plan:") for t in (i.get("tags") or []))
    ]


def _plan_titles(client, auth, project_id="core"):
    return {
        i["title"] for i in _items(client, auth, project_id)
        if gitops_svc.PLAN_TAG in (i.get("tags") or [])
    }


def test_patch_model_files_the_plan_on_the_items_list(client, auth):
    """THE CALL. Deleting file_migration_plan from the project handler leaves
    PATCH 200 and an empty checklist."""
    before = _plan_parents(client, auth)
    r = _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"})
    assert r.status_code == 200, r.text
    parents = _plan_parents(client, auth)
    assert len(parents) == len(before) + 1
    parent = next(p for p in parents if gitops_svc.plan_tag("prs_to_base") in p["tags"])
    assert parent["title"] == "Gitops: PRs to base"
    assert parent["status"] == "next"
    titles = _plan_titles(client, auth)
    assert "Observe the repo" in titles
    assert "Confirm the remote" in titles
    assert "Confirm `main` exists and is HEAD" in titles
    assert "Stop pushing to base" in titles
    assert "One PR from current work" in titles
    assert "Contract is live" in titles
    assert "First tagged cut" in titles
    assert "Graphban does not run git" in parent["description"]


def test_get_gitops_names_the_filed_plan(client, auth):
    """THE CALL. Deleting plan=_plan_ref from resolve_local leaves GET plan null
    while the parent item exists."""
    r = _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"})
    assert r.status_code == 200, r.text
    parent = next(
        p for p in _plan_parents(client, auth)
        if gitops_svc.plan_tag("prs_to_base") in p["tags"]
    )
    expected = {"id": parent["id"], "title": "Gitops: PRs to base"}
    assert r.json()["plan"] == expected
    assert _get(client, auth).json()["plan"] == expected
    g = _gitops(_ctx(client, _mcp_key(client, auth)))
    assert "plan" not in g


def test_org_gitops_get_has_no_plan(client, auth, monkeypatch):
    org, _a, _b = _hosted_org(client, auth, monkeypatch)
    r = client.patch(
        f"/api/orgs/{org['id']}/gitops",
        json={"model": "prs_to_base", "base_branch": "stage"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["plan"] is None


def test_reapplying_the_same_model_does_not_duplicate_the_parent(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    first = _plan_parents(client, auth)
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    assert _plan_parents(client, auth) == first


def test_a_different_model_files_a_new_parent_and_leaves_the_old_one(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    assert _patch(client, auth, {"model": "push_to_base", "base_branch": "main"}).status_code == 200
    parents = _plan_parents(client, auth)
    tags = {gitops_svc.plan_tag("prs_to_base"), gitops_svc.plan_tag("push_to_base")}
    got = {t for p in parents for t in p["tags"] if str(t).startswith("gitops-plan:")}
    assert tags <= got
    titles = _plan_titles(client, auth)
    assert "One PR from current work" in titles
    assert "First tagged cut" in titles
    # push_to_base does not add a second copy of Observe; the first parent still has it.


def test_push_to_base_does_not_file_pr_or_tag_children(client, auth):
    assert _patch(client, auth, {"model": "push_to_base", "base_branch": "develop"}).status_code == 200
    titles = _plan_titles(client, auth)
    assert "Observe the repo" in titles
    assert "Contract is live" in titles
    assert "One PR from current work" not in titles
    assert "First tagged cut" not in titles
    assert "Stop pushing to base" not in titles


def test_org_model_patch_does_not_file_per_project_plans(client, auth, monkeypatch):
    org, a, _b = _hosted_org(client, auth, monkeypatch)
    r = client.patch(
        f"/api/orgs/{org['id']}/gitops",
        json={"model": "prs_to_base", "base_branch": "stage"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert _plan_parents(client, auth, a["id"]) == []


def test_linked_403_does_not_file_a_plan(client, auth, monkeypatch):
    _link_web(client, auth)
    _mock_cloud(monkeypatch, _cloud_body())
    before = _plan_parents(client, auth)
    r = _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"})
    assert r.status_code == 403
    assert _plan_parents(client, auth) == before


def test_parent_depends_on_each_child(client, auth):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Link

    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    parent = next(
        p for p in _plan_parents(client, auth)
        if gitops_svc.plan_tag("prs_to_base") in p["tags"]
    )
    db = SessionLocal()
    try:
        deps = list(db.scalars(
            select(Link).where(Link.a == parent["id"], Link.type == "dependency")
        ).all())
        assert len(deps) == 7
    finally:
        db.close()


def _plan_row(client, auth, title, project_id="core"):
    return next(
        i for i in _items(client, auth, project_id)
        if i["title"] == title and gitops_svc.PLAN_TAG in (i.get("tags") or [])
    )


def test_non_observe_children_are_blocked_until_an_answer(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    observe = _plan_row(client, auth, "Observe the repo")
    remote = _plan_row(client, auth, "Confirm the remote")
    assert observe["status"] == "next"
    assert observe["blocker"] in ("", None)
    assert remote["status"] == "blocked"
    assert remote["blocker"] == gitops_svc.OBSERVE_WAITING


def test_observe_remote_unblocks_siblings_at_update_item(client, auth):
    """THE CALL. Deleting apply_observe_answer from update_item leaves PATCH 200
    and Confirm the remote still waiting."""
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    observe = _plan_row(client, auth, "Observe the repo")
    r = client.patch(
        f"/api/items/{observe['id']}",
        json={"evidence": [{"kind": "note", "detail": "remote origin is github.com/acme/app"}]},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    remote = _plan_row(client, auth, "Confirm the remote")
    assert remote["status"] == "next"
    assert remote["blocker"] == ""
    one_pr = _plan_row(client, auth, "One PR from current work")
    assert one_pr["status"] == "next"


def test_observe_unknown_keeps_siblings_blocked(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    observe = _plan_row(client, auth, "Observe the repo")
    r = client.patch(
        f"/api/items/{observe['id']}",
        json={"evidence": [{"kind": "note", "detail": "unknown — could not look"}]},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    remote = _plan_row(client, auth, "Confirm the remote")
    assert remote["status"] == "blocked"
    assert remote["blocker"] == gitops_svc.OBSERVE_UNKNOWN_BLOCK


def test_observe_none_unblocks_siblings(client, auth):
    assert _patch(client, auth, {"model": "push_to_base", "base_branch": "develop"}).status_code == 200
    observe = _plan_row(client, auth, "Observe the repo")
    r = client.patch(
        f"/api/items/{observe['id']}",
        json={"evidence": [{"kind": "note", "detail": "none"}]},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    remote = _plan_row(client, auth, "Confirm the remote")
    assert remote["status"] == "next"
    assert remote["blocker"] == ""


def test_observe_review_without_an_answer_is_422(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    observe = _plan_row(client, auth, "Observe the repo")
    r = client.patch(
        f"/api/items/{observe['id']}",
        json={"status": "review"},
        headers=auth,
    )
    assert r.status_code == 422, r.text
    assert "remote" in r.text and "unknown" in r.text
    assert _plan_row(client, auth, "Observe the repo")["status"] == "next"


def test_parent_review_while_observe_is_unknown_is_422(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    observe = _plan_row(client, auth, "Observe the repo")
    assert client.patch(
        f"/api/items/{observe['id']}",
        json={"evidence": [{"kind": "note", "detail": "unknown"}]},
        headers=auth,
    ).status_code == 200
    parent = next(
        p for p in _plan_parents(client, auth)
        if gitops_svc.plan_tag("prs_to_base") in p["tags"]
    )
    r = client.patch(f"/api/items/{parent['id']}", json={"status": "review"}, headers=auth)
    assert r.status_code == 422, r.text
    assert "unknown" in r.text
    assert _plan_row(client, auth, "Confirm the remote")["status"] == "blocked"


def test_github_repo_is_not_an_observe_answer(client, auth):
    from app.db import SessionLocal
    from app.services import platform as platform_svc

    db = SessionLocal()
    try:
        platform_svc.connect_github(db, "core", account="acme", repo="app")
    finally:
        db.close()

    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    observe = _plan_row(client, auth, "Observe the repo")
    assert not (observe.get("evidence") or [])
    remote = _plan_row(client, auth, "Confirm the remote")
    assert remote["status"] == "blocked"
    assert remote["blocker"] == gitops_svc.OBSERVE_WAITING


def _unblock_observe(client, auth, project_id="core"):
    observe = _plan_row(client, auth, "Observe the repo", project_id)
    r = client.patch(
        f"/api/items/{observe['id']}",
        json={"evidence": [{"kind": "note", "detail": "remote"}]},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    return observe


def test_already_matches_children_are_tagged_and_describe_the_token(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    remote = _plan_row(client, auth, "Confirm the remote")
    assert gitops_svc.ALREADY_TAG in remote["tags"]
    assert "`already`" in remote["description"]
    assert "will not mark this done" in remote["description"]
    live = _plan_row(client, auth, "Contract is live")
    assert gitops_svc.ALREADY_TAG not in (live.get("tags") or [])


def test_already_evidence_does_not_auto_complete(client, auth):
    """THE CALL. Writing status=done inside apply_already_evidence fails this."""
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    _unblock_observe(client, auth)
    remote = _plan_row(client, auth, "Confirm the remote")
    assert remote["status"] == "next"
    r = client.patch(
        f"/api/items/{remote['id']}",
        json={"evidence": [{"kind": "note", "detail": "already origin exists"}]},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    got = _plan_row(client, auth, "Confirm the remote")
    assert got["status"] == "next"
    details = [e.get("detail", "") for e in (got.get("evidence") or [])]
    assert any(d.lower().startswith("already") for d in details)


def test_already_child_review_without_evidence_is_422(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    _unblock_observe(client, auth)
    remote = _plan_row(client, auth, "Confirm the remote")
    r = client.patch(
        f"/api/items/{remote['id']}",
        json={"status": "review"},
        headers=auth,
    )
    assert r.status_code == 422, r.text
    assert "already" in r.text
    assert _plan_row(client, auth, "Confirm the remote")["status"] == "next"


def test_already_child_review_with_already_is_not_done(client, auth):
    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    _unblock_observe(client, auth)
    remote = _plan_row(client, auth, "Confirm the remote")
    r = client.patch(
        f"/api/items/{remote['id']}",
        json={
            "status": "review",
            "evidence": [{"kind": "note", "detail": "already origin exists"}],
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    got = _plan_row(client, auth, "Confirm the remote")
    assert got["status"] == "review"
    assert got["status"] != "done"


def test_github_repo_does_not_write_already(client, auth):
    from app.db import SessionLocal
    from app.services import platform as platform_svc

    db = SessionLocal()
    try:
        platform_svc.connect_github(db, "core", account="acme", repo="app")
    finally:
        db.close()

    assert _patch(client, auth, {"model": "prs_to_base", "base_branch": "main"}).status_code == 200
    remote = _plan_row(client, auth, "Confirm the remote")
    assert not (remote.get("evidence") or [])
    assert gitops_svc.ALREADY_TAG in remote["tags"]
    assert remote["status"] == "blocked"
