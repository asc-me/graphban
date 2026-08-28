"""`get_code_map` returns a page, and a page never reads as the whole graph (GRPH-146).

GRPH-55 records this as real scaling debt — *"get_code_map returns every node and edge"* —
and defers it against a measurable trigger. This is the read half of that deferral, brought
forward because the cost is not proportional to project size in the way the trigger assumes:
a described node serialises to roughly 400 bytes, so a few hundred of them is more context
than an agent's entire tool manifest, spent before it has asked a question.

**The defect this file mostly exists to prevent is not the size, it is the LIE.** Bounding a
read is easy; bounding it while `node_count` keeps reporting the number of rows returned
gives an agent a small complete map. It reads `node_count: 200` on a four-thousand-node
project, concludes it has the shape of the codebase, and reasons from a twentieth of it with
nothing in the payload to suggest otherwise. That is absence reading as clean, in the one
place where the caller has no way to check.

So the totals stay totals, `returned_*` says what came back, and `truncated` is a flag rather
than an inference the caller has to draw by comparing two numbers correctly.
"""
from __future__ import annotations

import json


def _mcp(client, key, name, args):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers={"X-API-Key": key},
    )
    return json.loads(r.json()["result"]["content"][0]["text"])


def _key(client, auth):
    return client.post(
        "/api/api-keys", json={"name": "map-agent", "project_id": "core"}, headers=auth
    ).json()["plaintext"]


def _graph(client, key, n: int):
    """`n` file nodes in a chain, so every node but the last has an outgoing edge."""
    nodes = [{"path": f"backend/app/gen/mod_{i:03d}.py", "kind": "file",
              "name": f"mod {i}", "lang": "python",
              "summary": f"Generated node {i} for the paging tests.",
              "content_hash": f"sha-{i:03d}"} for i in range(n)]
    edges = [{"src": nodes[i]["path"], "dst": nodes[i + 1]["path"], "type": "imports"}
             for i in range(n - 1)]
    # DESCRIBED IN REVERSE, deliberately. Insert order becomes rowid order, and a query with
    # no ORDER BY returns rowid order — so seeding in path order makes "sorted" and "as
    # inserted" the same sequence and a dropped ORDER BY is invisible. That is not
    # hypothetical: it survived the sabotage pass until this line existed.
    res = _mcp(client, key, "describe_code", {"nodes": nodes[::-1], "edges": edges})
    assert res["nodes_upserted"] == n, res
    return nodes


# ── the totals are totals ─────────────────────────────────────────────────────

def test_a_page_reports_the_projects_total_not_the_page_size(client, auth):
    """THE test. `node_count` is what an agent reads to decide how big the codebase is."""
    key = _key(client, auth)
    _graph(client, key, 12)

    page = _mcp(client, key, "get_code_map", {"limit": 5})

    assert page["returned_nodes"] == 5, "the cap was not applied"
    assert len(page["nodes"]) == 5
    assert page["node_count"] >= 12, (
        f"node_count is {page['node_count']} — a page reporting its own size as the "
        "project's total is a small COMPLETE map as far as the reader can tell")
    assert page["truncated"] is True


def test_an_unbounded_read_is_not_marked_truncated(client, auth):
    """The complement. Flagging every response as truncated would satisfy the test above and
    tell a caller nothing — it has to distinguish."""
    key = _key(client, auth)
    _graph(client, key, 6)

    whole = _mcp(client, key, "get_code_map", {"limit": 0})

    assert whole["truncated"] is False
    assert whole["returned_nodes"] == whole["node_count"]


def test_the_last_page_is_not_marked_truncated(client, auth):
    """Off-by-one at the boundary: a caller that pages until `truncated` is false must
    terminate, and must not stop one page early either."""
    key = _key(client, auth)
    _graph(client, key, 10)
    total = _mcp(client, key, "get_code_map", {"limit": 0})["node_count"]

    last = _mcp(client, key, "get_code_map", {"limit": 5, "offset": total - 5})

    assert last["truncated"] is False, "the final page claims there is more"
    assert last["offset"] == total - 5


# ── paging is coherent ────────────────────────────────────────────────────────

def test_paging_covers_every_node_exactly_once(client, auth):
    """Ordering is what makes a page meaningful. An unordered LIMIT returns an arbitrary
    subset that can change between calls, so page 2 could repeat or skip rows from page 1
    with nothing in either response to indicate it."""
    key = _key(client, auth)
    _graph(client, key, 11)
    total = _mcp(client, key, "get_code_map", {"limit": 0})["node_count"]

    seen: list[str] = []
    offset = 0
    for _ in range(total + 2):   # a BOUNDED loop, see below
        page = _mcp(client, key, "get_code_map", {"limit": 4, "offset": offset})
        seen.extend(n["path"] for n in page["nodes"])
        if not page["truncated"]:
            break
        # An empty page that still claims more is the non-terminating case, and it has to be
        # an assertion rather than a loop condition: `offset += 0` spins forever, so a `while`
        # here HANGS instead of failing. Found by sabotage — forcing `truncated` true turned
        # this test into a 15-minute run rather than a red one, and a test that hangs reports
        # nothing at all.
        assert page["returned_nodes"] > 0, (
            f"an empty page at offset {offset} still reports truncated — paging cannot finish")
        offset += page["returned_nodes"]
    else:
        raise AssertionError(f"paging did not terminate within {total + 2} pages")

    assert len(seen) == total, f"paged {len(seen)} nodes, project holds {total}"
    assert len(set(seen)) == total, "a node appeared on two pages"
    # THE ORDERING ITSELF. Without a stable ORDER BY, LIMIT/OFFSET slices an unspecified
    # sequence: the database is free to return rows in a different order between the two
    # queries, so a node can appear on two pages while another appears on none. Counting
    # cannot see it here — the seed is described in reverse, so rowid order and path order
    # disagree, and only a real ORDER BY produces a sorted walk.
    assert seen == sorted(seen), (
        "paging did not walk the nodes in a stable order — LIMIT/OFFSET over an unordered "
        "query returns an arbitrary slice that can repeat or skip rows between calls")


def test_a_page_carries_no_edge_pointing_outside_it(client, auth):
    """A page whose edges reference nodes it did not include reads as a broken graph rather
    than a partial one — and an agent drawing conclusions about coupling from it is reading
    dangling arrows."""
    key = _key(client, auth)
    _graph(client, key, 12)

    page = _mcp(client, key, "get_code_map", {"limit": 4})

    paths = {n["path"] for n in page["nodes"]}
    dangling = [e for e in page["edges"] if e["src"] not in paths or e["dst"] not in paths]
    assert not dangling, f"edges point outside the page: {dangling[:3]}"


def test_the_edge_total_is_the_projects_not_the_pages(client, auth):
    """Same lie as `node_count`, one field over."""
    key = _key(client, auth)
    _graph(client, key, 12)

    page = _mcp(client, key, "get_code_map", {"limit": 3})

    assert page["edge_count"] >= 11, (
        f"edge_count is {page['edge_count']} — it must be the project's total, not this "
        "page's")
    assert page["returned_edges"] <= page["edge_count"]


def test_the_node_query_is_ordered(client):
    """The ordering guarantee, asserted where it is REACHABLE.

    `test_paging_covers_every_node_exactly_once` cannot see this and neither can any other
    end-to-end test: `uq_code_node_path` is a unique index on (project_id, path), so both
    engines return path order from an index scan even with `ORDER BY` removed. Measured —
    described in reverse, SQLite still hands back sorted paths, and Postgres carries the same
    index. The sabotage pass survived on exactly that coincidence.

    An unordered LIMIT/OFFSET is not wrong today and is not guaranteed tomorrow: the planner
    may pick another shape as statistics change, and then pages repeat and skip rows with
    nothing in either response to show it. A guarantee that rests on the current query plan is
    not a guarantee, so it is pinned on the statement.
    """
    from app.services.code_graph import nodes_stmt

    sql = str(nodes_stmt("core", limit=5, offset=5).compile())
    assert "ORDER BY" in sql.upper(), (
        f"the node query has no ORDER BY, so paging slices an unspecified sequence:\n{sql}")
    assert "code_nodes.path" in sql, "ordered by something other than path"
    assert "LIMIT" in sql.upper() and "OFFSET" in sql.upper(), (
        "the bound is not reaching SQL — returning fewer rows after fetching them all leaves "
        "the scan GRPH-55 records")


# ── the default is the point ──────────────────────────────────────────────────

def test_an_agent_that_asks_for_nothing_gets_a_page(client, auth):
    """The caller who does not know to ask is exactly the one who cannot afford the answer.
    If the cap only applied when requested, the unbounded dump would remain the default and
    this change would be decoration."""
    from app.services import code_graph as code_svc

    key = _key(client, auth)
    _graph(client, key, code_svc.DEFAULT_MAP_NODES + 5)

    plain = _mcp(client, key, "get_code_map", {})

    assert plain["returned_nodes"] == code_svc.DEFAULT_MAP_NODES
    assert plain["truncated"] is True


def test_the_manifest_says_the_read_is_capped(client, auth):
    """A silent cap is the defect wearing the fix's name. The description is where an agent
    learns to check, and it is the first thing a token trim would take."""
    from app.mcp_server import TOOLS
    from app.services import code_graph as code_svc

    desc = next(t for t in TOOLS if t["name"] == "get_code_map")["description"]
    assert str(code_svc.DEFAULT_MAP_NODES) in desc, "the cap is not stated"
    assert "truncated" in desc, "nothing tells the agent to check whether it got everything"

    props = next(t for t in TOOLS if t["name"] == "get_code_map")["inputSchema"]["properties"]
    assert "limit" in props and "offset" in props, (
        "a capped read with no way to page is a smaller silence, not a fix")


# ── the one caller that must NOT be bounded ───────────────────────────────────

def test_the_graph_view_still_gets_everything(client, auth):
    """The view draws the whole graph with a force-directed layout. A page would produce a
    picture of an arbitrary fifth of the codebase with no way to tell from looking, which is
    why the cap lives at the MCP boundary and not in the service default."""
    from app.services import code_graph as code_svc

    key = _key(client, auth)
    _graph(client, key, code_svc.DEFAULT_MAP_NODES + 5)

    payload = client.get("/api/agent/code/map?project_id=core", headers=auth).json()

    # `truncated` is deliberately absent here: the route declares `CodeMapOut`, which filters
    # to the fields the view consumes, and the answer for this caller is always False anyway.
    # The substantive assertion is that EVERY node came back.
    assert len(payload["nodes"]) == payload["node_count"] > code_svc.DEFAULT_MAP_NODES, (
        f"the graph view got {len(payload['nodes'])} of {payload['node_count']} nodes — a "
        "force-directed layout over a page draws an arbitrary fifth of the codebase")


def test_the_chat_context_does_not_build_the_graph_to_count_it(client, auth, monkeypatch):
    """It called `get_code_map` — every node with its summary, every edge — to print two
    integers, on a path that runs per chat message.

    Driven rather than asserted on the source: `get_code_map` is made to raise, so the test
    fails if the counting path touches it at all. Asserting "COUNT was used" by reading the
    code would pass against a version that also loaded the map and ignored it.
    """
    from app.routers import agent as agent_router
    from app.services import code_graph as code_svc

    key = _key(client, auth)
    _graph(client, key, 6)

    def _boom(*a, **kw):
        raise AssertionError("the chat context built the whole code map to count it")

    monkeypatch.setattr(agent_router.code_svc, "get_code_map", _boom)

    text = agent_router._build_code_context(_session(), "core", [])
    assert "described nodes" in text
    assert "6 described nodes" in text, f"the counts are wrong: {text[:120]}"
    assert code_svc.count_nodes is not None


def _session():
    from app.db import SessionLocal

    return SessionLocal()
