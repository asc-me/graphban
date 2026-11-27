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


SELF_HOSTED = "https://intake.example.test/api/public/requests"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """The default URL is the hosted intake, which refuses a tokenless report up front
    (GRPH-1982). Tests that exercise the send path get a token; the ones about sending
    WITHOUT one point at a self-hosted intake, which accepts that."""
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "tok_default")
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token_required", None)


def _self_hosted_no_token(monkeypatch):
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_url", SELF_HOSTED)
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "")


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
    assert "upstream_feedback_unconfigured" in r.json()["detail"]


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
    _self_hosted_no_token(monkeypatch)

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


def test_explain_upstream_404_names_the_unset_token(monkeypatch):
    """Local check: no upstream disclosure needed to name a missing token."""
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "")
    assert up_svc.explain_upstream_404() == "UPSTREAM_FEEDBACK_TOKEN unset on this server"
    assert "public sharing" not in up_svc.explain_upstream_404()


def test_explain_upstream_404_with_a_token_names_both_possibilities(monkeypatch):
    """Token was sent and refused. The intake gives one 404 for a stale/wrong token and for
    a project with sharing off, so asserting either alone states a guess as fact."""
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "tok_live_123")
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_project", "super-arc")
    said = up_svc.explain_upstream_404()
    assert "was sent and not accepted" in said
    assert "not a current share token" in said
    assert "the project it belongs to has not enabled public sharing" in said
    # Hosted intakes resolve the project from the token, so the configured id may be wrong.
    assert "super-arc" not in said
    assert "UPSTREAM_FEEDBACK_TOKEN unset" not in said


def test_a_404_without_a_token_names_the_unset_token(client, auth, monkeypatch):
    """THE bug this guards. One 404 used to name BOTH causes, so an operator with no
    token still read 'public sharing' and went looking at the wrong knob."""
    monkeypatch.setattr(up_svc.httpx, "post", _Status(404))
    _self_hosted_no_token(monkeypatch)

    detail = client.post("/api/reports/upstream", json={"title": "x"}, headers=auth).json()["detail"]
    assert "UPSTREAM_FEEDBACK_TOKEN unset on this server" in detail
    assert "public sharing" not in detail


def test_a_404_with_a_token_names_both_possibilities(client, auth, monkeypatch):
    """The other half of the split: a token was sent and refused — stale token OR sharing off.
    No project is named: a hosted intake resolves it from the token, not the configured id."""
    monkeypatch.setattr(up_svc.httpx, "post", _Status(404))
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "tok_live_123")

    detail = client.post("/api/reports/upstream", json={"title": "x"}, headers=auth).json()["detail"]
    assert "has not enabled public sharing" in detail
    assert "not a current share token" in detail
    assert "UPSTREAM_FEEDBACK_TOKEN unset" not in detail


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


def _mcp_report_error(client, key):
    r = client.post("/api/mcp", headers={"X-API-Key": key}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "report_graphban_issue", "arguments": {"title": "x"}}})
    return r.json()["result"]["structuredContent"]["error"]


def test_the_mcp_tool_says_retrying_will_not_help(client, auth, monkeypatch):
    """An agent branches on the hint. "Retry later" against a config error is an infinite
    loop that never fixes itself. Token-unset is the SuperArc field case (GRPH-927)."""
    monkeypatch.setattr(up_svc.httpx, "post", _Status(404))
    _self_hosted_no_token(monkeypatch)
    key = client.post("/api/api-keys", json={"name": "reporter"}, headers=auth).json()["plaintext"]

    err = _mcp_report_error(client, key)

    assert err["code"] == "conflict"
    assert "UPSTREAM_FEEDBACK_TOKEN unset on this server" in err["message"]
    assert "public sharing" not in err["message"]
    assert "will not help" in err.get("hint", "")


def test_the_mcp_tool_names_both_possibilities_when_the_token_is_set(
        client, auth, monkeypatch):
    """THE CALL this guards. A token was sent and refused: either it is not a current
    share token or the project has sharing off — and the sender cannot tell which."""
    monkeypatch.setattr(up_svc.httpx, "post", _Status(404))
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "tok_live_123")
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_project", "super-arc")
    key = client.post("/api/api-keys", json={"name": "reporter"}, headers=auth).json()["plaintext"]

    err = _mcp_report_error(client, key)

    assert err["code"] == "conflict"
    assert "has not enabled public sharing" in err["message"]
    assert "not a current share token" in err["message"]
    assert "super-arc" not in err["message"]
    assert "UPSTREAM_FEEDBACK_TOKEN unset" not in err["message"]
    assert "will not help" in err.get("hint", "")


# ---- a hosted intake with no token is refused up front (GRPH-1982) ----------------------
def _explode(url, json=None, timeout=None):
    raise AssertionError("a report the intake cannot accept must not be sent")


def test_the_mcp_tool_refuses_unsent_with_a_named_reason_when_the_token_is_unset(
        client, auth, monkeypatch):
    """THE CALL this guards: the planner's report_graphban_issue that was lost. The default
    intake is hosted, so with no token the report is refused before any POST, under a code
    the agent branches on, and the message says it was not sent."""
    monkeypatch.setattr(up_svc.httpx, "post", _explode)
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "")
    key = client.post("/api/api-keys", json={"name": "reporter"}, headers=auth).json()["plaintext"]

    err = _mcp_report_error(client, key)

    assert err["code"] == "upstream_feedback_unconfigured"
    assert "was not sent" in err["message"]
    assert "UPSTREAM_FEEDBACK_TOKEN" in err["message"]
    assert "will not help" in err.get("hint", "")


def test_the_rest_route_refuses_unsent_when_the_token_is_unset(client, auth, monkeypatch):
    monkeypatch.setattr(up_svc.httpx, "post", _explode)
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "")

    cfg = client.get("/api/reports/upstream", headers=auth).json()
    assert cfg["enabled"] is False and "UPSTREAM_FEEDBACK_TOKEN" in cfg["reason"]
    r = client.post("/api/reports/upstream", json={"title": "x"}, headers=auth)
    assert r.status_code == 400
    assert "upstream_feedback_unconfigured" in r.json()["detail"]


def test_token_required_can_be_declared_for_another_hosted_host(monkeypatch):
    """A hosted intake on a host we do not know is declared, not guessed."""
    _self_hosted_no_token(monkeypatch)
    assert up_svc.unconfigured_reason() is None
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token_required", True)
    assert "UPSTREAM_FEEDBACK_TOKEN" in up_svc.unconfigured_reason()
    # ...and the inference can be overridden the other way for the default host.
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_url",
                        "https://feedback.asc-me.dev/api/public/requests")
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token_required", False)
    assert up_svc.unconfigured_reason() is None


def test_health_names_the_missing_token_without_calling_the_tool(client, monkeypatch):
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "")
    got = client.get("/health").json()["upstream_feedback"]
    assert got["configured"] is False
    assert "UPSTREAM_FEEDBACK_TOKEN" in got["missing"]


def test_health_is_clean_and_never_prints_the_token_when_set(client, monkeypatch):
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "tok_secret_value_9")
    r = client.get("/health")
    assert r.json()["upstream_feedback"] == {"configured": True, "missing": None}
    assert "tok_secret_value_9" not in r.text


def test_the_refusal_never_carries_a_token_value(monkeypatch):
    """The refusal names the setting. It is only raised with the token unset, but a
    declared-required self-hosted host must not leak anything else either."""
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "")
    with pytest.raises(up_svc.UpstreamUnconfigured) as e:
        up_svc.submit_upstream(type_="bug", title="x")
    assert "tok_" not in str(e.value)


def test_startup_logs_the_missing_token(monkeypatch, caplog):
    """The gap is visible at boot, not only on the first lost report. The lifespan calls
    this; a blank URL (deliberate opt-out) stays quiet."""
    import logging

    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "")
    with caplog.at_level(logging.WARNING, logger="graphban.upstream"):
        assert up_svc.warn_if_unconfigured()
    assert any("UPSTREAM_FEEDBACK_TOKEN" in r.getMessage() for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_url", "")
    assert up_svc.warn_if_unconfigured() is None
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_url", SELF_HOSTED)
    monkeypatch.setattr(up_svc.settings, "upstream_feedback_token", "tok_secret_value_9")
    assert up_svc.warn_if_unconfigured() is None
    assert not any("tok_secret_value_9" in r.getMessage() for r in caplog.records)


def test_the_lifespan_runs_the_startup_check():
    import inspect

    from app import main

    assert "warn_if_unconfigured()" in inspect.getsource(main.lifespan)
