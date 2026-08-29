"""Old and new wire names both work (AL-262, Graphban rename tier 1a).

This slice is deliberately *additive*: nothing it does changes what gets minted or
written. That ordering is the whole point. The self-host and the hosted tenant sync to
each other with a minted credential, so if the producing side moved first, the consuming
side would reject it — and the failure would look like an auth bug, not a rename.

So every assertion here is about **acceptance**. AL-263 has since flipped what is
produced (`gb_sk_`, `~/.graphban/`), and the acceptance assertions are unchanged — which
is the property that matters: nothing that ever worked stopped working.
"""
import pytest

from app.security import apikey


def _key(client, auth, **body) -> str:
    # Every tier (GRPH-571): this file is about ALIASES, and a tool absent because it was
    # not opted into would make an alias test pass for a reason that has nothing to do with
    # aliasing.
    body.setdefault("tool_tiers", ["prd", "codegraph", "fleet", "misc"])
    r = client.post("/api/api-keys", json={"name": "compat", **body}, headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()["plaintext"]


def _mcp(client, api_key: str, tool: str, args: dict | None = None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": api_key},
    )


# ---- API key prefix ---------------------------------------------------------------
def test_new_keys_are_minted_under_the_new_prefix(client, auth):
    """Flipped in AL-263, after AL-262 was confirmed live on BOTH instances (self-host
    alembic 0039 / git_sha 984630a, hosted auto-deployed). Until that was true, minting
    here would have produced credentials the other side rejected."""
    assert apikey.MINT_PREFIX == "gb_sk_"
    assert _key(client, auth).startswith("gb_sk_")


def test_no_prefix_is_ever_dropped():
    """Named explicitly rather than iterating ACCEPTED_PREFIXES — iterating the tuple
    passes happily after an entry is deleted, which is the one change that would silently
    lock out every key an instance has already issued. Keys are stored only as a hash and
    cannot be rewritten, so this list only ever grows."""
    for prefix in ("al_sk_", "gb_sk_"):
        assert prefix in apikey.ACCEPTED_PREFIXES, f"{prefix} must never be removed"
        assert apikey.is_api_key(prefix + "0" * 40)
    assert not apikey.is_api_key("nope_sk_" + "0" * 40)
    assert not apikey.is_api_key("")


def test_a_key_minted_under_either_prefix_authenticates(client, auth, monkeypatch):
    """The case that outlives the rename: every al_sk_ key already in an agent config or
    a CI secret has to keep authenticating indefinitely."""
    new = _key(client, auth)
    monkeypatch.setattr(apikey, "MINT_PREFIX", "al_sk_")
    old = _key(client, auth)
    assert new.startswith("gb_sk_") and old.startswith("al_sk_")

    for raw in (old, new):
        r = _mcp(client, raw, "list_projects")
        assert r.status_code == 200, (raw[:6], r.text)
        assert r.json()["result"].get("isError") is not True, r.json()


def test_bearer_auth_accepts_both_prefixes(client, auth, monkeypatch):
    """`security/deps` sniffs the Authorization header to tell an API key from a JWT.
    Sniffing only the old prefix would send a new key down the JWT path and 401."""
    new = _key(client, auth)
    monkeypatch.setattr(apikey, "MINT_PREFIX", "al_sk_")
    old = _key(client, auth)

    for raw in (old, new):
        r = client.post(
            "/api/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "list_projects", "arguments": {}}},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, (raw[:6], r.text)
        assert "error" not in r.json(), r.json()


def test_the_display_prefix_follows_whatever_minted_the_key(client, auth, monkeypatch):
    _key(client, auth)
    monkeypatch.setattr(apikey, "MINT_PREFIX", "al_sk_")
    _key(client, auth)
    prefixes = [k["prefix"] for k in client.get("/api/api-keys", headers=auth).json()]
    assert any(p.startswith("gb_sk_") for p in prefixes)
    assert any(p.startswith("al_sk_") for p in prefixes)


# ---- MCP tool name ----------------------------------------------------------------
def test_the_retired_tool_name_still_dispatches(client, auth):
    """Agents cache tool names in memory and in committed configs, so a name that ever
    worked has to keep working."""
    api_key = _key(client, auth, scopes=["read", "write"])
    for tool in ("report_graphban_issue", "report_agentledger_issue"):
        r = _mcp(client, api_key, tool, {"kind": "bug", "title": "t", "detail": "d"})
        assert r.status_code == 200, (tool, r.text)
        body = r.json()
        assert "error" not in body, (tool, body)
        # The upstream call may fail (no network in tests); what must NOT happen is
        # "unknown tool", which is what a missing alias would produce.
        assert "unknown tool" not in str(body).lower(), (tool, body)


def test_the_alias_is_not_advertised(client, auth):
    """An alias must not appear in tools/list or inflate the counts asserted elsewhere."""
    api_key = _key(client, auth)
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"X-API-Key": api_key},
    )
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "report_graphban_issue" in names
    assert "report_agentledger_issue" not in names
    assert len(names) == len(set(names))


def test_an_unknown_tool_is_still_an_error(client, auth):
    """Alias normalization must not accidentally make every name resolve."""
    api_key = _key(client, auth)
    r = _mcp(client, api_key, "report_nonsense_issue")
    assert "unknown tool" in str(r.json()).lower(), r.json()


def test_server_identifies_as_graphban(client, auth):
    api_key = _key(client, auth)
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"X-API-Key": api_key},
    )
    assert r.json()["result"]["serverInfo"]["name"] == "graphban"


def test_the_retired_tool_name_stays_local_when_proxying(client, auth):
    """The proxy picks local-vs-cloud BEFORE the dispatcher normalizes aliases, so the
    retired name has to be in the local allowlist too or it would start proxying."""
    from app.services import mcp_proxy

    assert "report_graphban_issue" in mcp_proxy.LOCAL_TOOLS
    assert "report_agentledger_issue" in mcp_proxy.LOCAL_TOOLS


# ---- CLI config location ----------------------------------------------------------
def test_config_is_read_from_either_location(tmp_path, monkeypatch):
    import json

    from app import cli

    monkeypatch.delenv("GRAPHBAN_CONFIG", raising=False)
    monkeypatch.delenv("AGENTLEDGER_CONFIG", raising=False)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))

    old = tmp_path / ".agentledger" / "config.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"project": "from-old"}))
    assert cli.load_config()["project"] == "from-old"

    # Once the new location exists it wins, without the old one being touched.
    new = tmp_path / ".graphban" / "config.json"
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps({"project": "from-new"}))
    assert cli.load_config()["project"] == "from-new"
    assert old.exists(), "an operator's existing config must never be moved or deleted"


def test_writes_go_to_the_new_location(tmp_path, monkeypatch):
    """Flipped in AL-263. The old file is still read when the new one is absent, and is
    never moved or deleted — it is the operator's file and it holds a live credential."""
    from app import cli

    monkeypatch.delenv("GRAPHBAN_CONFIG", raising=False)
    monkeypatch.delenv("AGENTLEDGER_CONFIG", raising=False)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))

    written = cli.save_config({"project": "core"})
    assert written == tmp_path / ".graphban" / "config.json"
    assert written.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".agentledger").exists(), "must not touch the old location"


@pytest.mark.parametrize("var", ["GRAPHBAN_CONFIG", "AGENTLEDGER_CONFIG"])
def test_either_env_override_wins(tmp_path, monkeypatch, var):
    import json

    from app import cli

    monkeypatch.delenv("GRAPHBAN_CONFIG", raising=False)
    monkeypatch.delenv("AGENTLEDGER_CONFIG", raising=False)
    target = tmp_path / "explicit.json"
    target.write_text(json.dumps({"project": "explicit"}))
    monkeypatch.setenv(var, str(target))

    assert cli.load_config()["project"] == "explicit"
    assert cli.save_config({"project": "explicit"}) == target
