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
                    "model": {"value": None, "source": "unmeasured"},
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


# ---- GRPH-1981: the caller's key limits are checked BEFORE forwarding ---------------------
# The cloud sees only the sync key, which carries none of the caller's limits, so a call the
# local check refuses must never reach `httpx.post`. Each refusal sits next to a control on
# the same key that IS forwarded — unchanged — so a refusal cannot pass by refusing everything.

def _recording_post(monkeypatch):
    sent: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json["params"])
        return _Resp({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "{}"}],
            "structuredContent": {"from": "cloud"}}})

    monkeypatch.setattr(mcp_proxy.httpx, "post", fake_post)
    return sent


def _prd_key_world(client, auth):
    """A project with two PRDs, an item in each (made before linking, so held locally), and
    a key limited to the first PRD."""
    proj = client.post("/api/projects", json={"name": "Boxed"}, headers=auth).json()["id"]
    owner = _key(client, auth, proj)
    mine, theirs = (client.post("/api/prds", json={"title": t, "project_id": proj},
                                headers=auth).json()["id"] for t in ("mine", "theirs"))

    def item(title, prd):
        r = _mcp(client, owner, "create_item", {"title": title, "prd_id": prd})
        return r["result"]["structuredContent"]["id"]

    inside, outside = item("inside", mine), item("outside", theirs)
    r = client.post("/api/api-keys", json={"name": "drain", "prd_id": mine}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["plaintext"], inside, outside


def _refused(r, rule=None):
    res = r["result"]
    assert res.get("isError") is True, res
    if rule:
        assert res["structuredContent"]["error"]["rule_id"] == rule, res
    return res


def test_a_prd_keys_out_of_prd_write_is_refused_locally_and_never_forwarded(
        client, auth, monkeypatch):
    drain, inside, outside = _prd_key_world(client, auth)
    sent = _recording_post(monkeypatch)
    _link(monkeypatch)

    _refused(_mcp(client, drain, "update_item", {"id": outside, "title": "renamed"}),
             "keys.prd_limited")
    _refused(_mcp(client, drain, "update_item", {"id": inside, "prd_id": "PRD-999"}),
             "keys.prd_limited")
    assert sent == []

    # Control: its own PRD's item is forwarded, arguments unchanged.
    args = {"id": inside, "title": "renamed"}
    r = _mcp(client, drain, "update_item", dict(args))
    assert r["result"]["structuredContent"]["from"] == "cloud"
    assert sent == [{"name": "update_item", "arguments": args}]


def test_a_prd_key_writing_an_item_the_box_does_not_hold_is_not_forwarded(
        client, auth, monkeypatch):
    """Locally an unknown item passes the PRD check; upstream it could be in any PRD."""
    drain, inside, _ = _prd_key_world(client, auth)
    sent = _recording_post(monkeypatch)
    _link(monkeypatch)

    _refused(_mcp(client, drain, "update_item", {"id": "CLOUD-77", "title": "x"}),
             "keys.prd_limited")
    _refused(_mcp(client, drain, "link_items", {"a": inside, "b": "CLOUD-77"}),
             "keys.prd_limited")
    assert sent == []


def test_a_prd_key_cannot_register_through_the_proxy(client, auth, monkeypatch):
    """Upstream the agent would sit on the sync key, with no PRD bound on its claims."""
    drain, _, _ = _prd_key_world(client, auth)
    sent = _recording_post(monkeypatch)
    _link(monkeypatch)

    _refused(_mcp(client, drain, "register_agent", {"label": "drain-a"}), "keys.prd_limited")
    _refused(_mcp(client, drain, "claim_next", {}), "keys.prd_limited")
    assert sent == []

    # Control: a read is not bounded by the PRD and still forwards.
    _mcp(client, drain, "get_backlog", {})
    assert [s["name"] for s in sent] == ["get_backlog"]


def test_a_read_only_key_cannot_write_upstream(client, auth, monkeypatch):
    ro = client.post("/api/api-keys", json={"name": "ro", "project_id": "core",
                                            "scopes": ["read"]}, headers=auth).json()["plaintext"]
    sent = _recording_post(monkeypatch)
    _link(monkeypatch)

    r = _mcp(client, ro, "create_item", {"title": "x"})
    assert r["result"]["isError"] is True
    assert r["result"]["structuredContent"]["error"]["code"] == "unauthorized"
    assert sent == []

    _mcp(client, ro, "get_backlog", {})
    assert [s["name"] for s in sent] == ["get_backlog"]


def test_a_role_restricted_key_is_held_to_its_ceiling_before_forwarding(
        client, auth, monkeypatch):
    from app.db import SessionLocal
    from app.models import ApiKey

    made = client.post("/api/api-keys", json={"name": "w", "project_id": "core"},
                       headers=auth).json()
    with SessionLocal() as s:
        s.get(ApiKey, made["id"]).roles = ["worker"]
        s.commit()
    worker = made["plaintext"]
    sent = _recording_post(monkeypatch)
    _link(monkeypatch)

    _refused(_mcp(client, worker, "mint_enrolment", {"agent_id": "X", "role": "worker"}))
    _refused(_mcp(client, worker, "register_agent", {"role_hint": "planner"}),
             "review.role_gated_tools")
    _refused(_mcp(client, worker, "register_agent", {}), "review.role_gated_tools")
    _refused(_mcp(client, worker, "register_agent",
                  {"role_hint": "worker", "enrolment_code": "WORKER-ABC"}),
             "review.role_gated_tools")
    assert sent == []

    # Control: a hint inside the ceiling forwards unchanged.
    _mcp(client, worker, "register_agent", {"role_hint": "worker", "label": "w1"})
    assert sent == [{"name": "register_agent",
                     "arguments": {"role_hint": "worker", "label": "w1"}}]


def test_an_unlimited_keys_call_is_forwarded_unchanged(client, auth, monkeypatch):
    sent = _recording_post(monkeypatch)
    _link(monkeypatch)
    args = {"title": "t", "status": "next", "touchpoints": ["a.py"]}
    _mcp(client, _key(client, auth), "create_item", dict(args))
    assert sent == [{"name": "create_item", "arguments": args}]
