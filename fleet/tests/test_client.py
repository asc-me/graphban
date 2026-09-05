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


#: Every module permitted to open a socket, and the counterpart it may reach. Pinned by exact
#: equality below, so a third door fails a test rather than passing review.
#:
#: There are two because the distribution holds two programs. The supervisor talks to Graphban
#: holding a credential with authority, and every such call is checked against an allowlist.
#: `gbagent` (PRD-24 S3) talks to a model endpoint, which has authority over nothing: it cannot
#: claim work, sign off, or move an item. When `gbagent` needs Graphban it goes through
#: `client.py` like everything else, with a worker's allowlist rather than the supervisor's.
NETWORK_DOORS = {
    "gbfleet/client.py": "Graphban, through the checked allowlist",
    "gbagent/llm.py": "a local model endpoint, which holds no authority",
}


def test_only_the_named_doors_make_requests():
    """The structural half of the guarantee.

    An allowlist is decoration if any other module can reach httpx directly, and that
    is a normal thing to do by accident — the supervisor already imports httpx, and a
    one-off `httpx.get` looks like the smallest possible change.
    """
    sources = sorted(FLEET_SRC.rglob("*.py"))
    # THE CONTROL, borrowed from `test_packaging.py`, which already does this correctly:
    # "no python sources under {FLEET_SRC} — this guard scanned nothing". This sweep did not,
    # and it is the one that decides which modules may reach the network at all. Measured:
    # pointing the walk at a directory with no sources left this file passing 31 tests, so an
    # allowlist guarded by nothing read exactly like an allowlist that held.
    assert sources, (
        f"no python sources under {FLEET_SRC} — this guard scanned nothing, and an egress "
        "allowlist that examined no modules cannot have found a violation")

    offenders: list[str] = []
    for path in sources:
        if path.relative_to(FLEET_SRC).as_posix() in NETWORK_DOORS:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bhttpx\b|\brequests\b|\burllib\b|\bhttp\.client\b", line):
                offenders.append(f"{path.relative_to(FLEET_SRC)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "only these modules may talk to the network:\n"
        + "\n".join(f"  {k} -> {v}" for k, v in NETWORK_DOORS.items())
        + "\n\noffenders:\n"
        + "\n".join(offenders)
    )


def test_the_set_of_doors_is_exactly_two():
    """`not in NETWORK_DOORS` passes for every widening of NETWORK_DOORS, so the set itself is
    pinned. Adding a door should be an edit somebody has to explain, which is the same argument
    `ALLOWED_TOOLS` is pinned by exact equality above."""
    assert set(NETWORK_DOORS) == {"gbfleet/client.py", "gbagent/llm.py"}
    for door in NETWORK_DOORS:
        assert (FLEET_SRC / door).is_file(), f"{door} is named as a door but does not exist"


def test_the_model_door_cannot_reach_graphban():
    """What keeps the second door narrow.

    The Graphban allowlist would be worth nothing if the module exempted from the httpx guard
    could also post to `/api/mcp`. It talks to a model endpoint and nothing else, and this is
    the assertion rather than the docstring that says so.
    """
    source = (FLEET_SRC / "gbagent" / "llm.py").read_text(encoding="utf-8").lower()

    for forbidden in ("api/mcp", "x-api-key", "tools/call", "graphban.call"):
        assert forbidden not in source, (
            f"gbagent/llm.py mentions {forbidden!r} — the model door must not reach Graphban"
        )


#: Public methods that legitimately do NOT route through the allowlist-checked `call`.
#: Pinned by exact equality, so a second one is an edit somebody has to justify.
#:
#: `list_tools` fetches `tools/list`, and **listing is not calling**. The allowlist governs
#: what a credential may DO; the manifest is the same document every MCP client fetches at
#: connect, before it has a role at all (PRD-17 D-b — the manifest is not trimmed by role,
#: the call gate refuses instead). Routing it through `call` would mean putting a
#: pseudo-tool name in every allowlist to permit reading a public document.
#: `post_attempt` (PRD-38 D3) posts telemetry to a REST route, which is not a tool and has no
#: entry in any tool allowlist to route through. It is exempt from THIS check and governed by
#: its own: `ALLOWED_PATHS`, pinned below, and the `_post` helper that refuses anything else.
#: An exemption with no replacement gate would be the second door this file exists to prevent.
NOT_TOOL_CALLS = {"list_tools", "post_attempt"}


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
        and n.name not in {"call", "close", "endpoint"} | NOT_TOOL_CALLS
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


def test_the_exemptions_from_the_allowlist_are_exactly_two():
    """`not in NOT_TOOL_CALLS` passes for every widening of NOT_TOOL_CALLS, so the set is
    pinned. The next method that skips the check should be an edit with a reason attached."""
    assert NOT_TOOL_CALLS == {"list_tools", "post_attempt"}


def test_the_rest_surface_is_one_path_and_every_post_is_checked_against_it():
    """The replacement gate for `post_attempt`'s exemption.

    A credential with authority now reaches two surfaces on one connection pool. The tool
    allowlist governs `/api/mcp` and says nothing about anything else, so the paths get an
    allowlist of their own — pinned by exact equality here for the same reason the tool set
    is, and enforced in `_post` rather than trusted to callers.
    """
    from gbfleet.client import ALLOWED_PATHS, ATTEMPTS_PATH

    assert ALLOWED_PATHS == {"/api/fleet/attempts"}
    assert ATTEMPTS_PATH in ALLOWED_PATHS

    source = (FLEET_SRC / "gbfleet" / "client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Graphban")
    posts = [n for n in cls.body if isinstance(n, ast.FunctionDef)
             and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                     and c.func.attr == "post" for c in ast.walk(n))]
    # `_rpc` is the MCP endpoint, governed by `ALLOWED_TOOLS`; `_post` is everything else,
    # governed by `ALLOWED_PATHS`. Two POSTing methods, two gates, and no third.
    assert sorted(n.name for n in posts) == ["_post", "_rpc"], (
        "every method that issues an httpx POST must be one of the two gated ones")

    server = _server(lambda r: httpx.Response(200, json={"ok": True}))
    with pytest.raises(NotPermitted):
        server._post("/api/items", {}, timeout=1.0)


def test_a_telemetry_post_that_cannot_land_returns_none_rather_than_raising():
    """D3: the supervisor posts this beside real work, twice per child. A measurement that
    could fail a spawn would be a worse bargain than having no measurement."""
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    assert _server(refuse).post_attempt(enrolment_code="ENR-1", winner="gbagent:") is None

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    assert _server(unreachable).post_attempt(enrolment_code="ENR-1") is None


def test_a_telemetry_post_sends_only_what_it_knows():
    """A null is not a value here. Sending `turns_used: None` would have the server store a
    null over something a previous post got right — which its merge rule forbids, but the
    client should not be asking."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "at_1"})

    reply = _server(handler).post_attempt(enrolment_id="enr_1", turns_used=None,
                                          wall_seconds=12, adapter="gbagent")
    assert reply == {"id": "at_1"}
    assert seen["path"] == "/api/fleet/attempts"
    assert seen["body"] == {"enrolment_id": "enr_1", "wall_seconds": 12, "adapter": "gbagent"}


def test_listing_the_manifest_cannot_invoke_anything():
    """The reason the exemption is safe. `tools/list` takes no tool name, so there is no
    argument through which it could become a call — this asserts that rather than trusting
    the method name."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": seen["id"],
                                         "result": {"tools": [{"name": "search_code"}]}})

    tools = _server(handler).list_tools()

    assert seen["method"] == "tools/list"
    assert seen["params"] == {}, "no name, no arguments, nothing to invoke"
    assert [t["name"] for t in tools] == ["search_code"]


def test_a_manifest_reply_without_tools_is_empty_rather_than_a_crash():
    """A server that answers something unexpected must not take down a run before it starts;
    `orient.build` is what decides an empty manifest is fatal, and it says so by name."""
    empty = _server(lambda r: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "result": {}}))

    assert empty.list_tools() == []


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
