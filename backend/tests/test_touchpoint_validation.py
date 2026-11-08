"""GRPH-1393: a touchpoint matching nothing in the code graph is warned about at filing."""
import json

from tests.annotations import effective


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
        "/api/api-keys",
        json={"name": "tp-agent", "project_id": "core", "tool_tiers": ["codegraph"]},
        headers=auth,
    ).json()["plaintext"]


def _describe(client, key, paths):
    _mcp(client, key, "describe_code", {"nodes": [
        {"path": p, "kind": "file", "name": p, "summary": "x", "content_hash": "h"}
        for p in paths]})


def test_create_item_warns_and_suggests_package_init(client, auth):
    key = _key(client, auth)
    _describe(client, key, ["backend/app/models/__init__.py", "backend/app/routers/items.py"])
    out = _mcp(client, key, "create_item", {
        "title": "t", "touchpoints": ["backend/app/models.py", "backend/app/routers/items.py"]})
    w = out["touchpoint_warnings"]
    assert [x["touchpoint"] for x in w] == ["backend/app/models.py"]
    assert w[0]["closest"] == "backend/app/models/__init__.py"


def test_update_item_warns_and_globs_dirs_pass(client, auth):
    key = _key(client, auth)
    _describe(client, key, ["backend/app/models/__init__.py"])
    item = _mcp(client, key, "create_item", {"title": "t"})
    assert "touchpoint_warnings" not in item
    out = _mcp(client, key, "update_item", {
        "id": item["id"], "touchpoints": ["backend/app/*", "backend/app/models", "web/src/api.ts"]})
    assert [x["touchpoint"] for x in out["touchpoint_warnings"]] == ["web/src/api.ts"]


def test_rest_create_carries_warning(client, auth):
    key = _key(client, auth)
    _describe(client, key, ["backend/app/models/__init__.py"])
    r = client.post("/api/items", json={
        "title": "t", "project_id": "core", "touchpoints": ["backend/app/models.py"]},
        headers=auth)
    assert r.status_code == 201
    assert r.json()["touchpoint_warnings"][0]["closest"] == "backend/app/models/__init__.py"
