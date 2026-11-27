"""GRPH-1977: the scripting surface — `prd-coverage`, `fleet-status`, `decompose --preview`.

Each is a thin wrapper over the MCP tool of the same name. The tests stand a MockTransport in
for the server, so what is asserted is the request that LEFT the process, not a mocked helper.
"""
from __future__ import annotations

import json

import httpx
import pytest

from gbfleet import cli
from gbfleet.client import Graphban

COMMON = ["--server", "http://graphban.invalid", "--project", "agentledger"]


class Server:
    def __init__(self, payload=None, error=None):
        self.calls: list[dict] = []
        self.payload = payload if payload is not None else {"ok": True}
        self.error = error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body["params"])
        if self.error:
            result = {"isError": True, "content": [{"type": "text", "text": "boom"}],
                      "structuredContent": {"error": self.error}}
        else:
            result = {"content": [{"type": "text", "text": "{}"}],
                      "structuredContent": self.payload}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def server(monkeypatch):
    srv = Server()
    monkeypatch.setenv("GBFLEET_API_KEY", "gbk_test")
    real = Graphban

    def make(base, key, **kw):
        return real(base, key, transport=httpx.MockTransport(srv), **kw)

    monkeypatch.setattr(cli, "Graphban", make)
    return srv


CASES = [
    (["prd-coverage", "GRPH-P96"], "prd_coverage", {"prd_id": "GRPH-P96"}),
    (["fleet-status"], "fleet_status", {}),
    (["decompose", "GRPH-P96", "--preview"], "decompose_prd",
     {"prd_id": "GRPH-P96", "create": False}),
]


@pytest.mark.parametrize("argv,tool,args", CASES)
def test_json_prints_the_tools_result_and_calls_the_tool(server, capsys, argv, tool, args):
    server.payload = {"shaped": True, "gaps": ["s1"]}
    assert cli.main(argv + ["--json"] + COMMON) == 0
    assert json.loads(capsys.readouterr().out) == {"shaped": True, "gaps": ["s1"]}
    assert len(server.calls) == 1 and server.calls[0]["name"] == tool
    sent = server.calls[0]["arguments"]
    assert {k: v for k, v in sent.items() if k != "project_id"} == args
    assert sent["project_id"] == "agentledger"


@pytest.mark.parametrize("argv,tool,args", CASES)
def test_without_json_a_readable_summary_is_printed(server, capsys, argv, tool, args):
    server.payload = {"shaped": False, "gaps": ["section-one"]}
    assert cli.main(argv + COMMON) == 0
    out = capsys.readouterr().out
    assert "shaped: False" in out and "- section-one" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


@pytest.mark.parametrize("argv,tool,args", CASES)
def test_a_failed_call_exits_nonzero_with_the_code_and_hint(server, capsys, argv, tool, args):
    server.error = {"code": "not_approved", "message": "needs grill", "hint": "answer the grill"}
    assert cli.main(argv + ["--json"] + COMMON) == 1
    err = capsys.readouterr().err
    assert "not_approved" in err and "answer the grill" in err


def test_decompose_without_preview_is_refused_and_files_nothing(server, capsys):
    assert cli.main(["decompose", "GRPH-P96"] + COMMON) != 0
    assert "--preview" in capsys.readouterr().err
    assert server.calls == []


def test_decompose_never_sends_create_true(server):
    cli.main(["decompose", "GRPH-P96", "--preview", "--json"] + COMMON)
    assert server.calls[0]["arguments"]["create"] is False


def test_the_scripting_client_cannot_reach_a_decision(server):
    assert cli.SCRIPTING_READS == {"prd_coverage", "fleet_status", "decompose_prd"}


def test_help_lists_nothing_that_answers_a_grill_signs_off_or_approves(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    listing = capsys.readouterr().out.lower()
    for word in ("grill", "sign-off", "sign_off", "signoff", "approve"):
        assert word not in listing, word
    for command in ("prd-coverage", "fleet-status", "decompose"):
        assert command in listing
    sub = cli.build_parser()._subparsers._group_actions[0].choices
    for name in sub:
        assert not any(w in name for w in ("grill", "sign", "approve", "answer")), name
