"""AL-138: local MCP proxy — graph tools stay local, everything else forwards to the cloud
when the instance is linked."""
from app.services import mcp_proxy


def _key(client, auth, project_id="core"):
    return client.post("/api/api-keys", json={"name": "agent", "project_id": project_id},
                       headers=auth).json()["plaintext"]


def _mcp(client, key, tool, args=None):
    return client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}}}, headers={"X-API-Key": key}).json()


def _link(monkeypatch, url="https://cloud", key="gb_sk_link"):
    monkeypatch.setattr(mcp_proxy.settings, "sync_cloud_url", url)
    monkeypatch.setattr(mcp_proxy.settings, "sync_api_key", key)


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_should_proxy_only_when_linked_and_non_graph(monkeypatch):
    _link(monkeypatch, url="", key="")  # unlinked → never proxy
    assert not mcp_proxy.should_proxy("get_backlog")

    _link(monkeypatch)  # linked
    assert mcp_proxy.should_proxy("get_backlog")        # cloud-authoritative
    assert not mcp_proxy.should_proxy("search_code")    # graph → local
    assert not mcp_proxy.should_proxy("get_context")    # key meta → local


def test_non_graph_tool_forwards_to_cloud(client, auth, monkeypatch):
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, key=headers["X-API-Key"])
        return _Resp({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "[]"}],
            "structuredContent": {"results": [], "from": "cloud"}}})

    monkeypatch.setattr(mcp_proxy.httpx, "post", fake_post)
    _link(monkeypatch)

    r = _mcp(client, _key(client, auth), "get_backlog")
    # forwarded to the cloud MCP, authed with the org-minted link credential
    assert sent["url"] == "https://cloud/api/mcp" and sent["key"] == "gb_sk_link"
    assert sent["body"]["params"]["name"] == "get_backlog"
    # the cloud's result is returned verbatim
    assert r["result"]["structuredContent"]["from"] == "cloud"


def test_graph_tool_stays_local_when_linked(client, auth, monkeypatch):
    hits = {"n": 0}
    monkeypatch.setattr(mcp_proxy.httpx, "post", lambda *a, **k: hits.__setitem__("n", hits["n"] + 1))
    _link(monkeypatch)

    r = _mcp(client, _key(client, auth), "get_code_map")
    assert hits["n"] == 0 and "result" in r  # served locally, never forwarded


def test_unlinked_runs_everything_local(client, auth, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not forward when unlinked")

    monkeypatch.setattr(mcp_proxy.httpx, "post", boom)
    _link(monkeypatch, url="", key="")

    r = _mcp(client, _key(client, auth), "get_backlog")
    assert "result" in r  # local dispatch


def test_linked_get_context_stays_local_and_pulls_gitops(client, auth, monkeypatch):
    """Key meta stays local; gitops is fetched, not proxied as the whole tool."""
    from app.services import gitops as gitops_svc

    hits = {"mcp": 0, "gitops": 0}

    def fake_post(*a, **k):
        hits["mcp"] += 1
        raise AssertionError("get_context must not proxy")

    def fake_get(url, *a, **k):
        hits["gitops"] += 1
        class R:
            status_code = 200
            def json(self):
                fields = {f: {"value": None, "source": "unmeasured"}
                          for f in gitops_svc.FIELDS}
                return {
                    "project_id": "cloud",
                    "fields": fields,
                    "control": {"state": "local", "writable": True, "message": ""},
                    "was": None,
                    "version_from": {"value": None, "source": "unmeasured"},
                }
        return R()

    monkeypatch.setattr(mcp_proxy.httpx, "post", fake_post)
    monkeypatch.setattr(gitops_svc.httpx, "get", fake_get)
    _link(monkeypatch)

    assert not mcp_proxy.should_proxy("get_context")
    r = _mcp(client, _key(client, auth), "get_context")
    assert "result" in r and not r["result"].get("isError")
    g = r["result"]["structuredContent"]["gitops"]
    assert hits["mcp"] == 0 and hits["gitops"] == 1
    assert g["control"] == "linked_unset"
    for f in gitops_svc.FIELDS:
        assert f in g


def test_cloud_error_is_surfaced_as_a_tool_error(client, auth, monkeypatch):
    monkeypatch.setattr(mcp_proxy.httpx, "post",
                        lambda *a, **k: _Resp({"jsonrpc": "2.0", "id": 1,
                                               "error": {"code": -32000, "message": "cloud down"}}))
    _link(monkeypatch)
    r = _mcp(client, _key(client, auth), "get_backlog")
    assert r["result"]["isError"] is True and "cloud down" in r["result"]["content"][0]["text"]
