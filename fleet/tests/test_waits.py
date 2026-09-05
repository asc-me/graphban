"""P30 D11 — typed waits, not a free-text blocker and not a second board."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gbagent.coord import WORKER_TOOLS, Coordinator
from gbagent.orient import COORDINATION_TOOLS
from gbfleet.client import ALLOWED_TOOLS, Graphban, NotPermitted
from gbfleet.waits import WAIT_TAGS, ids as wait_ids

from conftest import telemetry_ack  # noqa: E402


def _mcp(payload, *, id_=1):
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": id_,
              "result": {"structuredContent": payload}},
    )


def _graphban(handler, allowed=WORKER_TOOLS) -> Graphban:
    return Graphban("http://graphban.invalid", "gbk_seat", allowed=allowed,
                    transport=httpx.MockTransport(handler))


def test_create_item_and_link_items_are_worker_writes():
    assert "create_item" in COORDINATION_TOOLS
    assert "link_items" in COORDINATION_TOOLS
    assert "create_item" in WORKER_TOOLS
    assert "link_items" in WORKER_TOOLS
    assert "create_item" not in ALLOWED_TOOLS


def test_file_wait_blocks_the_original_and_does_not_send_it_to_review():
    """SABOTAGE. The helper must not `update_item(status=review)` because it filed a wait."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        sent.append(body["params"])
        name = body["params"]["name"]
        if name == "create_item":
            return _mcp({"id": "GRPH-W1", "tags": ["wait:merge"], "status": "blocked"},
                        id_=body["id"])
        if name == "link_items":
            return _mcp({"id": "lnk", "a": "GRPH-1", "b": "GRPH-W1", "type": "dependency"},
                        id_=body["id"])
        return _mcp({"id": "GRPH-1", "status": "blocked"}, id_=body["id"])

    out = Coordinator(client=_graphban(handler), item_id="GRPH-1").file_wait("merge")

    names = [p["name"] for p in sent]
    assert names == ["create_item", "link_items", "update_item"]
    assert sent[0]["arguments"]["tags"] == ["wait:merge"]
    assert sent[0]["arguments"]["status"] == "blocked"
    assert sent[1]["arguments"]["type"] == "dependency"
    assert sent[2]["arguments"]["status"] == "blocked"
    assert sent[2]["arguments"]["id"] == "GRPH-1"
    assert out["status"] == "blocked"
    assert all(p["arguments"].get("status") != "review" for p in sent)


def test_file_wait_refuses_free_text():
    with pytest.raises(ValueError, match="not a type"):
        Coordinator(client=_graphban(lambda r: _mcp({})), item_id="GRPH-1").file_wait(
            "please look")


def test_on_self_tags_the_original_and_blocks_it():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        sent.append(body["params"])
        name = body["params"]["name"]
        if name == "get_item_details":
            return _mcp({"id": "GRPH-1", "tags": ["prd"]}, id_=body["id"])
        return _mcp({"id": "GRPH-1", "status": "blocked",
                     "tags": ["prd", "wait:decision"]}, id_=body["id"])

    Coordinator(client=_graphban(handler), item_id="GRPH-1").file_wait(
        "decision", on_self=True)

    names = [p["name"] for p in sent]
    assert names == ["get_item_details", "update_item"]
    assert sent[1]["arguments"]["status"] == "blocked"
    assert "wait:decision" in sent[1]["arguments"]["tags"]
    assert "prd" in sent[1]["arguments"]["tags"]


def test_wait_ids_come_from_search_not_from_blocker_text():
    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        tag = (body["params"]["arguments"].get("tags") or [""])[0]
        rows = [{"id": f"GRPH-{tag.split(':')[-1]}"}] if tag == "wait:merge" else []
        return _mcp({"results": rows}, id_=body["id"])

    # Planner-shaped allowlist: search_items is a read, not a supervisor tool.
    client = Graphban("http://graphban.invalid", "gbk_planner",
                      allowed=frozenset({"search_items"}),
                      transport=httpx.MockTransport(handler))
    assert wait_ids(client) == ["GRPH-merge"]


def test_the_supervisor_cannot_search_or_create_waits():
    client = Graphban("http://graphban.invalid", "gbk_sup",
                      transport=httpx.MockTransport(lambda r: _mcp({})))
    with pytest.raises(NotPermitted):
        wait_ids(client)
    with pytest.raises(NotPermitted):
        client.call("create_item", title="x")


def test_the_supervisor_module_does_not_import_the_finder():
    import gbfleet.supervisor as sup
    source = Path(sup.__file__).read_text(encoding="utf-8")
    assert "from .waits import" not in source
    assert "from gbfleet.waits import" not in source
    assert "from . import waits" not in source


def test_wait_tags_are_the_five_named_in_the_prd():
    assert WAIT_TAGS == (
        "wait:merge", "wait:decision", "wait:secret", "wait:access", "wait:deploy",
    )
