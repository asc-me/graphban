"""MCP `initialize` handshake — advertised version + spec negotiation."""
from app.mcp_server import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS


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
