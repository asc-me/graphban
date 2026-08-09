"""Upstream 'Report an issue with Graphban' — forwards a user/agent report to the
maintainer's intake. httpx is mocked so nothing leaves the test process.

The second half covers reaching a HOSTED intake (GRPH-326), which is the prerequisite for
PRD-16: no signal reaches the learning platform until reports can actually arrive. A
hosted receiver honours only the public share token and ignores `project_id` by design, so
one tenant cannot name another's project (AL-73).
"""
import json as _json

import httpx
import pytest

import app.services.upstream as up_svc


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_post(url, json=None, timeout=None):
    _fake_post.last = {"url": url, "json": json}
    return _FakeResp({"request": {"id": "R-42", "title": (json or {}).get("title")}, "duplicates": []})


def _mcp(client, key, name, args):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers={"X-API-Key": key},
    )
    return _json.loads(r.json()["result"]["content"][0]["text"])


def test_upstream_config_default_enabled(client, auth):
    r = client.get("/api/reports/upstream", headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] is True
    assert d["target"] == "feedback.asc-me.dev"  # from the default upstream URL


def test_upstream_config_requires_auth(client):
    assert client.get("/api/reports/upstream").status_code == 401


def test_upstream_report_forwards(client, auth, monkeypatch):
    monkeypatch.setattr(up_svc.httpx, "post", _fake_post)
    r = client.post(
        "/api/reports/upstream",
        json={"type": "bug", "title": "search_code 500s on empty query", "detail": "repro: …"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["request_id"] == "R-42"
    # forwarded to the configured upstream project with the right shape
    sent = _fake_post.last["json"]
    # Frozen until tier 3: this is a project id on the separate feedback.asc-me.dev
    # instance, so renaming it here would break report routing.
    assert sent["project_id"] == "agentledger"
    assert sent["type"] == "bug"
    assert sent["source_url"] == "graphban:in-app"


def test_upstream_report_requires_auth(client):
    assert client.post("/api/reports/upstream", json={"title": "x"}).status_code == 401


def test_upstream_disabled_when_url_blank(client, auth, monkeypatch):
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_url", "")
    assert client.get("/api/reports/upstream", headers=auth).json()["enabled"] is False
    r = client.post("/api/reports/upstream", json={"title": "x"}, headers=auth)
    assert r.status_code == 400  # not configured


def test_mcp_report_agentledger_issue(client, auth, monkeypatch):
    monkeypatch.setattr(up_svc.httpx, "post", _fake_post)
    key = client.post("/api/api-keys", json={"name": "reporter"}, headers=auth).json()["plaintext"]
    out = _mcp(client, key, "report_agentledger_issue",
               {"type": "feature", "title": "Add a dark-mode toggle", "detail": "…"})
    assert out["ok"] is True and out["request_id"] == "R-42"
    assert out["target"] == "feedback.asc-me.dev"
    assert _fake_post.last["json"]["source_url"] == "graphban:mcp-agent"


# ---- reaching a hosted intake (GRPH-326) ------------------------------------------------
class _Status:
    """A real httpx.HTTPStatusError, because the ordering bug this guards against is a
    subclass relationship — a hand-rolled stand-in would not reproduce it."""

    def __init__(self, code):
        self.code = code

    def __call__(self, url, json=None, timeout=None):
        request = httpx.Request("POST", url)
        response = httpx.Response(self.code, request=request)
        raise httpx.HTTPStatusError(f"HTTP {self.code}", request=request, response=response)


def test_the_share_token_is_sent_when_configured(client, auth, monkeypatch):
    """A hosted intake reads ONLY this. Without it every report 404s."""
    monkeypatch.setattr(up_svc.httpx, "post", _fake_post)
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "tok_live_123")

    client.post("/api/reports/upstream", json={"title": "x"}, headers=auth)
    assert _fake_post.last["json"]["token"] == "tok_live_123"


def test_both_addressing_modes_are_sent(client, auth, monkeypatch):
    """The receiver decides which one it honours, so one payload stays correct against
    either — the sender needs no knowledge of which kind it is talking to."""
    monkeypatch.setattr(up_svc.httpx, "post", _fake_post)
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "tok_live_123")

    client.post("/api/reports/upstream", json={"title": "x"}, headers=auth)
    sent = _fake_post.last["json"]
    assert sent["project_id"] == "agentledger" and sent["token"] == "tok_live_123"


def test_no_token_key_is_sent_when_unset(client, auth, monkeypatch):
    """A self-hosted receiver takes the raw project id. Sending an empty token would make
    a blank string look like a credential that was tried and rejected."""
    monkeypatch.setattr(up_svc.httpx, "post", _fake_post)
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "")

    client.post("/api/reports/upstream", json={"title": "x"}, headers=auth)
    assert "token" not in _fake_post.last["json"]


def test_a_rejected_report_is_not_reported_as_an_unreachable_host(client, auth, monkeypatch):
    """THE bug this guards. `HTTPStatusError` SUBCLASSES `HTTPError`, so catching the
    general case first turned every permanent 4xx into "unreachable, retry later" — which
    sends the next agent chasing dead hosts instead of reading its own config."""
    monkeypatch.setattr(up_svc.httpx, "post", _Status(404))

    r = client.post("/api/reports/upstream", json={"title": "x"}, headers=auth)
    assert "unreachable" not in r.json()["detail"].lower()
    assert "UPSTREAM_FEEDBACK_TOKEN" in r.json()["detail"]


def test_a_404_says_the_same_answer_covers_two_causes(client, auth, monkeypatch):
    """A project that has not enabled public sharing returns the same 404 as one that does
    not exist — deliberately, so the surface cannot be probed. Saying so stops an operator
    concluding their token is wrong when sharing is simply off."""
    monkeypatch.setattr(up_svc.httpx, "post", _Status(404))

    detail = client.post("/api/reports/upstream", json={"title": "x"}, headers=auth).json()["detail"]
    assert "public sharing" in detail


@pytest.mark.parametrize("code,status", [(400, 500), (403, 500), (500, 502), (503, 502)])
def test_whose_fault_it_is_shows_in_the_status(client, auth, monkeypatch, code, status):
    """502 says the upstream is at fault; a 4xx means ours is — our own config is wrong,
    and returning 502 there would send an operator to check someone else's server."""
    monkeypatch.setattr(up_svc.httpx, "post", _Status(code))

    assert client.post("/api/reports/upstream", json={"title": "x"},
                       headers=auth).status_code == status


def test_a_genuinely_unreachable_host_still_reads_as_unreachable(client, auth, monkeypatch):
    """The original behaviour has to survive the fix: a transport failure IS transient, and
    telling an operator to check their config would be the same mistake inverted."""
    def _refuse(url, json=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(up_svc.httpx, "post", _refuse)
    r = client.post("/api/reports/upstream", json={"title": "x"}, headers=auth)
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


def test_the_mcp_tool_says_retrying_will_not_help(client, auth, monkeypatch):
    """An agent branches on the hint. "Retry later" against a config error is an infinite
    loop that never fixes itself."""
    monkeypatch.setattr(up_svc.httpx, "post", _Status(404))
    key = client.post("/api/api-keys", json={"name": "reporter"}, headers=auth).json()["plaintext"]

    r = client.post("/api/mcp", headers={"X-API-Key": key}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "report_graphban_issue", "arguments": {"title": "x"}}})
    err = r.json()["result"]["structuredContent"]["error"]

    assert err["code"] == "conflict"
    assert "UPSTREAM_FEEDBACK_TOKEN" in err["message"]
    assert "will not help" in err.get("hint", "")
