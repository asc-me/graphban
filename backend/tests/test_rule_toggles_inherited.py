"""GRPH-1727: the toggles read carries the VALUE a Reset would land on, not only its source.

The rules dock draws it after "Reset to inherited", so the person sees the defaults they are
about to get back (and can edit from them) before they save. Per rule, `inherited.params` is
the org row's params when the org sets them, else the library default — on both the HTTP read
and the `get_rule_toggles` MCP tool, which share `rule_toggles.effective`.
"""
from tests.test_rule_toggles import (  # noqa: F401  (fixtures)
    RULE, _put_org, _put_project, _toggles, library, org,
)


def _mcp_toggles(client, org, pid):
    key = client.post("/api/api-keys", json={"name": "k", "project_id": pid,
                                             "tool_tiers": ["misc"]},
                      headers=org["owner"]).json()["plaintext"]
    res = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_rule_toggles", "arguments": {"project_id": pid}}},
        headers={"X-API-Key": key}).json()["result"]
    assert not res.get("isError"), res
    return {t["rule_id"]: t for t in res["structuredContent"]["toggles"]}


def test_inherited_params_is_the_library_default_when_the_org_has_no_params(client, org):
    r = _put_project(client, org, org["a"], {"params": {"min_tests_failed": 5}, "reason": "own"})
    assert r.status_code == 200, r.text
    for read in (_toggles, _mcp_toggles):
        t = read(client, org, org["a"])[RULE]
        # The effective value is the project's; what a reset lands on is the default.
        assert t["params"] == {"value": {"min_tests_failed": 5}, "source": "project"}
        assert t["inherits"]["params"] == "platform default"
        assert t["inherited"]["params"] == {"min_tests_failed": 1}
        assert t["inherited"]["on"] is False


def test_inherited_params_is_the_org_value_when_the_org_sets_params(client, org):
    assert _put_project(client, org, org["a"], {"params": {"min_tests_failed": 5},
                                                "reason": "own"}).status_code == 200
    assert _put_org(client, org, {"on": True, "params": {"min_tests_failed": 3},
                                  "reason": "org"}).status_code == 200
    for read in (_toggles, _mcp_toggles):
        t = read(client, org, org["a"])[RULE]
        assert t["params"] == {"value": {"min_tests_failed": 5}, "source": "project"}
        assert t["inherits"]["params"] == "org"
        assert t["inherited"]["params"] == {"min_tests_failed": 3}
        assert t["inherited"]["on"] is True
