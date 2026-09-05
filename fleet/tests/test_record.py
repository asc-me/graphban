"""P30 D10 — a writer with standing unions measured paths; empty is not a write.

The supervisor measures and reports. This module is what `until` (planner) and
`gbagent` call so the next partition sees what this reap actually changed.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gbagent.coord import WORKER_TOOLS
from gbfleet.client import ALLOWED_TOOLS, Graphban
from gbfleet.record import measured

from conftest import telemetry_ack  # noqa: E402


def _mcp(payload, *, id_=1):
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": id_,
              "result": {"structuredContent": payload}},
    )


def test_measured_sends_this_reaps_paths_only():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        sent.append(body["params"])
        return _mcp({"id": "GRPH-1", "touchpoints": ["a.py"]}, id_=body["id"])

    client = Graphban("http://graphban.invalid", "gbk_planner", allowed=WORKER_TOOLS,
                      transport=httpx.MockTransport(handler))
    measured(client, "GRPH-1", ["a.py"])

    assert sent[0]["name"] == "update_item"
    assert sent[0]["arguments"] == {"id": "GRPH-1", "touchpoints": ["a.py"]}


def test_empty_is_not_a_write():
    """The absence that reads as clean: sending `[]` would wipe declared paths."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _mcp({}, id_=1)

    client = Graphban("http://graphban.invalid", "gbk_planner", allowed=WORKER_TOOLS,
                      transport=httpx.MockTransport(handler))
    assert measured(client, "GRPH-1", []) is None
    assert measured(client, "GRPH-1", None) is None
    assert measured(client, "", ["a.py"]) is None
    assert sent == []


def test_blank_and_duplicate_paths_are_not_sent():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        sent.append(body["params"]["arguments"])
        return _mcp({}, id_=body["id"])

    client = Graphban("http://graphban.invalid", "gbk_planner", allowed=WORKER_TOOLS,
                      transport=httpx.MockTransport(handler))
    measured(client, "GRPH-1", ["  a.py  ", "a.py", "", "b.py"])

    assert sent[0]["touchpoints"] == ["a.py", "b.py"]


def test_the_supervisor_allowlist_still_refuses_the_write():
    """The helper existing is not a widening. A supervisor client must still bounce."""
    from gbfleet.client import NotPermitted

    client = Graphban("http://graphban.invalid", "gbk_sup",
                      transport=httpx.MockTransport(lambda r: _mcp({})))
    assert "update_item" not in ALLOWED_TOOLS
    with pytest.raises(NotPermitted):
        measured(client, "GRPH-1", ["a.py"])


def test_the_supervisor_module_does_not_import_the_writer():
    import gbfleet.supervisor as sup
    source = Path(sup.__file__).read_text(encoding="utf-8")
    assert "from .record import" not in source
    assert "from gbfleet.record import" not in source
    assert "from . import record" not in source
