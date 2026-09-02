"""MCP protocol surface: `initialize` handshake, `server/discover`, and the
2026-07-28 modern-era envelope — including the pin that LEGACY replies did not move."""
import base64

from app.mcp_server import (
    ERR_HEADER_MISMATCH,
    ERR_UNSUPPORTED_PROTOCOL_VERSION,
    META_SERVER_INFO,
    META_VERSION,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)


def _key(client, auth):
    return client.post("/api/api-keys", json={"name": "init"}, headers=auth).json()["plaintext"]


def _initialize(client, key, requested=None):
    params = {"protocolVersion": requested} if requested else {}
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params},
        headers={"X-API-Key": key},
    ).json()["result"]


def test_initialize_advertises_the_current_finalized_version(client, auth):
    assert PROTOCOL_VERSION == "2025-11-25"  # single owner of the advertised version
    r = _initialize(client, _key(client, auth))
    assert r["protocolVersion"] == "2025-11-25"
    assert r["capabilities"] == {"tools": {}}
    assert r["serverInfo"]["name"] == "graphban"
    from app.version import __version__
    assert r["serverInfo"]["version"] == __version__


def test_initialize_echoes_a_supported_requested_version(client, auth):
    key = _key(client, auth)
    for version in SUPPORTED_PROTOCOL_VERSIONS:
        assert _initialize(client, key, requested=version)["protocolVersion"] == version


def test_initialize_falls_back_to_default_for_an_unsupported_version(client, auth):
    r = _initialize(client, _key(client, auth), requested="2099-01-01")
    assert r["protocolVersion"] == PROTOCOL_VERSION


def test_initialize_never_echoes_the_modern_version(client, auth):
    # The compatibility matrix pins this: "an `initialize` request selects legacy
    # semantics" — and 2026-07-28 has no handshake semantics to select, so a
    # handshake asking for it gets the legacy default, not a version the client's
    # own messages cannot carry. Modern is requested per-request in `_meta` instead.
    key = _key(client, auth)
    assert _initialize(client, key, requested=MODERN_PROTOCOL_VERSION)["protocolVersion"] \
        == PROTOCOL_VERSION
    assert MODERN_PROTOCOL_VERSION not in SUPPORTED_PROTOCOL_VERSIONS


# --- modern era (2026-07-28): the per-request envelope (GRPH-223) --------------

def _modern(client, key, method, params=None, *, version=MODERN_PROTOCOL_VERSION,
            hdr_version=..., mcp_method=..., mcp_name=None, headers=None,
            send_version_meta=True):
    """POST one well-formed modern request. `...` means 'mirror the body'; passing
    an explicit value is how a test makes exactly one field disagree.
    `send_version_meta=False` builds a request WITHOUT `_meta` — i.e. legacy era.

    `params` here are the RPC's own params (e.g. name/arguments for tools/call);
    `_meta` is attached automatically."""
    meta = {}
    if send_version_meta:
        meta[META_VERSION] = version
        meta["io.modelcontextprotocol/clientInfo"] = {"name": "pytest", "version": "0"}
    body = {"jsonrpc": "2.0", "id": 1, "method": method,
            "params": {**(params or {}), "_meta": meta}}
    h = {"X-API-Key": key, **(headers or {})}
    hv = version if hdr_version is ... else hdr_version
    if hv is not None:
        h["MCP-Protocol-Version"] = hv
    h["Mcp-Method"] = method if mcp_method is ... else mcp_method
    if mcp_name is not None:
        h["Mcp-Name"] = mcp_name
    return client.post("/api/mcp", json=body, headers=h)


def _meta_of(resp_json):
    return resp_json["result"]["_meta"]


def test_discover_advertises_versions_capabilities_and_identity(client, auth):
    r = _modern(client, _key(client, auth), "server/discover").json()["result"]
    assert r["resultType"] == "complete"
    assert r["supportedVersions"] == sorted(
        SUPPORTED_PROTOCOL_VERSIONS | {MODERN_PROTOCOL_VERSION})
    assert r["capabilities"] == {"tools": {}}
    assert r["_meta"][META_SERVER_INFO]["name"] == "graphban"
    assert r["ttlMs"] > 0 and r["cacheScope"] == "private"


def test_discover_is_answered_without_meta_too(client, auth):
    # A dual-era client probing over HTTP may send the handshake-style variant;
    # answering costs nothing and is strictly more useful than -32601.
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1,
                                      "method": "server/discover"},
                    headers={"X-API-Key": _key(client, auth)}).json()["result"]
    assert "supportedVersions" in r
    assert "resultType" not in r  # legacy-era reply keeps the legacy shape


def test_modern_tools_list_carries_cache_hints_and_stamps(client, auth):
    r = _modern(client, _key(client, auth), "tools/list").json()["result"]
    assert set(r) == {"tools", "resultType", "ttlMs", "cacheScope", "_meta"}
    assert r["resultType"] == "complete"
    assert r["tools"], "a fresh key must still see its core manifest"
    assert r["_meta"][META_SERVER_INFO]["name"] == "graphban"


def test_modern_tools_call_stamps_success_and_error_alike(client, auth):
    # An `isError` payload is still a Result, and a modern client reading
    # `resultType` must not find it missing precisely when a tool failed —
    # absence-at-the-error-path is this repo's oldest defect class.
    key = _key(client, auth)
    ok = _modern(client, key, "tools/call",
                 {"name": "get_backlog", "arguments": {}},
                 mcp_name="get_backlog").json()["result"]
    assert ok["resultType"] == "complete"
    assert ok["structuredContent"] is not None
    assert ok["_meta"][META_SERVER_INFO]["name"] == "graphban"
    bad = _modern(client, key, "tools/call",
                  {"name": "no_such_tool", "arguments": {}},
                  mcp_name="no_such_tool").json()["result"]
    assert bad["isError"] and bad["resultType"] == "complete"


def test_modern_mcp_name_accepts_the_base64_sentinel(client, auth):
    # The spec's sentinel encoding must round-trip on the equality check, not just
    # the plain form — an encoded value compared raw would reject every non-ASCII
    # tool name we ever add.
    encoded = "=?base64?" + base64.b64encode(b"get_backlog").decode() + "?="
    r = _modern(client, _key(client, auth), "tools/call",
                {"name": "get_backlog", "arguments": {}}, mcp_name=encoded)
    assert r.status_code == 200
    assert r.json()["result"]["resultType"] == "complete"


def test_modern_request_without_version_header_is_rejected(client, auth):
    r = _modern(client, _key(client, auth), "tools/list", hdr_version=None)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ERR_HEADER_MISMATCH


def test_modern_header_and_meta_must_agree(client, auth):
    r = _modern(client, _key(client, auth), "tools/list",
                hdr_version="2025-11-25")  # body says modern
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ERR_HEADER_MISMATCH


def test_modern_unsupported_version_lists_what_is_supported(client, auth):
    # The retry path the spec is built around: a client asked for the future gets
    # a `supported` list it can pick from — and the rejected version back to prove
    # the data is about THIS request.
    r = _modern(client, _key(client, auth), "tools/list", version="2027-01-01")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == ERR_UNSUPPORTED_PROTOCOL_VERSION
    assert err["data"]["requested"] == "2027-01-01"
    assert err["data"]["supported"] == sorted(
        SUPPORTED_PROTOCOL_VERSIONS | {MODERN_PROTOCOL_VERSION})


def test_modern_method_header_must_match_the_body(client, auth):
    # Load balancers route on this header; if the server trusted the body without
    # checking, the two could describe different calls. That is the whole reason
    # -32020 exists.
    r = _modern(client, _key(client, auth), "tools/list", mcp_method="tools/call")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ERR_HEADER_MISMATCH


def test_modern_mcp_name_must_match_the_body(client, auth):
    r = _modern(client, _key(client, auth), "tools/call",
                {"name": "get_backlog", "arguments": {}}, mcp_name="suggest_next")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ERR_HEADER_MISMATCH


def test_modern_mcp_name_is_required_for_tools_call(client, auth):
    r = _modern(client, _key(client, auth), "tools/call",
                {"name": "get_backlog", "arguments": {}})  # no mcp_name
    assert r.status_code == 400
    assert r.json()["error"]["code"] == ERR_HEADER_MISMATCH


def test_modern_unknown_method_is_404(client, auth):
    # 404 + JSON-RPC body is how a probing client distinguishes a real modern
    # endpoint from a legacy server 404ing the path — a 200 here would read as
    # legacy and trigger a handshake fallback that then fails anyway.
    r = _modern(client, _key(client, auth), "server/no_such_rpc")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == -32601


# --- the anti-regression pin ----------------------------------------------------

def test_legacy_replies_keep_their_pre_adoption_shape(client, auth):
    # The entire back-compat story is that legacy clients see NOTHING change.
    # If a modern stamp leaks onto these replies, a strict 2025-era validator
    # (schema-checked clients exist) breaks on a Tuesday with no deploy on the
    # client side to blame. This test is the cheap version of that Tuesday.
    key = _key(client, auth)
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"X-API-Key": key}).json()["result"]
    assert set(r) == {"tools"}
    init = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": "2025-11-25"}},
                       headers={"X-API-Key": key}).json()["result"]
    assert set(init) == {"protocolVersion", "capabilities", "serverInfo"}
