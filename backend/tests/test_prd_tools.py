"""AL-65 + AL-66: PRD authoring over MCP (create_prd/update_prd) and the grill
command (grill_prd) — the front half of the grill→PRD→decompose loop."""

from app import tagging


def _login(client, email="alex@ascme-labs.com"):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _key(client, auth, **body):
    return client.post("/api/api-keys", json={"name": "prd", **body}, headers=auth).json()["plaintext"]


def _mcp(client, key, tool, args):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]
    return r


def _ok(result):
    assert result.get("isError") is not True, result
    return result["structuredContent"]


# ---- AL-65: create_prd / update_prd ----

def test_create_prd_via_mcp(client, auth):
    key = _key(client, auth, project_id="core")
    prd = _ok(_mcp(client, key, "create_prd", {
        "title": "Grill loop", "body": "# Grill loop\n\n## Overview\n\n## Goals\n- ship it\n"
    }))
    assert tagging.parse(prd["id"])[1] == "prd"  # <TAG>-P<n>, not the old PRD- (PRD-13)
    assert prd["project_id"] == "core"
    assert prd["sections"] == ["Overview", "Goals"]  # `## ` headings, in order
    # And it's real: decompose_prd sees its sections.
    dec = _ok(_mcp(client, key, "decompose_prd", {"prd_id": prd["id"]}))
    assert dec["prd_id"] == prd["id"]


def test_create_prd_from_template(client, auth):
    key = _key(client, auth, project_id="core")
    prd = _ok(_mcp(client, key, "create_prd", {"title": "From template", "template": "standard"}))
    assert "Overview" in prd["sections"] and "Goals" in prd["sections"]


def test_update_prd_via_mcp(client, auth):
    key = _key(client, auth, project_id="core")
    prd = _ok(_mcp(client, key, "create_prd", {"title": "Draft"}))
    # A whole-body replace carries `base_hash` from get_prd (GRPH-357); without it the call
    # is refused, because an unread replace deletes what it did not reproduce.
    read = _ok(_mcp(client, key, "get_prd", {"prd_id": prd["id"]}))
    upd = _ok(_mcp(client, key, "update_prd", {
        "id_ignore": None, "prd_id": prd["id"], "status": "review",
        "base_hash": read["body_hash"], "body": "# Draft\n\n## Scope\n- narrowed\n",
    }))
    assert upd["status"] == "review"
    assert upd["sections"] == ["Scope"]


def test_update_prd_bad_status_is_validation(client, auth):
    key = _key(client, auth, project_id="core")
    prd = _ok(_mcp(client, key, "create_prd", {"title": "S"}))
    res = _mcp(client, key, "update_prd", {"prd_id": prd["id"], "status": "shipped"})
    assert res["structuredContent"]["error"]["code"] == "validation"


def test_update_prd_missing_is_not_found(client, auth):
    key = _key(client, auth, project_id="core")
    res = _mcp(client, key, "update_prd", {"prd_id": "PRD-9999", "title": "x"})
    assert res["structuredContent"]["error"]["code"] == "not_found"


# ---- AL-66: grill_prd ----

def test_grill_prd_returns_questions(client, auth):
    key = _key(client, auth, project_id="core")
    prd = _ok(_mcp(client, key, "create_prd", {"title": "Thin", "template": "standard"}))
    out = _ok(_mcp(client, key, "grill_prd", {"prd_id": prd["id"]}))
    assert out["prd_id"] == prd["id"]
    assert "?" in out["questions"]  # it's questions
    assert out["questions"].lstrip().startswith("-")  # a markdown bullet list


def test_grill_command_via_rest(client, auth):
    prd = client.post("/api/prds", json={"title": "R", "template": "standard", "project_id": "core"},
                      headers=auth).json()
    r = client.post(f"/api/prds/{prd['id']}/ai", json={"command": "grill"}, headers=auth)
    assert r.status_code == 200
    assert "?" in r.json()["text"]


# ---- authz ----

def test_read_only_member_cannot_create_prd(client):
    ops = _login(client, "ops@ascme-labs.com")  # read-only on core
    key = _key(client, ops, project_id="core")
    res = _mcp(client, key, "create_prd", {"title": "nope"})
    assert res["structuredContent"]["error"]["code"] == "unauthorized"


def test_scoped_key_cannot_grill_foreign_prd(client, auth):
    # Create a PRD in web; a key scoped to core can't grill it.
    web_prd = client.post("/api/prds", json={"title": "W", "project_id": "web"}, headers=auth).json()
    core_key = _key(client, auth, project_id="core")
    res = _mcp(client, core_key, "grill_prd", {"prd_id": web_prd["id"]})
    assert res["structuredContent"]["error"]["code"] == "unauthorized"


# ---- create_prd is audited (composes with AL-43) ----

def test_create_prd_is_audited(client, auth):
    key = _key(client, auth, project_id="core")
    prd = _ok(_mcp(client, key, "create_prd", {"title": "Audited"}))
    events = client.get("/api/events?project_id=core", headers=auth).json()["results"]
    assert any(e["action"] == "create_prd" and e["target_id"] == prd["id"] for e in events)
