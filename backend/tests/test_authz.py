"""AL-42: authorization is enforced, not just displayed.

A key's declared scopes ∩ its owner's memberships bound every MCP call; REST
mutations require project membership with write access. Seeded fixtures
(seed.py): alex = write on core/web/infra; ops = read on core, write on infra,
none on web; kate = read core/web.
"""
from tests import attest


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _mint(client, headers, **body):
    r = client.post("/api/api-keys", json={"name": "t", **body}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["plaintext"]


def _call(client, key, tool, arguments):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200
    return r.json()["result"]


def _error_code(result):
    assert result.get("isError") is True, result
    return result["structuredContent"]["error"]["code"]


# ---- key scopes ----

def test_read_scoped_key_cannot_mutate(client, auth):
    key = _mint(client, auth, scopes=["read"])
    res = _call(client, key, "create_item", {"title": "nope"})
    assert _error_code(res) == "unauthorized"
    assert "write" in res["structuredContent"]["error"]["message"]
    # ...but reads still work.
    ok = _call(client, key, "search_items", {"query": ""})
    assert ok.get("isError") is not True


# ---- project scope ----

def test_scoped_key_cannot_escape_via_project_id_arg(client, auth):
    key = _mint(client, auth, project_id="core")
    res = _call(client, key, "create_item", {"title": "escape", "project_id": "web"})
    assert _error_code(res) == "unauthorized"


def test_scoped_key_cannot_reach_foreign_item_by_id(client, auth):
    # AL-12 etc. live in core; a key scoped to web can't touch them by id.
    key = _mint(client, auth, project_id="web")
    for tool, args in [
        ("update_item", {"id": "AL-12", "status": "done"}),
        ("get_item_details", {"id": "AL-12"}),
        ("extract_lessons", {"id": "AL-12"}),
    ]:
        res = _call(client, key, tool, args)
        assert _error_code(res) == "unauthorized", (tool, res)


def test_key_never_outranks_its_owner(client):
    # ops has read-only membership on core: even a write-scoped key minted by
    # ops cannot write to core...
    ops = _login(client, "ops@ascme-labs.com")
    key = _mint(client, ops, project_id="core", scopes=["read", "write"])
    res = _call(client, key, "create_item", {"title": "sneak"})
    assert _error_code(res) == "unauthorized"
    # ...while the same key reads core fine.
    ok = _call(client, key, "get_backlog", {})
    assert ok.get("isError") is not True


def test_global_key_is_bounded_by_owner_memberships(client):
    # kate has no write access anywhere: a global write-scoped key finds no
    # project in scope at all.
    kate = _login(client, "kate@ascme-labs.com")
    key = _mint(client, kate, scopes=["read", "write"])
    res = _call(client, key, "create_item", {"title": "nowhere"})
    assert _error_code(res) == "unauthorized"
    assert "membership" in res["structuredContent"]["error"]["message"]


def test_list_projects_filtered_to_readable(client):
    kate = _login(client, "kate@ascme-labs.com")
    key = _mint(client, kate)
    res = _call(client, key, "list_projects", {})
    ids = {p["id"] for p in res["structuredContent"]["results"]}
    assert ids == {"core", "web"}  # kate: read core/web, none on infra


def test_get_context_reports_scope(client, auth):
    key = _mint(client, auth, project_id="core")
    ctx = _call(client, key, "get_context", {})["structuredContent"]
    assert ctx["readable_projects"] == ["core"]
    assert ctx["writable_projects"] == ["core"]


# ---- link integrity (rides on the scope guard) ----

def test_link_items_rejects_dangling_ids(client, auth):
    key = _mint(client, auth, project_id="core")
    res = _call(client, key, "link_items", {"a": "AL-12", "b": "AL-9999"})
    assert _error_code(res) == "not_found"
    assert "not found" in res["structuredContent"]["error"]["message"]


# ---- REST membership guards ----

def test_rest_non_member_cannot_patch_project(client):
    ops = _login(client, "ops@ascme-labs.com")  # web access: none
    r = client.patch("/api/projects/web", json={"name": "pwned"}, headers=ops)
    assert r.status_code == 404  # existence-hiding


def test_rest_read_only_member_gets_403_on_write(client):
    ops = _login(client, "ops@ascme-labs.com")  # core access: read
    r = client.patch("/api/items/AL-12", json=attest.complete_body(), headers=ops)
    assert r.status_code == 403


def test_rest_non_member_cannot_mint_key_for_project(client):
    ops = _login(client, "ops@ascme-labs.com")
    r = client.post("/api/api-keys", json={"name": "k", "project_id": "web"}, headers=ops)
    assert r.status_code == 404


def test_rest_shard_edit_requires_write_membership(client, auth):
    # Create a core shard as alex, then ops (read-only on core) may not edit it.
    shard = client.post(
        "/api/memory/shards",
        json={"text": "core fact", "scope": "global", "project_id": "core"},
        headers=auth,
    ).json()
    ops = _login(client, "ops@ascme-labs.com")
    r = client.patch(f"/api/memory/shards/{shard['id']}", json={"text": "tampered"}, headers=ops)
    assert r.status_code == 403


def test_rest_platform_write_requires_membership(client):
    ops = _login(client, "ops@ascme-labs.com")
    r = client.post("/api/platform/github/disconnect?project_id=web", headers=ops)
    assert r.status_code == 404
    r = client.patch("/api/platform?project_id=core", json={"llm_mode": "local"}, headers=ops)
    assert r.status_code == 403


def test_rest_project_list_filtered_to_memberships(client):
    kate = _login(client, "kate@ascme-labs.com")
    ids = {p["id"] for p in client.get("/api/projects", headers=kate).json()}
    assert ids == {"core", "web"}
