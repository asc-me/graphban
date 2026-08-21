"""The supervisor's reach, made a set with a test in front of it.

PRD-22 §4. The authority table is prose, and prose is what stops binding when somebody
adds a convenient call. These tests are about two things: that the set is what the PRD
says it is, and that there is no second door around it.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import httpx
import pytest

from gbfleet.client import (
    ALLOWED_TOOLS,
    Graphban,
    NotPermitted,
    ProtocolError,
    ServerUnreachable,
    ToolFailed,
)

FLEET_SRC = Path(__file__).resolve().parents[1] / "src"

#: Calls PRD-22 §4 names under "May not", plus the ones a tired implementer would
#: reach for. Listed separately from `ALLOWED_TOOLS` so this test fails if a name ever
#: appears in both — an allowlist checked against itself proves nothing.
FORBIDDEN = [
    "mint_enrolment",  # may not mint a seat
    "assign_role",  # may not assign a role
    "claim_next",  # may not claim work
    "claim_cluster",
    "claim_review",
    "sign_off",  # may not sign off
    "bounce",  # may not bounce
    "submit_verdict",
    "register_agent",  # the CHILD registers itself, on its own seat
    "heartbeat",  # likewise
    "update_item",
    "create_item",
    "release_item",
]


def _reply(result: dict, id_: int = 1, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"jsonrpc": "2.0", "id": id_, "result": result})


def _ok(payload: dict) -> httpx.Response:
    return _reply(
        {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload}
    )


def _server(handler) -> Graphban:
    return Graphban("http://graphban.invalid", "gbk_test", transport=httpx.MockTransport(handler))


# --- the set --------------------------------------------------------------------


def test_the_permitted_set_is_exactly_this():
    """Pinned exactly, not checked for membership.

    A test that only asserts `sign_off not in ALLOWED_TOOLS` passes for every widening
    nobody thought to name. This one fails on any change, which makes widening the
    supervisor's reach a deliberate edit with a reviewer attached.
    """
    assert ALLOWED_TOOLS == frozenset({"fleet_status", "propose_allocation"})


def test_nothing_the_prd_forbids_is_permitted():
    overlap = sorted(set(FORBIDDEN) & ALLOWED_TOOLS)
    assert not overlap, f"PRD-22 §4 says the supervisor may not call {overlap}"


@pytest.mark.parametrize("tool", FORBIDDEN)
def test_a_forbidden_call_is_refused_before_it_leaves_the_process(tool: str):
    def never(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"{tool} reached the network")

    with pytest.raises(NotPermitted) as exc:
        _server(never).call(tool, whatever=1)
    assert tool in str(exc.value)


def test_the_refusal_says_what_to_do_about_it():
    """A refusal a reader cannot act on gets worked around rather than considered."""
    with pytest.raises(NotPermitted) as exc:
        _server(lambda r: _ok({})).call("sign_off")
    message = str(exc.value)
    assert "ALLOWED_TOOLS" in message
    assert "fleet_status" in message and "propose_allocation" in message


# --- no second door ---------------------------------------------------------------


def test_only_one_function_in_the_package_makes_a_request():
    """The structural half of the guarantee.

    An allowlist is decoration if any other module can reach httpx directly, and that
    is a normal thing to do by accident — the supervisor already imports httpx, and a
    one-off `httpx.get` looks like the smallest possible change.
    """
    offenders: list[str] = []
    for path in sorted(FLEET_SRC.rglob("*.py")):
        if path.name == "client.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bhttpx\b|\brequests\b|\burllib\b|\bhttp\.client\b", line):
                offenders.append(f"{path.relative_to(FLEET_SRC)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "only gbfleet/client.py may talk to the network, so the allowlist has one door:\n"
        + "\n".join(offenders)
    )


def test_every_named_helper_goes_through_the_checked_call():
    """The other way round the allowlist: a helper that built its own request would be
    unchecked while looking exactly like the ones that are not."""
    source = (FLEET_SRC / "gbfleet" / "client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Graphban")

    helpers = [
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef)
        and not n.name.startswith("_")
        and n.name not in {"call", "close", "endpoint"}
    ]
    assert helpers, "no helpers found — this test would pass vacuously"

    for helper in helpers:
        calls = [
            n.func.attr
            for n in ast.walk(helper)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]
        assert "call" in calls, f"{helper.name} does not route through call()"
        assert "post" not in calls, f"{helper.name} makes its own request"


def test_the_helpers_cover_the_permitted_set():
    """If a permitted tool has no helper, call sites reach for `call` with a string —
    and a string is what the next widening will be spelled with."""
    named = {n for n in dir(Graphban) if not n.startswith("_")}
    assert ALLOWED_TOOLS <= named


# --- the wire -------------------------------------------------------------------


def test_a_permitted_call_reaches_the_server_in_the_expected_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-API-Key")
        seen["body"] = json.loads(request.content)
        return _ok({"agents": [], "idle": True})

    result = _server(handler).fleet_status(project_id="agentledger")

    assert result == {"agents": [], "idle": True}
    assert seen["url"] == "http://graphban.invalid/api/mcp"
    assert seen["key"] == "gbk_test"
    assert seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"] == {
        "name": "fleet_status",
        "arguments": {"project_id": "agentledger"},
    }


def test_a_tool_error_is_not_a_transport_failure():
    """D-a and D-i both turn on this distinction. `isError` means the server is fine
    and answered; treating it as unreachable would stop the supervisor spawning over a
    bad argument."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(
            {
                "content": [{"type": "text", "text": "no_such_project: nope"}],
                "structuredContent": {
                    "error": {"code": "no_such_project", "message": "nope", "hint": "check the id"}
                },
                "isError": True,
            }
        )

    with pytest.raises(ToolFailed) as exc:
        _server(handler).fleet_status()
    assert exc.value.code == "no_such_project"
    assert "check the id" in str(exc.value)
    assert not isinstance(exc.value, ServerUnreachable)


def test_a_connection_failure_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(ServerUnreachable):
        _server(handler).fleet_status()


def test_a_server_error_is_unreachable_and_a_client_error_is_not():
    """A 5xx is the server failing to answer — retrying is right and spawning is not.
    A 4xx is an answer: a bad credential does not become true by waiting, and treating
    it as a partition would make the supervisor sit quietly forever."""
    with pytest.raises(ServerUnreachable):
        _server(lambda r: httpx.Response(503, text="down")).fleet_status()

    with pytest.raises(ProtocolError) as exc:
        _server(lambda r: httpx.Response(401, text="invalid api key")).fleet_status()
    assert "401" in str(exc.value)


def test_a_reply_to_a_different_request_is_refused():
    """Cheap, and the failure it prevents is a supervisor acting on somebody else's
    answer — from a proxy, a cache, or a retry that overtook itself."""
    with pytest.raises(ProtocolError) as exc:
        _server(lambda r: _reply({"structuredContent": {}}, id_=999)).fleet_status()
    assert "999" in str(exc.value)


def test_a_jsonrpc_error_is_reported_as_one():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no such tool"}},
        )

    with pytest.raises(ProtocolError) as exc:
        _server(handler).fleet_status()
    assert "no such tool" in str(exc.value)


def test_a_reply_with_no_structured_content_is_refused_rather_than_guessed():
    """The text block is a JSON-in-a-string mirror kept for back-compat. Parsing it
    would work today and silently diverge the moment the two stop agreeing."""
    with pytest.raises(ProtocolError):
        _server(lambda r: _reply({"content": [{"type": "text", "text": '{"a": 1}'}]})).fleet_status()
