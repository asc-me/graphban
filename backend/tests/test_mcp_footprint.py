"""AL-78: keep the MCP surface cheap for the agents that carry it.

Two costs an agent pays: the tool manifest shipped on every `tools/list`, and the
payload of every read. These tests pin the wins — a scope-gated manifest and lean
list rows — so they can't silently regress.
"""
import json

from app.mcp_server import TOOLS, _READ_ONLY


def _mint(client, auth, scopes):
    return client.post(
        "/api/api-keys", json={"name": "fp", "scopes": scopes}, headers=auth
    ).json()["plaintext"]


def _rpc(client, key, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/api/mcp", json=body, headers={"X-API-Key": key}).json()


def _list(client, key):
    return [t["name"] for t in _rpc(client, key, "tools/list")["result"]["tools"]]


def _call(client, key, tool, arguments):
    return _rpc(client, key, "tools/call",
               {"name": tool, "arguments": arguments})["result"]["structuredContent"]


# ---- Win #1: scope-gated manifest --------------------------------------------
def test_read_only_key_sees_only_read_tools(client, auth):
    """A key without the `write` scope can't call a mutation, so it shouldn't be
    shipped 16 write-tool schemas it would only get Forbidden on."""
    names = _list(client, _mint(client, auth, ["read"]))
    assert set(names) == set(_READ_ONLY)
    for write_tool in ("create_item", "update_item", "add_memory", "describe_code"):
        assert write_tool not in names


def test_write_key_sees_the_full_manifest(client, auth):
    names = _list(client, _mint(client, auth, ["read", "write"]))
    assert set(names) == {t["name"] for t in TOOLS}
    assert len(names) == len(TOOLS)


def test_get_context_tool_count_matches_the_scoped_manifest(client, auth):
    """The count get_context reports is what THIS key can call — not the server total —
    so it agrees with the manifest the agent actually received."""
    read_key = _mint(client, auth, ["read"])
    assert _call(client, read_key, "get_context", {})["tool_count"] == len(_READ_ONLY)
    write_key = _mint(client, auth, ["read", "write"])
    assert _call(client, write_key, "get_context", {})["tool_count"] == len(TOOLS)


# ---- Win #2: lean list rows, opt-in verbosity --------------------------------
def _seed_item(client, key):
    return _call(client, key, "create_item", {
        "title": "footprint fixture item",
        "touchpoints": ["backend/app/mcp_server.py"],
        "tags": ["footprint"],
        "effort": 3,
    })["id"]


def test_search_items_is_lean_by_default(client, auth):
    key = _mint(client, auth, ["read", "write"])
    _seed_item(client, key)
    page = _call(client, key, "search_items", {"query": "footprint fixture"})
    assert page["results"], "fixture item should be found"
    row = page["results"][0]
    assert set(row) == {"id", "title", "status"}
    # the fat fields are absent by default
    for fat in ("touchpoints", "assignee", "claimed_by", "prd_id", "fidelity", "effort"):
        assert fat not in row


def test_search_items_full_opts_back_in(client, auth):
    key = _mint(client, auth, ["read", "write"])
    _seed_item(client, key)
    page = _call(client, key, "search_items", {"query": "footprint fixture", "fields": "full"})
    row = page["results"][0]
    assert row["touchpoints"] == ["backend/app/mcp_server.py"]
    assert "fidelity" in row and "effort" in row


def test_get_backlog_lean_keeps_the_ranking_signal(client, auth):
    """The prioritization fields are the reason to call get_backlog, so lean drops the
    fat item fields but never ready/score."""
    key = _mint(client, auth, ["read", "write"])
    _seed_item(client, key)
    page = _call(client, key, "get_backlog", {})
    assert page["results"], "backlog should not be empty after seeding"
    row = page["results"][0]
    assert {"id", "title", "status", "ready", "score"} <= set(row)
    assert "touchpoints" not in row  # fat field stays opt-in
    full = _call(client, key, "get_backlog", {"fields": "full"})["results"][0]
    assert "touchpoints" in full and "score" in full


def test_bad_fields_value_is_a_validation_error(client, auth):
    key = _mint(client, auth, ["read"])
    res = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "search_items", "arguments": {"fields": "verbose"}}},
        headers={"X-API-Key": key},
    ).json()["result"]
    assert res["isError"] is True
    assert res["structuredContent"]["error"]["code"] == "validation"


# ---- regression guard: the manifest can't quietly bloat again ----------------
def test_manifest_stays_within_token_budget():
    """A ceiling, not an exact size — new tools are fine, unbounded prose is not.
    Measured ~7.4k tokens for the full manifest at the time of AL-78.

    Raised 9000 -> 9500 in AL-299, deliberately and with the numbers checked rather than
    as a reflex when it went red. PRD-14/15 added four tools (publish_memory,
    reject_memory, create_project, answer_grill) totalling ~3.7k chars; without them the
    manifest measures ~8.1k tokens, comfortably under the old ceiling. So the growth is
    tool COUNT, which this guard explicitly permits, not the verbosity it exists to
    catch.

    Prose was addressed first: ~740 chars came out of the four new descriptions and the
    shared effort/touchpoint strings before the ceiling moved at all. If this fires
    again, trim before raising — and check the same way, by subtracting whatever tools
    are new since the last bump.

    Raised 9500 -> 10500 in GRPH-254, by that same procedure. PRD-12's agent surface adds
    four tools (prd_acceptance, request_rebaseline, submit_verdict, close_prd) totalling
    ~3.9k chars; subtract them and the manifest measures ~9.0k tokens, comfortably under
    the old ceiling. They average 987 chars against 1001 for the existing 36, so this is
    tool COUNT again rather than the verbosity the guard exists to catch — and the count
    was already held down deliberately, since the eight acceptance READS share one tool
    behind a `view` argument instead of claiming eight names.

    ~1.0k chars came out of those four descriptions before the ceiling moved. The new
    headroom (~476 tokens) is the same slack the 9500 ceiling had before this change, so
    the guard stays as tight as it was rather than being loosened.

    Raised 10500 -> 11500 in GRPH-332, by the same procedure. PRD-17 D1 adds two tools
    (register_agent, fleet_status); subtract them and the manifest measures ~10474 tokens.
    Prose came out first and the numbers say it worked: the two started at 1240 chars each
    against a 995 average and were cut to 891, so they are now LEANER than the mean and the
    growth is tool COUNT, which this guard permits, rather than the verbosity it exists to
    catch.

    Worth recording what the measurement also showed: at the 10500 ceiling the surface had
    only ~26 tokens of headroom left, so tools had been added since GRPH-254 without anyone
    re-running this arithmetic. The new ~579 tokens restores roughly the slack the previous
    two bumps left, rather than buying room for another unchecked run of growth."""
    full_chars = len(json.dumps({"tools": TOOLS}))
    read_chars = len(json.dumps({"tools": [t for t in TOOLS if t["name"] in _READ_ONLY]}))
    assert full_chars // 4 < 11500, f"full manifest ~{full_chars // 4} tokens — trim descriptions"
    # scope-gating must keep buying its ~half-off for read keys
    assert read_chars < full_chars * 0.55
