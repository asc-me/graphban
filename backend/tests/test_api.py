from tests.annotations import effective
import json
from app import tagging


def test_health(client):
    from app.version import __version__

    body = client.get("/health").json()
    assert body["status"] == "ok"          # process up + DB reachable in tests
    assert body["db"] == "ok"
    assert body["version"] == __version__  # release identity
    assert "git_sha" in body               # exact build (default "unknown" outside a built image)


def test_health_reports_git_sha_from_env(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "git_sha", "abc1234")
    assert client.get("/health").json()["git_sha"] == "abc1234"


def test_login_and_me(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200
    tok = r.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert me.json()["handle"] == "ascme"


def test_login_bad_password(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "nope"})
    assert r.status_code == 401


def test_items_require_auth(client):
    assert client.get("/api/items?project_id=core").status_code == 401


def test_seeded_items(client, auth):
    items = client.get("/api/items?project_id=core", headers=auth).json()
    assert len(items) == 9
    # Responses render the key from the project tag; `AL-12` is the frozen stored id
    # and never appears in output again (PRD-13). Requests still ACCEPT it — see below.
    assert items[0]["id"] == "CP-12"  # sort_order 0


def test_create_and_update_item(client, auth):
    r = client.post("/api/items", json={"title": "New thing", "tags": ["x"], "effort": 3}, headers=auth)
    assert r.status_code == 201
    iid = r.json()["id"]
    # Keys render from the project tag now, not a global prefix (PRD-13): the seeded
    # `core` project is tagged CP, and 23 is its own next number.
    assert iid == "CP-23"
    assert tagging.parse(iid) == ("CP", "item", 23)
    up = client.patch(f"/api/items/{iid}", json={"status": "in_progress"}, headers=auth)
    assert up.json()["status"] == "in_progress"


def test_invalid_status_rejected(client, auth):
    r = client.patch("/api/items/AL-12", json={"status": "banana"}, headers=auth)
    assert r.status_code == 422


def test_reorder(client, auth):
    items = client.get("/api/items?project_id=core", headers=auth).json()
    ids = [i["id"] for i in items]
    reversed_ids = list(reversed(ids))
    r = client.patch("/api/items/reorder", json={"ordered_ids": reversed_ids}, headers=auth)
    new_ids = [i["id"] for i in r.json()]
    assert new_ids == reversed_ids


def test_requests_vote_and_link(client, auth):
    reqs = client.get("/api/requests?project_id=core", headers=auth).json()
    assert len(reqs) == 5
    v = client.post("/api/requests/R-35/vote", json={"delta": 1}, headers=auth)
    assert v.json()["votes"] == 4
    ln = client.post("/api/requests/R-35/link", json={"item_id": "AL-12"}, headers=auth)
    assert ln.json()["status"] == "linked"
    # The reference field renders too, or a retag would leak the old tag through it.
    assert ln.json()["linked_to"] == "CP-12"  # posted as AL-12; resolution accepted it


def test_link_unknown_item_422(client, auth):
    r = client.post("/api/requests/R-35/link", json={"item_id": "AL-999"}, headers=auth)
    assert r.status_code == 422


def test_memory_search_ranks_relevant_first(client, auth):
    r = client.post(
        "/api/memory/search",
        json={"query": "pgvector single postgres container self-host", "top_k": 3, "project_id": "core"},
        headers=auth,
    )
    hits = r.json()
    assert hits[0]["shard"]["id"] == "m1"
    assert hits[0]["score"] >= hits[-1]["score"]


def test_add_memory_then_searchable(client, auth):
    client.post(
        "/api/memory/shards",
        json={"text": "Chose Vite over Next for the decoupled SPA frontend", "scope": "global", "project_id": "core"},
        headers=auth,
    )
    r = client.post("/api/memory/search", json={"query": "vite spa frontend decoupled", "top_k": 1, "project_id": "core"}, headers=auth)
    assert "Vite" in r.json()[0]["shard"]["text"]


def test_mcp_requires_key(client):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_mcp_tools_and_call(client, auth):
    key = client.post("/api/api-keys", json={"name": "test", "tool_tiers": ["prd", "codegraph", "fleet", "misc"]}, headers=auth).json()["plaintext"]
    hk = {"X-API-Key": key}
    tl = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=hk)
    names = [t["name"] for t in tl.json()["result"]["tools"]]
    assert set(names) >= {
        "get_context", "list_projects",
        "create_item", "update_item", "search_items", "add_memory", "search_memory",
        "get_backlog", "get_item_details", "suggest_next", "link_items", "extract_lessons",
        "generate_digest",
    }
    from app.mcp_server import LIVE_TOOL_COUNT
    assert len(names) == LIVE_TOOL_COUNT

    call = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "create_item", "arguments": {"title": "From agent", "effort": 1}}},
        headers=hk,
    )
    payload = json.loads(call.json()["result"]["content"][0]["text"])
    assert payload["title"] == "From agent"
    assert payload["project_id"]  # #5: writes confirm where they landed

    # The agent-created item is visible through the web API — shared service layer.
    items = client.get("/api/items?project_id=core", headers=auth).json()
    assert any(i["title"] == "From agent" for i in items)


def test_register_then_create_first_project(client):
    # A brand-new user (as after a full data wipe) can register and stand up a workspace.
    r = client.post(
        "/api/auth/register",
        json={"name": "Sam Rivers", "email": "sam@example.com", "handle": "sam", "password": "pw123456"},
    )
    assert r.status_code == 201, r.text
    hdr = {"Authorization": f"Bearer {r.json()['access_token']}"}

    proj = client.post("/api/projects", json={"name": "My First Project", "accent": "#a78bfa"}, headers=hdr)
    assert proj.status_code == 201, proj.text
    body = proj.json()
    assert body["name"] == "My First Project"
    assert body["id"] == "my-first-project"  # slugified from the name

    # It shows up in the project list and the creator is a member.
    projects = client.get("/api/projects", headers=hdr).json()
    assert any(p["id"] == "my-first-project" for p in projects)
    members = client.get("/api/projects/my-first-project/members", headers=hdr).json()
    assert any(m["role"] == "owner" and m["user"]["email"] == "sam@example.com" for m in members)

    # Creating an item scoped to the new project works and lands there (not "core").
    it = client.post(
        "/api/items",
        json={"title": "First task", "effort": 2, "project_id": "my-first-project"},
        headers=hdr,
    )
    assert it.status_code == 201, it.text
    assert it.json()["project_id"] == "my-first-project"
    listed = client.get("/api/items?project_id=my-first-project", headers=hdr).json()
    assert [i["title"] for i in listed] == ["First task"]


def test_create_item_unknown_project_is_404(client, auth):
    # A missing/incorrect project is a clean client error, not a 500. Since the
    # authz pass (AL-42), unknown and not-a-member are both existence-hiding 404s.
    r = client.post("/api/items", json={"title": "x", "project_id": "does-not-exist"}, headers=auth)
    assert r.status_code == 404


def _mcp(client, key, name, args):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers={"X-API-Key": key},
    )
    return json.loads(r.json()["result"]["content"][0]["text"])


def test_project_scoped_api_key_targets_its_project(client, auth):
    # Two fresh projects; a key scoped to A should write to A by default.
    client.post("/api/projects", json={"name": "Alpha"}, headers=auth)
    client.post("/api/projects", json={"name": "Beta"}, headers=auth)
    created = client.post("/api/api-keys", json={"name": "alpha-agent", "project_id": "alpha"}, headers=auth)
    assert created.status_code == 201
    assert created.json()["project_id"] == "alpha"
    key = created.json()["plaintext"]

    # Default: agent writes land in the key's project.
    item = _mcp(client, key, "create_item", {"title": "scoped task"})
    assert item["id"]
    got = client.get("/api/items?project_id=alpha", headers=auth).json()
    assert any(i["title"] == "scoped task" for i in got)

    # A project-scoped key can NOT escape its project via the project_id arg
    # (this used to work — the F1/AL-42 authz hole).
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "create_item",
                         "arguments": {"title": "override task", "project_id": "beta"}}},
        headers={"X-API-Key": key},
    ).json()["result"]
    assert r["isError"] is True
    assert r["structuredContent"]["error"]["code"] == "unauthorized"
    beta = client.get("/api/items?project_id=beta", headers=auth).json()
    assert not any(i["title"] == "override task" for i in beta)

    # A GLOBAL key from a user with write access to both projects may choose.
    gkey = client.post("/api/api-keys", json={"name": "global-agent"}, headers=auth).json()["plaintext"]
    _mcp(client, gkey, "create_item", {"title": "override task", "project_id": "beta"})
    beta = client.get("/api/items?project_id=beta", headers=auth).json()
    assert any(i["title"] == "override task" for i in beta)


def test_create_api_key_unknown_project_is_422(client, auth):
    r = client.post("/api/api-keys", json={"name": "x", "project_id": "nope"}, headers=auth)
    assert r.status_code == 422


def test_mcp_get_context_and_list_projects(client, auth):
    key = client.post("/api/api-keys", json={"name": "ctx", "project_id": "core", "tool_tiers": ["prd", "codegraph", "fleet", "misc"]}, headers=auth).json()["plaintext"]
    ctx = _mcp(client, key, "get_context", {})
    assert ctx["project_id"] == "core"
    assert ctx["key_project_id"] == "core"
    from app.mcp_server import LIVE_TOOL_COUNT
    # Minted with every tool tier above (GRPH-571), so `tool_count` is still the whole
    # manifest and this stays a test about `get_context` reporting the truth rather than a
    # test about tiering. A plain key would report 33 and that would ALSO be correct.
    assert ctx["tool_count"] == LIVE_TOOL_COUNT
    assert ctx["project_count"] >= 1

    projects = _mcp(client, key, "list_projects", {})["results"]
    assert any(p["id"] == "core" for p in projects)
    assert all({"id", "name", "accent"} <= set(p) for p in projects)


def test_mcp_search_items_matches_tags(client, auth):
    # Seeded items carry tags; search by tag must find them (was a description/behavior mismatch).
    key = client.post("/api/api-keys", json={"name": "srch"}, headers=auth).json()["plaintext"]
    # fields=full so the returned rows carry `tags` to verify the match (lean rows omit it).
    page = _mcp(client, key, "search_items", {"tags": ["mcp"], "fields": "full"})
    hits = page["results"]
    assert hits and all("mcp" in [t.lower() for t in i["tags"]] for i in hits)
    assert page["total"] >= len(hits) and page["has_more"] in (True, False)


def test_mcp_idempotent_create(client, auth):
    key = client.post("/api/api-keys", json={"name": "idem"}, headers=auth).json()["plaintext"]
    a = _mcp(client, key, "create_item", {"title": "once", "idempotency_key": "abc-123"})
    b = _mcp(client, key, "create_item", {"title": "once again", "idempotency_key": "abc-123"})
    assert a["id"] == b["id"]  # retry returns the original, no duplicate
    matches = [i for i in client.get("/api/items?project_id=core", headers=auth).json() if i["id"] == a["id"]]
    assert len(matches) == 1


def test_mcp_tools_carry_annotations(client, auth):
    key = client.post("/api/api-keys", json={"name": "ann"}, headers=auth).json()["plaintext"]
    tl = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                     headers={"X-API-Key": key})
    by = {t["name"]: t for t in tl.json()["result"]["tools"]}
    # Read through the defaults: the manifest sends only what differs from them, so
    # `create_item` advertises non-read-only by SAYING NOTHING, which is the same claim.
    assert effective(by["search_items"])["readOnlyHint"] is True
    assert effective(by["create_item"])["readOnlyHint"] is False
    assert effective(by["update_item"])["destructiveHint"] is True


def test_every_mcp_tool_declares_output_schema(client, auth):
    key = client.post("/api/api-keys", json={"name": "os"}, headers=auth).json()["plaintext"]
    tl = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                     headers={"X-API-Key": key})
    tools = tl.json()["result"]["tools"]
    missing = [t["name"] for t in tools if t.get("outputSchema", {}).get("type") != "object"]
    assert missing == [], f"tools without an object outputSchema: {missing}"


def test_mcp_returns_structured_content(client, auth):
    key = client.post("/api/api-keys",
                      json={"name": "sc",
                            "tool_tiers": ["prd", "codegraph", "fleet", "misc"]},
                      headers=auth).json()["plaintext"]
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_context", "arguments": {}}},
                    headers={"X-API-Key": key})
    result = r.json()["result"]
    from app.mcp_server import LIVE_TOOL_COUNT
    assert result["structuredContent"]["tool_count"] == LIVE_TOOL_COUNT  # typed, not JSON-in-text


def test_mcp_errors_are_structured_not_500(client, auth):
    key = client.post("/api/api-keys", json={"name": "err"}, headers=auth).json()["plaintext"]
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "update_item", "arguments": {"id": "AL-999"}}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200  # never a raw 500
    result = r.json()["result"]
    assert result["isError"] is True
    # AL-47: unknown id is now the precise `not_found` code, with a repair hint.
    err = result["structuredContent"]["error"]
    assert err["code"] == "not_found"
    assert "search_items" in err["hint"]


def test_create_project_slug_collision(client, auth):
    a = client.post("/api/projects", json={"name": "Duplicate"}, headers=auth).json()
    b = client.post("/api/projects", json={"name": "Duplicate"}, headers=auth).json()
    assert a["id"] == "duplicate"
    assert b["id"] == "duplicate-2"
