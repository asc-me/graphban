"""GRPH-997 — the build servers the seat grants, which gbagent used to drop on the floor.

GRPH-816 made `--mcp-server context7` a grant: the stanza is copied into the seat file, and
`--strict-mcp-config` means the file is the whole list of what a child can reach. Every vendor
child then reached it, because a vendor harness is an MCP client. `read_seat` kept the
`graphban` entry and returned two strings, so this agent got the credential and none of the
tools — and gbagent is the one harness with no shell and no web, i.e. the one for which a docs
server and a browser are not things it can fake with `curl`.

Three properties are under test, and the third is the one a green layer would still miss:

**The grant becomes tools**, advertised with the SERVER's own schema, reached at the server's
own url with the operator's own headers.

**Every failure is a result.** A server that will not connect, one that answers and lists
nothing, a call it refuses, a call that dies mid-run: each comes back as text a model can read,
and none of them ends the run. "Listed no tools" and "would not connect" are asserted to be
DIFFERENT sentences, because the reassuring reading of both is "there is no docs server here".

**The call site is wired.** A layer with thorough tests that `cli._run` never constructs is the
defect this repository keeps finding (GRPH-534, GRPH-247), so the production site is pinned by
source and the seat the supervisor writes is read back end to end.
"""
from __future__ import annotations

import ast
import inspect
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from gbagent import buildtools, cli, orient
from gbagent.buildtools import PREFIX, UNUSABLE, BuildTools
from gbagent.config import VerifyConfig
from gbagent.llm import ToolCall, ToolSpec
from gbagent.orient import ORIENTATION_TOOLS
from gbagent.toolset import Toolset
from gbfleet.client import Graphban, McpServer
from gbfleet.seat import Seat
from conftest import make_stub_script, stub_argv  # noqa: E402

DOCS = {"name": "resolve-library-id", "description": "Find the library you meant.",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}},
                        "required": ["q"]}}
BROWSER = {"name": "navigate", "description": "Open a page.",
           "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}}


class FakeServer:
    """The granted servers, as one MockTransport handler.

    Answers by request HOST rather than by a single endpoint on purpose: a grant of two
    servers that both answered from one url would pass every assertion about tools and hide
    the fact that the second server was never reached at all.
    """

    def __init__(self):
        self.tools: dict[str, list[dict]] = {}
        #: Hosts that refuse the connection outright.
        self.down: set[str] = set()
        #: Hosts whose `tools/call` answers `isError`.
        self.refusing: set[str] = set()
        #: Hosts that answer `text/event-stream` instead of JSON.
        self.sse: set[str] = set()
        #: Hosts that reject the handshake, i.e. stateless servers.
        self.no_handshake: set[str] = set()
        #: Content blocks a host answers `tools/call` with, instead of the default echo.
        self.blocks: dict[str, list[dict]] = {}
        #: Everything that arrived: (host, method, tool, session header).
        self.seen: list[tuple] = []

    def grant(self, host: str, tools: list[dict]) -> dict:
        """The seat stanza for one server, and remember what it should answer."""
        self.tools[host] = tools
        return {"type": "http", "url": f"https://{host}/mcp", "headers": {"k": f"secret-{host}"}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        body = json.loads(request.content)
        method = body.get("method")
        params = body.get("params") or {}
        self.seen.append((host, method, params.get("name"),
                          request.headers.get("mcp-session-id"), request.headers.get("k")))
        if host in self.down:
            raise httpx.ConnectError("no route to host")
        if method == "initialize":
            if host in self.no_handshake:
                return self._json(body, None, error={"code": -32601, "message": "Method not found"})
            return httpx.Response(200, headers={"Mcp-Session-Id": f"sid-{host}"},
                                  json={"jsonrpc": "2.0", "id": body["id"], "result": {
                                      "protocolVersion": "2025-06-18",
                                      "capabilities": {"tools": {}},
                                      "serverInfo": {"name": host, "version": "1"}}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return self._reply(body, {"tools": self.tools.get(host, [])}, host)
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if host in self.refusing:
                return self._reply(body, {"content": [{"type": "text",
                                                      "text": f"{name} is rate limited"}],
                                          "isError": True}, host)
            blocks = self.blocks.get(host) or [{"type": "text", "text":
                                                f"{host}:{name} for {arguments.get('q', '?')}"}]
            return self._reply(body, {"content": blocks}, host)
        return self._json(body, None, error={"code": -32601, "message": f"unknown {method}"})

    def _reply(self, body: dict, result: dict, host: str) -> httpx.Response:
        if host in self.sse:
            frame = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=f"event: message\ndata: {frame}\n\n")
        return self._json(body, result)

    @staticmethod
    def _json(body: dict, result: dict | None, error: dict | None = None) -> httpx.Response:
        payload: dict = {"jsonrpc": "2.0", "id": body.get("id")}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        return httpx.Response(200, json=payload)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _build(fake: FakeServer, shared: dict) -> BuildTools:
    return buildtools.build(shared, transport=fake.transport())


@pytest.fixture()
def docs() -> tuple[FakeServer, BuildTools]:
    fake = FakeServer()
    return fake, _build(fake, {"context7": fake.grant("mcp.context7.test", [DOCS])})


@pytest.fixture()
def wt(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "backend").mkdir(parents=True)
    (root / "README.md").write_text("# repo\n")
    return root


def _toolset(root: Path, build_servers=None, orientation=None) -> Toolset:
    runner = make_stub_script(root / "backend" / "r.py", prints=("1 passed in 1.0s",))
    return Toolset(root=root, build_servers=build_servers, orientation=orientation,
                   cfg=VerifyConfig(argv=stub_argv(runner), cwd=root / "backend", source="r.py"))


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, input=arguments)


# ---- the grant becomes tools ----------------------------------------------------------


def test_a_granted_tool_is_advertised_under_its_servers_name(docs):
    _, layer = docs

    assert [spec.name for spec in layer.specs] == [f"{PREFIX}context7{buildtools.SEPARATOR}"
                                                   "resolve-library-id"]


def test_the_schema_is_the_servers_and_not_a_copy(docs):
    """The reason `orient` fetches a manifest rather than declaring one: a copy of somebody
    else's contract goes stale quietly and is found as a model calling with arguments nobody
    accepts, at thirty seconds a turn."""
    _, layer = docs

    spec = layer.specs[0]

    assert spec.description == DOCS["description"]
    assert spec.input_schema == DOCS["inputSchema"]


def test_two_grants_are_each_reached_at_their_own_url_with_their_own_headers():
    """THE CALL, per server. One mock answering every host would have passed this file's other
    assertions while the second server's url was never used at all."""
    fake = FakeServer()
    shared = {"context7": fake.grant("mcp.context7.test", [DOCS]),
              "browser": fake.grant("browser.test", [BROWSER])}

    layer = _build(fake, shared)

    assert sorted(spec.name for spec in layer.specs) == [
        f"{PREFIX}browser{buildtools.SEPARATOR}navigate",
        f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id",
    ]
    listed = {(host, header) for host, method, _, _, header in fake.seen
              if method == "tools/list"}
    assert listed == {("mcp.context7.test", "secret-mcp.context7.test"),
                      ("browser.test", "secret-browser.test")}, (
        "each server must be listed at its own url with its own credential")


def test_the_ledgers_own_entry_is_not_a_build_grant():
    """`graphban` is the credential this child registered with, reached through `coord` on its
    own key. Advertising it as a build tool would give the model a second, unmonitored path to
    the ledger — and `gbfleet` is the supervisor's local server, which is not a build tool
    either. Both are `mcpshare.RESERVED`, so the rule has one definition."""
    fake = FakeServer()
    shared = {"graphban": {"type": "http", "url": "https://gb.test/api/mcp",
                           "headers": {"X-API-Key": "gbk_seat"}},
              "gbfleet": {"command": "gbfleet", "args": ["mcp"]},
              "context7": fake.grant("mcp.context7.test", [DOCS])}

    layer = _build(fake, shared)

    assert [g.name for g in layer.granted] == ["context7"]
    assert not any(name.startswith(f"{PREFIX}graphban") for name in layer.names)
    assert not any("gbfleet" in name for name in layer.names)
    assert not any(host == "gb.test" for host, *_ in fake.seen), (
        "the ledger's own entry must not be dialed as a build server")


def test_nothing_granted_is_an_empty_layer_that_says_nothing():
    """The default, and the one every seat so far has had. An empty grant must not produce a
    paragraph of prose in the task text or a placeholder tool in the list."""
    layer = buildtools.build({})

    assert layer.granted == [] and layer.specs == []
    assert layer.instruction() == ""
    assert layer.one_line() == ""
    assert layer.handles(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id") is False


def test_a_stdio_stanza_is_refused_rather_than_launched():
    """A `command` stanza is a program to run. This agent's whole safety story is that it has
    no shell, and running a command read out of a config file is how that stops being true."""
    layer = buildtools.build({"browser-use": {"command": "npx", "args": ["browser-use"]}})

    reason = layer.granted[0].reason
    assert "no url" in reason and "no shell" in reason
    assert layer.granted[0].server is None, "nothing was dialed and nothing was spawned"


def test_a_non_http_url_is_said_to_be_unreachable_rather_than_dropped():
    layer = buildtools.build({"weird": {"url": "file:///etc/passwd"}})

    assert "not http" in layer.granted[0].reason
    assert layer.granted[0].server is None


# ---- every failure is a result --------------------------------------------------------


def test_a_server_that_will_not_connect_is_reported_and_the_run_continues():
    fake = FakeServer()
    fake.down.add("mcp.context7.test")
    layer = _build(fake, {"context7": fake.grant("mcp.context7.test", [DOCS])})

    result = layer.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id",
                                 q="httpx"))

    assert result.is_error
    assert "context7" in result.content and "tools/list" in result.content


def test_a_server_that_lists_no_tools_is_not_reported_as_one_that_is_down():
    """Two absences, two sentences. Collapsing them is the failure mode this repository keeps
    naming: the reader learns only that there are no tools, and not whether the grant was
    empty or the server was gone."""
    empty = FakeServer()
    empty_layer = _build(empty, {"context7": empty.grant("mcp.context7.test", [])})
    down = FakeServer()
    down.down.add("mcp.context7.test")
    down_layer = _build(down, {"context7": down.grant("mcp.context7.test", [DOCS])})

    assert "listed no tools" in empty_layer.granted[0].reason
    assert "did not answer" in down_layer.granted[0].reason
    assert empty_layer.granted[0].reason != down_layer.granted[0].reason


def test_a_manifest_of_nameless_entries_is_said_to_be_one():
    """A server that answered with three entries and no names is not a server that answered
    with nothing, and the count is what tells an operator which they have."""
    fake = FakeServer()
    layer = _build(fake, {"context7": fake.grant("mcp.context7.test",
                                                 [{"description": "no name field"}])})

    assert "1 tool(s), none with a usable name" in layer.granted[0].reason


def test_an_unusable_grant_still_occupies_a_line_in_the_tool_list(docs):
    """The advertisement half of "it must not look like the tool does not exist": a model
    reading only the tool list still learns the server was granted and why it is silent."""
    fake = FakeServer()
    fake.down.add("mcp.context7.test")
    layer = _build(fake, {"context7": fake.grant("mcp.context7.test", [DOCS])})

    spec = layer.specs[0]

    assert spec.name == f"{PREFIX}context7{buildtools.SEPARATOR}{UNUSABLE}"
    assert "granted" in spec.description and "not callable" in spec.description
    assert "no route to host" in spec.description


def test_a_call_the_server_refuses_comes_back_as_its_own_sentence():
    """`isError` is data here, not an exception: a server with no authority over the ledger
    declining a call is something a model can read and route around."""
    fake = FakeServer()
    fake.refusing.add("mcp.context7.test")
    layer = _build(fake, {"context7": fake.grant("mcp.context7.test", [DOCS])})

    result = layer.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id",
                                 q="httpx"))

    assert result.is_error
    assert "rate limited" in result.content


def test_a_server_that_dies_after_listing_costs_a_turn_and_not_the_run():
    fake = FakeServer()
    layer = _build(fake, {"context7": fake.grant("mcp.context7.test", [DOCS])})
    fake.down.add("mcp.context7.test")

    result = layer.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id",
                                 q="httpx"))

    assert result.is_error and "no route to host" in result.content


def test_a_successful_call_returns_the_servers_text(docs):
    fake, layer = docs

    result = layer.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id",
                                 q="httpx"))

    assert not result.is_error
    assert "mcp.context7.test:resolve-library-id for httpx" in result.content
    called = [(host, tool) for host, method, tool, _, _ in fake.seen if method == "tools/call"]
    assert called == [("mcp.context7.test", "resolve-library-id")]


def test_a_non_text_block_is_named_rather_than_dropped():
    """An image silently omitted reads as a tool that returned nothing. This agent reads text
    only, so the sentence has to say which of the two happened."""
    fake = FakeServer()
    fake.blocks["browser.test"] = [{"type": "image", "data": "AAAA"},
                                   {"type": "text", "text": "the page loaded"}]
    layer = _build(fake, {"browser": fake.grant("browser.test", [BROWSER])})

    result = layer.execute(_call(f"{PREFIX}browser{buildtools.SEPARATOR}navigate", url="x"))

    assert "image block not shown" in result.content
    assert "the page loaded" in result.content


def test_a_huge_result_is_truncated_where_the_graph_layer_truncates():
    """A browser page is bigger than a code map, and the bound is the same one `orient` already
    pays for: compaction only helps AFTER the window is full."""
    fake = FakeServer()
    fake.blocks["mcp.context7.test"] = [{"type": "text",
                                         "text": "x" * (orient.MAX_RESULT_CHARS + 5000)}]
    layer = _build(fake, {"context7": fake.grant("mcp.context7.test", [DOCS])})

    result = layer.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id"))

    assert result.content.endswith("chars")
    assert len(result.content) < orient.MAX_RESULT_CHARS + 200


def test_an_unknown_tool_under_a_granted_server_is_answered_by_that_server(docs):
    """Not the toolset's "no tool named X". A granted server owns every name under it, so a
    typo gets the server's own list back instead of a sentence that says the capability is
    absent."""
    _, layer = docs

    result = layer.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-librarie"))

    assert result.is_error
    assert "resolve-library-id" in result.content
    assert "no tool named" not in result.content


def test_a_call_under_a_server_that_never_connected_does_not_read_as_no_such_tool():
    fake = FakeServer()
    fake.down.add("mcp.context7.test")
    layer = _build(fake, {"context7": fake.grant("mcp.context7.test", [DOCS])})

    assert layer.handles(f"{PREFIX}context7{buildtools.SEPARATOR}anything-at-all")
    result = layer.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}anything-at-all"))

    assert result.is_error
    assert "no tool named" not in result.content
    assert "context7" in result.content


def test_a_name_under_a_server_nobody_granted_is_not_this_layers_business(docs):
    """The boundary in the other direction: `handles` is wide over GRANTED servers and nothing
    else, so a model inventing `mcp__gmail__send` gets the ordinary unknown-tool refusal
    rather than an answer from a layer that has no such server."""
    _, layer = docs

    assert layer.handles(f"{PREFIX}gmail{buildtools.SEPARATOR}send") is False
    assert layer.handles("read_file") is False
    assert layer.handles(PREFIX) is False
    assert layer.handles("") is False


def test_a_tool_name_containing_the_separator_is_not_mis_split():
    """`partition` on the FIRST separator: a server named `browser-use` answering a tool called
    `page__snapshot` must round-trip, or the call is dispatched to a server that does not
    exist."""
    fake = FakeServer()
    tool = {"name": "page__snapshot", "description": "d", "inputSchema": {"type": "object"}}
    layer = _build(fake, {"browser-use": fake.grant("browser-use.test", [tool])})

    name = layer.specs[0].name
    assert name == f"{PREFIX}browser-use{buildtools.SEPARATOR}page__snapshot"
    assert layer.handles(name)
    assert not layer.execute(_call(name)).is_error


# ---- what the model is told -----------------------------------------------------------


def test_the_instruction_names_a_live_grant_and_a_dead_one_differently():
    fake = FakeServer()
    fake.down.add("browser.test")
    shared = {"context7": fake.grant("mcp.context7.test", [DOCS, BROWSER]),
              "browser": fake.grant("browser.test", [])}

    text = _build(fake, shared).instruction()

    assert "context7" in text and "2 tool(s)" in text
    assert "NOT callable" in text and "browser —" in text
    assert "not a reason to stop" in text


def test_a_grant_with_no_tools_is_still_named_to_the_model():
    """The absence rule applied to the prompt: "no tools advertised" on its own reads as
    "there is no docs server here", which is the reassuring and wrong reading."""
    fake = FakeServer()
    layer = _build(fake, {"context7": fake.grant("mcp.context7.test", [])})

    assert "context7" in layer.instruction()
    assert "listed no tools" in layer.instruction()


def test_the_trace_line_says_what_the_grant_turned_into():
    fake = FakeServer()
    fake.down.add("browser.test")
    shared = {"context7": fake.grant("mcp.context7.test", [DOCS]),
              "browser": fake.grant("browser.test", [])}

    line = _build(fake, shared).one_line()

    assert "context7: 1 tool(s)" in line and "browser:" in line


# ---- the toolset dispatches it --------------------------------------------------------


def test_the_toolset_routes_a_granted_call_to_the_server_and_a_file_call_to_disk(wt, docs):
    """THE CALL. `BuildTools.execute` can be perfect and never receive anything: this is the
    dispatch that decides whether the layer exists at runtime."""
    _, layer = docs
    toolset = _toolset(wt, build_servers=layer)

    granted = toolset.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}"
                                    "resolve-library-id", q="httpx"))
    local = toolset.execute(_call("read_file", path="README.md"))

    assert "resolve-library-id for httpx" in granted.content
    assert "# repo" in local.content
    assert layer.calls == 1, "the filesystem read went to the server, or the call did not"


def test_granted_tools_are_advertised_after_the_graph_and_before_the_filesystem(wt):
    """Order is the cheapest nudge available (D1's own argument): the graph is what this agent
    is told to reach for first, a docs server is closer to that than `grep` is."""
    layer = BuildTools(granted=[], specs=[ToolSpec(name=f"{PREFIX}context7__x", description="",
                                                   input_schema={"type": "object"})])
    toolset = _toolset(wt, build_servers=layer, orientation=_orientation())

    names = [spec.name for spec in toolset.specs]

    assert names[:len(ORIENTATION_TOOLS)] == list(ORIENTATION_TOOLS)
    assert names.index(f"{PREFIX}context7__x") < names.index("grep")
    assert names.index("search_code") < names.index(f"{PREFIX}context7__x")


def test_an_agent_with_no_grant_keeps_every_tool_it_had(wt):
    """The layer is an addition, not a dependency: no seat has ever carried a grant until now,
    and every run so far has to keep working unchanged."""
    names = [spec.name for spec in _toolset(wt).specs]

    assert "read_file" in names and "run_tests" in names
    assert not any(name.startswith(PREFIX) for name in names)


def test_a_build_call_is_not_a_write_and_does_not_satisfy_the_completion_guard(wt, docs):
    """The guard is what stopped a fabricated completion (S7 walk). A docs call changes
    nothing in the worktree, so a run that only asked context7 something must still be refused
    a move to review — and must not be counted as having written."""
    _, layer = docs
    toolset = _toolset(wt, build_servers=layer, orientation=_orientation("update_item"))
    toolset.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id", q="x"))

    assert toolset.written == []
    refusal = toolset.execute(_call("update_item", id="GRPH-1", status="review"))

    assert refusal.is_error and "not changed any file" in refusal.content


def test_the_live_page_line_names_the_server_and_not_the_prefixed_tool(wt, docs):
    """`last_action` is what the Live page shows. `mcp__browser-use__navigate` is a name, not a
    sentence, and the row would read as noise."""
    _, layer = docs
    toolset = _toolset(wt, build_servers=layer)

    toolset.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id", q="x"))

    assert toolset.last_action == "calling context7 resolve-library-id"


def test_an_unknown_name_still_lists_the_whole_surface(wt, docs):
    _, layer = docs
    toolset = _toolset(wt, build_servers=layer)

    result = toolset.execute(_call("nope"))

    assert result.is_error
    assert f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id" in result.content
    assert "run_tests" in result.content


# ---- the transport underneath --------------------------------------------------------


def test_the_session_id_from_the_handshake_is_echoed_back():
    """Graphban's own server issues one at `initialize` and reads it at `tools/call`
    (`backend/app/mcp_server.py`). A client that dropped it would be refused by every server
    that keeps session state — which is most of the ones a grant names."""
    fake = FakeServer()
    server = McpServer(name="context7", url="https://mcp.context7.test/mcp",
                       transport=fake.transport())

    server.initialize()
    server.list_tools()

    sessions = {method: sid for _, method, _, sid, _ in fake.seen}
    assert sessions["initialize"] is None, "there is nothing to echo yet"
    assert sessions["tools/list"] == "sid-mcp.context7.test"


def test_a_server_that_wants_no_handshake_still_lists_its_tools():
    """The handshake is best-effort. Treating "this server is stateless" as "this grant is
    broken" would fail against the simplest servers there are."""
    fake = FakeServer()
    fake.no_handshake.add("mcp.context7.test")
    fake.tools["mcp.context7.test"] = [DOCS]
    server = McpServer(name="context7", url="https://mcp.context7.test/mcp",
                       transport=fake.transport())

    server.initialize()

    assert [t["name"] for t in server.list_tools()] == ["resolve-library-id"]


def test_an_event_stream_reply_is_read_and_not_reported_as_unreadable():
    """Streamable HTTP lets a server answer `text/event-stream`. Reading only `.json()` would
    report a working docs server as broken, and the model would be told the grant failed."""
    fake = FakeServer()
    fake.sse.add("mcp.context7.test")
    layer = _build(fake, {"context7": fake.grant("mcp.context7.test", [DOCS])})

    assert layer.granted[0].usable
    result = layer.execute(_call(f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id",
                                 q="httpx"))

    assert not result.is_error and "for httpx" in result.content


def test_the_handshake_names_the_protocol_version_this_distribution_serves():
    """One string, two ends. `gbfleet.mcp` answers `initialize` with its own constant and
    `client.py` cannot import it (that module imports client), so the agreement is pinned here
    rather than left to two literals that drift."""
    from gbfleet import client, mcp

    assert client.SHARED_PROTOCOL_VERSION == mcp.PROTOCOL_VERSION


def test_a_closed_layer_drops_its_sockets(docs):
    """A child that exits without dropping them leaves the operator's docs server carrying a
    connection per dead run, and a wave is hundreds of dead runs."""
    _, layer = docs
    assert layer.granted[0].server._http is not None, "the grant was connected at build"

    layer.close()

    assert layer.granted[0].server._http is None


# ---- the seat, end to end -----------------------------------------------------------


def test_read_shared_returns_the_whole_file_and_leaves_policy_to_the_layer(tmp_path):
    """`read_shared` is a read. The rule about which stanzas are grants lives in
    `buildtools.build`, beside the rest of what-may-be-called, so there is one place to argue
    with rather than a filter split across two modules — and a reader of the seat does not have
    to know which half of it is a credential."""
    fake = FakeServer()
    path = tmp_path / "seat.json"
    stanza = fake.grant("mcp.context7.test", [DOCS])
    written = Seat(code="X", server_url="https://gb.test", api_key="gbk_seat",
                   shared={"context7": stanza}).mcp_config()

    shared = _read(written, path)

    assert set(shared) == {"graphban", "context7"}, "the reader returns what the file holds"
    assert [g.name for g in _build(fake, shared).granted] == ["context7"], (
        "and the layer is what decides the ledger's own entry was never a grant")


def _read(config: dict, path: Path) -> dict:
    path.write_text(json.dumps(config))
    return cli.read_shared(path)


def test_the_seat_a_supervisor_writes_is_the_one_this_child_reads(tmp_path):
    """GRPH-816 wrote the grant into the seat and stopped. This is the round trip it left
    broken, asserted from the supervisor's own writer to the child's own advertisement."""
    fake = FakeServer()
    stanza = fake.grant("mcp.context7.test", [DOCS])
    path = tmp_path / "seat.json"
    path.write_text(json.dumps(
        Seat(code="X", server_url="https://gb.test", api_key="gbk_seat",
             shared={"context7": stanza}).mcp_config()))

    base_url, api_key = cli.read_seat(path)
    layer = _build(fake, cli.read_shared(path))

    assert (base_url, api_key) == ("https://gb.test", "gbk_seat"), "the credential path is unchanged"
    assert [spec.name for spec in layer.specs] == [
        f"{PREFIX}context7{buildtools.SEPARATOR}resolve-library-id"]


@pytest.mark.parametrize("body", ["not json at all", "{}", '{"mcpServers": []}',
                                  '{"mcpServers": {"context7": "http://c7"}}'])
def test_read_shared_never_refuses_a_seat_the_credential_reader_already_accepted(tmp_path, body):
    """`read_seat` is the refusal worth exiting 78 for. A malformed GRANT is one docs server a
    model cannot call, and killing the build over it would spend the run on something the work
    does not depend on."""
    path = tmp_path / "seat.json"
    path.write_text(body)

    assert cli.read_shared(path) == {}


def test_a_grant_does_not_move_the_seat_into_the_worktree(tmp_path):
    """The item's other boundary. An operator's docs-server credential now rides in this file,
    so the file has to stay where salvage cannot commit it."""
    from gbfleet.adapters import ADAPTERS
    from gbfleet.worktree import Worktree

    tree = tmp_path / "wt"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], capture_output=True, check=True)
    instruction = tmp_path / "instr"
    instruction.write_text("x")
    seat = Seat(code="X", server_url="https://gb.test", api_key="gbk_seat",
                shared={"context7": {"type": "http", "url": "https://c7.test/mcp",
                                     "headers": {"k": "operator-secret"}}})

    launch = ADAPTERS["gbagent"].launch(
        seat, Worktree(path=tree, branch="b", repo=tree), instruction, Path("/usr/bin/true"))

    with pytest.raises(ValueError):
        Path(launch.seat_path).resolve().relative_to(tree.resolve())
    assert "operator-secret" in json.dumps(launch.config), "the grant is in THAT file"
    assert "operator-secret" not in " ".join(launch.argv), "and not on argv, where `ps` reads it"


# ---- the production site -----------------------------------------------------------


def test_cli_run_is_what_connects_the_grant():
    """THE CALL, at the site that matters. Every test above constructs the layer itself;
    deleting the two lines in `_run` that build it and hand it to the toolset left them all
    green, and a spawned child then advertises nothing (GRPH-534's shape exactly)."""
    src = inspect.getsource(cli._run)

    assert "buildtools.build(read_shared(" in src, (
        "cli._run no longer builds the granted servers — a spawned child reads the seat, "
        "keeps the credential, and drops the grant")
    assert "build_servers=build_servers" in src, (
        "cli._run builds the layer and never hands it to the toolset, so nothing dispatches to it")
    assert "build_servers.instruction()" in src, (
        "the model is never told about a grant that came up empty, so an unusable server reads "
        "as no server at all")
    # Both halves, because pinning the call alone is a test that reads stronger than it is:
    # mutating the guard to `if False:` left the string `build_servers.close()` in the source
    # and this assertion green, with the sockets leaking on every run.
    assert "if build_servers is not None:" in src and "build_servers.close()" in src, (
        "cli._run leaves the granted servers' sockets open after the run")


def test_the_layer_never_reaches_past_the_seat():
    """`mcpshare.select` is the operator's decision, made before spawn, and it is what refuses
    a glob. A child that read `~/.claude.json` for itself would be holding every server on the
    machine — the leak GRPH-802 removed — with no refusal path at all.

    Asserted on the AST rather than on the text, because both modules SAY they do not do this,
    and a substring check would be satisfied by the sentence."""
    for module in (buildtools, cli):
        tree = ast.parse(inspect.getsource(module))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

        assert not {"select", "available", "source_path"} & called, (
            f"{module.__name__} reads the operator's own MCP config")
        assert not {"expanduser", "home"} & attributes, (
            f"{module.__name__} looks for a file outside the seat it was given")


def _orientation(*extra: str) -> orient.Orientation:
    """A real `Orientation`, because the ordering claim is about the two layers together."""
    names = [*ORIENTATION_TOOLS, *extra]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "tools": [{"name": n, "description": n, "inputSchema": {"type": "object"}}
                          for n in names]}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"structuredContent": {}}})

    return orient.build(Graphban("http://graphban.invalid", "gbk_seat",
                                 transport=httpx.MockTransport(handler)), extra=extra)
