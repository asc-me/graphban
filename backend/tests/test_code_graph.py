"""Code-structure graph: agent describes the codebase over MCP, then it's queryable."""
import json


def _mcp(client, key, name, args):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers={"X-API-Key": key},
    )
    return json.loads(r.json()["result"]["content"][0]["text"])


def _key(client, auth):
    return client.post(
        "/api/api-keys", json={"name": "code-agent", "project_id": "core"}, headers=auth
    ).json()["plaintext"]


ITEMS_NODE = {
    "path": "backend/app/services/items.py",
    # A source file is a `file`. These fixtures said `module` — the same drift GRPH-382 found
    # 119 times on the live graph — and the server now corrects it and says so.
    "kind": "file",
    "name": "items service",
    "lang": "python",
    "summary": "Owns tracker item CRUD, status transitions, and agent claiming/leasing.",
    "content_hash": "sha-aaa",
}
MEM_NODE = {
    "path": "backend/app/services/memory.py",
    "kind": "file",
    "name": "memory service",
    "lang": "python",
    "summary": "Semantic memory shards: embed on write, pgvector cosine search with a SQLite fallback.",
    "content_hash": "sha-bbb",
}


def test_describe_then_map_and_neighbors(client, auth):
    key = _key(client, auth)

    res = _mcp(client, key, "describe_code", {
        "nodes": [ITEMS_NODE, MEM_NODE],
        "edges": [{"src": ITEMS_NODE["path"], "dst": MEM_NODE["path"], "type": "imports"}],
    })
    assert res["nodes_upserted"] == 2
    assert res["edges_upserted"] == 1

    cmap = _mcp(client, key, "get_code_map", {})
    assert cmap["node_count"] == 2 and cmap["edge_count"] == 1
    assert {n["path"] for n in cmap["nodes"]} == {ITEMS_NODE["path"], MEM_NODE["path"]}
    assert all(n["fresh"] for n in cmap["nodes"])

    # Outgoing from items.py -> memory.py; incoming on memory.py from items.py.
    out = _mcp(client, key, "code_neighbors", {"path": ITEMS_NODE["path"]})
    assert out["node"]["kind"] == "file"
    assert {"dst": MEM_NODE["path"], "type": "imports"} in out["outgoing"]
    inc = _mcp(client, key, "code_neighbors", {"path": MEM_NODE["path"]})
    assert {"src": ITEMS_NODE["path"], "type": "imports"} in inc["incoming"]


def test_neighbors_intersects_item_touchpoints(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {"nodes": [ITEMS_NODE]})
    # A work item that touches the same path should surface under the code node.
    item = _mcp(client, key, "create_item", {
        "title": "Fix claim lease race", "touchpoints": [ITEMS_NODE["path"]],
    })
    nb = _mcp(client, key, "code_neighbors", {"path": ITEMS_NODE["path"]})
    assert any(t["id"] == item["id"] for t in nb["items_touching"])


def test_neighbors_on_undescribed_path_still_shows_inbound(client, auth):
    key = _key(client, auth)
    # Only items.py is described, but it imports an as-yet-undescribed config module.
    _mcp(client, key, "describe_code", {
        "nodes": [ITEMS_NODE],
        "edges": [{"src": ITEMS_NODE["path"], "dst": "backend/app/config.py", "type": "imports"}],
    })
    nb = _mcp(client, key, "code_neighbors", {"path": "backend/app/config.py"})
    assert nb["node"] is None  # never described
    assert {"src": ITEMS_NODE["path"], "type": "imports"} in nb["incoming"]


def test_search_code_ranks_by_summary(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {"nodes": [ITEMS_NODE, MEM_NODE]})
    hits = _mcp(client, key, "search_code", {"query": "pgvector cosine semantic search", "top_k": 2})
    assert hits["returned"] >= 1
    # The memory node's summary is the semantic match; it should rank first.
    assert hits["results"][0]["path"] == MEM_NODE["path"]


def test_redescribe_is_idempotent_by_path(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {"nodes": [ITEMS_NODE]})
    updated = {**ITEMS_NODE, "summary": "Now also emits code links on touchpoint change.", "content_hash": "sha-ccc"}
    _mcp(client, key, "describe_code", {"nodes": [updated]})
    cmap = _mcp(client, key, "get_code_map", {})
    paths = [n["path"] for n in cmap["nodes"]]
    assert paths.count(ITEMS_NODE["path"]) == 1  # upsert, not duplicate
    node = next(n for n in cmap["nodes"] if n["path"] == ITEMS_NODE["path"])
    assert node["content_hash"] == "sha-ccc"
    assert "code links" in node["summary"]


def test_prune_marks_absent_nodes_stale(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {"nodes": [ITEMS_NODE, MEM_NODE]})
    # Re-describe only items.py with prune -> memory.py wasn't seen, so it goes stale.
    res = _mcp(client, key, "describe_code", {"nodes": [ITEMS_NODE], "prune": True})
    assert res["marked_stale"] == 1
    cmap = _mcp(client, key, "get_code_map", {})
    fresh_by_path = {n["path"]: n["fresh"] for n in cmap["nodes"]}
    assert fresh_by_path[ITEMS_NODE["path"]] is True
    assert fresh_by_path[MEM_NODE["path"]] is False


def test_code_agent_answers_grounded_in_graph(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {
        "nodes": [ITEMS_NODE, MEM_NODE],
        "edges": [{"src": ITEMS_NODE["path"], "dst": MEM_NODE["path"], "type": "imports"}],
    })
    r = client.post(
        "/api/agent/code",
        json={"message": "what does the memory service do and what uses it?", "project_id": "core"},
        headers=auth,
    )
    assert r.status_code == 200
    data = r.json()
    # The answer is grounded in the retrieved code nodes...
    assert any(h["node"]["path"] == MEM_NODE["path"] for h in data["nodes"])
    # ...and the stub echoes the context, which carries the real dependency edge.
    assert ("depends on" in data["reply"]) or ("is used by" in data["reply"])
    assert MEM_NODE["path"] in data["reply"]


def test_code_agent_admits_when_area_undescribed(client, auth):
    # A fresh project with nothing described — the agent must not invent code.
    client.post("/api/projects", json={"name": "Bare"}, headers=auth)
    r = client.post(
        "/api/agent/code",
        json={"message": "what depends on the router?", "project_id": "bare"},
        headers=auth,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["nodes"] == []
    assert "described yet" in data["reply"]


def test_code_map_endpoint(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {
        "nodes": [ITEMS_NODE, MEM_NODE],
        "edges": [{"src": ITEMS_NODE["path"], "dst": MEM_NODE["path"], "type": "imports"}],
    })
    r = client.get("/api/agent/code/map?project_id=core", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["node_count"] == 2 and data["edge_count"] == 1
    assert {n["path"] for n in data["nodes"]} == {ITEMS_NODE["path"], MEM_NODE["path"]}
    assert {"src": ITEMS_NODE["path"], "dst": MEM_NODE["path"], "type": "imports"} in data["edges"]


def test_code_neighbors_endpoint(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {
        "nodes": [ITEMS_NODE, MEM_NODE],
        "edges": [{"src": ITEMS_NODE["path"], "dst": MEM_NODE["path"], "type": "imports"}],
    })
    item = _mcp(client, key, "create_item", {
        "title": "Tune the embedder", "touchpoints": [MEM_NODE["path"]],
    })
    r = client.get(f"/api/agent/code/neighbors?path={MEM_NODE['path']}&project_id=core", headers=auth)
    assert r.status_code == 200
    nb = r.json()
    assert nb["node"]["path"] == MEM_NODE["path"]
    assert {"src": ITEMS_NODE["path"], "type": "imports"} in nb["incoming"]
    assert any(t["id"] == item["id"] for t in nb["items_touching"])


def test_code_map_requires_auth(client):
    assert client.get("/api/agent/code/map").status_code == 401


def test_code_agent_requires_auth(client):
    r = client.post("/api/agent/code", json={"message": "hi"})
    assert r.status_code == 401


def test_code_agent_stream_emits_nodes_then_deltas(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {"nodes": [MEM_NODE]})
    r = client.post(
        "/api/agent/code/stream",
        json={"message": "semantic search over shards", "project_id": "core"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.text
    assert "event: nodes" in body
    assert "event: delta" in body
    assert "event: done" in body
    assert MEM_NODE["path"] in body


def test_link_item_and_request_to_code_both_ways(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {"nodes": [MEM_NODE]})
    r1 = _mcp(client, key, "link_code", {"ref_id": "AL-12", "path": MEM_NODE["path"], "relation": "implements"})
    assert r1["ref_type"] == "item" and r1["relation"] == "implements"
    r2 = _mcp(client, key, "link_code", {"ref_id": "R-35", "path": MEM_NODE["path"], "relation": "fixes"})
    assert r2["ref_type"] == "request"

    # Code side: code_neighbors surfaces the explicit linked work.
    nb = _mcp(client, key, "code_neighbors", {"path": MEM_NODE["path"]})
    assert any(x["id"] == "AL-12" and x["relation"] == "implements" for x in nb["linked_items"])
    assert any(x["id"] == "R-35" and x["relation"] == "fixes" for x in nb["linked_requests"])

    # Work side (REST reverse): the item's linked code, with the described node attached.
    rows = client.get("/api/agent/code/for?ref_id=AL-12&project_id=core", headers=auth).json()
    hit = next(r for r in rows if r["path"] == MEM_NODE["path"])
    assert hit["relation"] == "implements" and hit["node"]["kind"] == "file"


def test_link_code_is_idempotent(client, auth):
    key = _key(client, auth)
    a = _mcp(client, key, "link_code", {"ref_id": "AL-12", "path": "backend/app/x.py", "relation": "affects"})
    b = _mcp(client, key, "link_code", {"ref_id": "AL-12", "path": "backend/app/x.py", "relation": "affects"})
    assert a["id"] == b["id"]  # same row, not a duplicate


def test_link_code_links_undescribed_path(client, auth):
    # The bridge works before the code is described (dangling target).
    key = _key(client, auth)
    _mcp(client, key, "link_code", {"ref_id": "AL-12", "path": "backend/app/not_yet.py"})
    rows = client.get("/api/agent/code/for?ref_id=AL-12&project_id=core", headers=auth).json()
    hit = next(r for r in rows if r["path"] == "backend/app/not_yet.py")
    assert hit["relation"] == "affects" and hit["node"] is None


def test_unlink_code(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "link_code", {"ref_id": "AL-12", "path": "backend/app/y.py", "relation": "affects"})
    res = _mcp(client, key, "unlink_code", {"ref_id": "AL-12", "path": "backend/app/y.py"})
    assert res["removed"] == 1
    rows = client.get("/api/agent/code/for?ref_id=AL-12&project_id=core", headers=auth).json()
    assert not any(r["path"] == "backend/app/y.py" for r in rows)


def test_link_code_unknown_ref_errors(client, auth):
    key = _key(client, auth)
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "link_code", "arguments": {"ref_id": "AL-9999", "path": "backend/app/z.py"}}},
        headers={"X-API-Key": key},
    )
    result = r.json()["result"]
    assert result.get("isError") is True
    assert result["structuredContent"]["error"]["code"] == "not_found"


def test_code_link_rest_endpoints(client, auth):
    _mcp(client, _key(client, auth), "describe_code", {"nodes": [ITEMS_NODE]})
    created = client.post(
        "/api/agent/code/link",
        json={"ref_id": "AL-08", "path": ITEMS_NODE["path"], "relation": "affects"},
        headers=auth,
    )
    assert created.status_code == 201
    assert created.json()["ref_type"] == "item"
    # unknown ref → 404 (existence-hiding not-found, AL-47)
    bad = client.post("/api/agent/code/link", json={"ref_id": "AL-9999", "path": ITEMS_NODE["path"]}, headers=auth)
    assert bad.status_code == 404
    # unlink via REST
    gone = client.post("/api/agent/code/unlink", json={"ref_id": "AL-08", "path": ITEMS_NODE["path"]}, headers=auth)
    assert gone.json()["removed"] == 1


def test_code_chat_grounds_in_linked_work(client, auth):
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {"nodes": [MEM_NODE]})
    _mcp(client, key, "link_code", {"ref_id": "R-35", "path": MEM_NODE["path"], "relation": "fixes"})
    r = client.post(
        "/api/agent/code",
        json={"message": "what work is open on the memory service?", "project_id": "core"},
        headers=auth,
    )
    # The stub echoes the grounded context, which now carries the linked request.
    assert "R-35" in r.json()["reply"]


def test_code_tools_are_scoped_and_read_only_flags(client, auth):
    key = _key(client, auth)
    tl = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                     headers={"X-API-Key": key})
    by = {t["name"]: t for t in tl.json()["result"]["tools"]}
    assert by["describe_code"]["annotations"]["readOnlyHint"] is False
    assert by["describe_code"]["annotations"]["idempotentHint"] is True
    for ro in ("get_code_map", "code_neighbors", "search_code"):
        assert by[ro]["annotations"]["readOnlyHint"] is True
    # project scoping is injected onto the input schema
    assert "project_id" in by["describe_code"]["inputSchema"]["properties"]
