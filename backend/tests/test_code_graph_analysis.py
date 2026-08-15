"""Structural queries over the code graph — hubs, components, path (PRD-20 D8).

Every assertion here is also a determinism assertion in disguise: the graph view keeps a
person's place by never moving a node without cause, and a structural overlay that reshuffled
between identical reads would give that away for nothing.
"""
import pytest

from app.services import code_graph as cg


@pytest.fixture
def db(client):
    """A session against the reset database. Depends on `client` for the same reason the
    other service-level tests do: it is what guarantees the schema exists and is clean."""
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _seed(db, project_id="core"):
    """Two components.

      hub.py  <- a.py, b.py, c.py          (c also imports b)
      x.py   <-> y.py                       (a separate island)
      lonely.py                             (described, no edges)
    """
    for path in ("hub.py", "a.py", "b.py", "c.py", "x.py", "y.py", "lonely.py"):
        cg.upsert_node(db, project_id=project_id, path=path, kind="module", name=path)
    for src, dst, t in (
        ("a.py", "hub.py", "imports"),
        ("b.py", "hub.py", "imports"),
        ("c.py", "hub.py", "imports"),
        ("c.py", "b.py", "calls"),
        ("x.py", "y.py", "imports"),
    ):
        cg.upsert_edge(db, project_id=project_id, src=src, dst=dst, type_=t)
    db.commit()


def test_hubs_rank_by_inbound_not_total_degree(db):
    """The fixture is built so the two rankings DISAGREE.

    An earlier version of this test used the shared seed, where `hub.py` happens to top both
    inbound and total degree — so it passed with the ranking swapped to total and asserted
    nothing about the distinction in its own name. `greedy.py` exists to break that tie:
    5 outbound against hub.py's 3 inbound, so total-degree ranking puts greedy.py first and
    inbound ranking puts hub.py first.
    """
    _seed(db)
    for dep in ("d1.py", "d2.py", "d3.py", "d4.py", "d5.py"):
        cg.upsert_node(db, project_id="core", path=dep, kind="module", name=dep)
        cg.upsert_edge(db, project_id="core", src="greedy.py", dst=dep, type_="imports")
    db.commit()

    ranked = cg.hubs(db, "core", limit=99)
    by_path = {r["path"]: r for r in ranked}
    assert by_path["greedy.py"]["outbound"] == 5 and by_path["greedy.py"]["inbound"] == 0
    assert by_path["hub.py"]["inbound"] == 3 and by_path["hub.py"]["outbound"] == 0

    # A file importing five things is complicated; a file five things import is load-bearing.
    # Only the second is the single point of failure the question is actually asking about.
    assert ranked[0]["path"] == "hub.py"
    assert [r["path"] for r in ranked].index("hub.py") < [r["path"] for r in ranked].index("greedy.py")


def test_hubs_are_deterministic_and_break_ties_on_path(db):
    _seed(db)
    once = cg.hubs(db, "core", limit=99)
    again = cg.hubs(db, "core", limit=99)
    assert once == again
    zero = [r["path"] for r in once if r["inbound"] == 0 and r["outbound"] == 0]
    assert zero == sorted(zero)


def test_hubs_respect_the_edge_type_filter(db):
    _seed(db)
    calls_only = cg.hubs(db, "core", edge_types=["calls"], limit=99)
    top = calls_only[0]
    assert top["path"] == "b.py" and top["inbound"] == 1
    assert next(r for r in calls_only if r["path"] == "hub.py")["inbound"] == 0


def test_hubs_surface_an_undescribed_node(db):
    # An edge endpoint nobody described is exactly the load-bearing node worth surfacing;
    # dropping it because it has no CodeNode row would hide the answer.
    cg.upsert_node(db, project_id="core", path="described.py", kind="module", name="d")
    cg.upsert_edge(db, project_id="core", src="described.py", dst="ghost.py", type_="imports")
    db.commit()
    top = cg.hubs(db, "core", limit=1)[0]
    assert top["path"] == "ghost.py"
    assert top["inbound"] == 1
    assert top["described"] is False and top["kind"] is None


def test_components_split_islands_and_order_largest_first(db):
    _seed(db)
    comps = cg.components(db, "core")
    assert [c["size"] for c in comps] == [4, 2, 1]
    assert comps[0]["members"] == ["a.py", "b.py", "c.py", "hub.py"]
    assert comps[1]["members"] == ["x.py", "y.py"]
    assert comps[2]["members"] == ["lonely.py"]


def test_component_anchor_is_the_highest_inbound_member(db):
    _seed(db)
    big = cg.components(db, "core")[0]
    # The label a collapsed component wears in D9's galaxy view.
    assert big["anchor"] == "hub.py"


def test_components_are_deterministic_across_reads(db):
    _seed(db)
    assert cg.components(db, "core") == cg.components(db, "core")


def test_components_ignore_a_self_loop(db):
    cg.upsert_node(db, project_id="core", path="solo.py", kind="module", name="s")
    cg.upsert_edge(db, project_id="core", src="solo.py", dst="solo.py", type_="calls")
    db.commit()
    comps = cg.components(db, "core")
    assert len(comps) == 1 and comps[0]["members"] == ["solo.py"]


def test_path_traverses_undirected_but_reports_direction(db):
    _seed(db)
    # a.py -> hub.py <- b.py. Directionally there is no route from a to b; as a reachability
    # question there plainly is, and reporting "not connected" would be a lie about the code.
    res = cg.path(db, "core", "a.py", "b.py")
    assert res["found"] is True
    assert [h["dst"] for h in res["hops"]] == ["hub.py", "b.py"]
    assert res["hops"][0] == {"src": "a.py", "dst": "hub.py", "type": "imports", "forward": True}
    # The second hop runs against the arrow, and says so rather than hiding it.
    assert res["hops"][1]["forward"] is False


def test_path_finds_the_shortest_route(db):
    _seed(db)
    # c.py reaches b.py directly, not via hub.py.
    res = cg.path(db, "core", "c.py", "b.py")
    assert len(res["hops"]) == 1


def test_path_reports_no_route_separately_from_no_such_node(db):
    _seed(db)
    unreachable = cg.path(db, "core", "a.py", "x.py")
    assert unreachable["found"] is False and unreachable["missing"] == []

    unknown = cg.path(db, "core", "a.py", "nope.py")
    assert unknown["found"] is False and unknown["missing"] == ["nope.py"]
    # These must not collapse into one answer: "nothing connects these" and "you named a file
    # I have never heard of" send a caller in different directions.
    assert unreachable["missing"] != unknown["missing"]


def test_path_to_self_is_found_with_no_hops(db):
    _seed(db)
    res = cg.path(db, "core", "hub.py", "hub.py")
    assert res["found"] is True and res["hops"] == []


def test_path_is_deterministic_when_routes_tie(db):
    # Two equally short routes from start to end; the answer must not depend on row order.
    for p in ("start.py", "left.py", "right.py", "end.py"):
        cg.upsert_node(db, project_id="core", path=p, kind="module", name=p)
    for src, dst in (("start.py", "left.py"), ("start.py", "right.py"),
                     ("left.py", "end.py"), ("right.py", "end.py")):
        cg.upsert_edge(db, project_id="core", src=src, dst=dst, type_="imports")
    db.commit()
    first = cg.path(db, "core", "start.py", "end.py")
    assert first["hops"] == cg.path(db, "core", "start.py", "end.py")["hops"]
    assert len(first["hops"]) == 2


def test_path_respects_the_edge_type_filter(db):
    _seed(db)
    # c.py -> b.py is the only `calls` edge; via imports only, the route runs through hub.py.
    assert len(cg.path(db, "core", "c.py", "b.py", edge_types=["calls"])["hops"]) == 1
    assert len(cg.path(db, "core", "c.py", "b.py", edge_types=["imports"])["hops"]) == 2


def test_analysis_composes_the_three_and_omits_path_without_endpoints(db):
    _seed(db)
    out = cg.analysis(db, "core", limit=2)
    assert len(out["hubs"]) == 2
    assert out["components"][0]["anchor"] == "hub.py"
    assert out["path"] is None
    with_path = cg.analysis(db, "core", a="a.py", b="b.py")
    assert with_path["path"]["found"] is True


def test_analysis_is_empty_but_shaped_on_a_graph_with_nothing_in_it(db):
    out = cg.analysis(db, "core")
    assert out == {"hubs": [], "components": [], "path": None}


# ── the surfaces (PRD-20 D8: one REST route, one MCP tool) ────────────────────

import json  # noqa: E402


def _mcp_raw(client, key, name, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _mcp(client, key, name, args):
    return json.loads(_mcp_raw(client, key, name, args)["content"][0]["text"])


def _key(client, auth):
    return client.post(
        "/api/api-keys", json={"name": "graph-agent", "project_id": "core"}, headers=auth
    ).json()["plaintext"]


def test_graph_query_over_mcp_answers_all_three(client, auth, db):
    _seed(db)
    key = _key(client, auth)

    hubs = _mcp(client, key, "graph_query", {"query": "hubs", "limit": 2})
    assert hubs["returned"] == 2
    assert hubs["results"][0]["path"] == "hub.py"

    comps = _mcp(client, key, "graph_query", {"query": "components"})
    assert [c["size"] for c in comps["results"]] == [4, 2, 1]

    route = _mcp(client, key, "graph_query", {"query": "path", "a": "a.py", "b": "b.py"})
    assert route["found"] is True and len(route["hops"]) == 2


def test_graph_query_refuses_an_unknown_edge_type_rather_than_narrowing(client, auth, db):
    _seed(db)
    key = _key(client, auth)
    res = _mcp_raw(client, key, "graph_query", {"query": "hubs", "edge_types": ["nope"]})
    # Filtering to nothing and returning [] would be indistinguishable from a real empty graph.
    assert res["isError"] is True
    assert "unknown edge type" in res["content"][0]["text"].lower()


def test_graph_query_path_without_endpoints_is_refused(client, auth, db):
    _seed(db)
    key = _key(client, auth)
    res = _mcp_raw(client, key, "graph_query", {"query": "path", "a": "a.py"})
    assert res["isError"] is True


def test_code_analysis_route_serves_the_same_answers(client, auth, db):
    _seed(db)
    r = client.get("/api/agent/code/analysis?project_id=core&limit=1", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["hubs"][0]["path"] == "hub.py"
    assert body["components"][0]["anchor"] == "hub.py"
    assert body["path"] is None


def test_code_analysis_route_422s_on_an_unknown_edge_type(client, auth, db):
    _seed(db)
    r = client.get(
        "/api/agent/code/analysis?project_id=core&edge_types=imports,bogus", headers=auth
    )
    assert r.status_code == 422


def test_code_analysis_route_needs_auth(client, db):
    _seed(db)
    assert client.get("/api/agent/code/analysis?project_id=core").status_code == 401


# ── graph health (GRPH-404) ───────────────────────────────────────────────────

def test_health_distinguishes_never_described_from_nothing_stale(db):
    """The absence rule, which is the whole reason this is a read and not a red/green light.

    "Nothing is stale" and "no describe pass has ever run, so nothing COULD be stale" must not
    print the same. `ever_described` is the third answer.
    """
    empty = cg.health(db, "core")
    assert empty["ever_described"] is False
    assert empty["described"] == 0
    assert empty["stale_but_claimed"] == []

    cg.upsert_node(db, project_id="core", path="a.py", kind="file", name="a")
    db.commit()
    described = cg.health(db, "core")
    assert described["ever_described"] is True
    # Same empty stale list, different meaning — and the payload says which.
    assert described["stale_but_claimed"] == []


def test_health_reports_a_stale_node_that_open_work_still_claims(client, auth, db):
    """The highest-value signal: the map is out of date exactly where work is happening."""
    cg.upsert_node(db, project_id="core", path="backend/gone.py", kind="file", name="gone")
    db.commit()
    cg.mark_paths_stale(db, "core", ["backend/gone.py"])
    db.commit()
    key = _key(client, auth)
    _mcp(client, key, "create_item", {"title": "touches the stale file",
                                      "touchpoints": ["backend/gone.py"]})

    out = cg.health(db, "core")
    hit = [r for r in out["stale_but_claimed"] if r["area"] == "backend/gone.py"]
    assert hit and hit[0]["paths"] == ["backend/gone.py"]


def test_health_reports_an_unresolvable_touchpoint_without_guessing_why(client, auth, db):
    """The server has no checkout, so it flags only what it can know for CERTAIN.

    `outside_repo` is decidable from the string. Whether `vercel env` is a missing file or not a
    path at all is not, and guessing would contradict the same decision PRD-20 D4 made about
    off-map areas.
    """
    cg.upsert_node(db, project_id="core", path="a.py", kind="file", name="a")
    db.commit()
    key = _key(client, auth)
    _mcp(client, key, "create_item", {"title": "external work",
                                      "touchpoints": ["vercel env", "../other-repo/**"]})

    out = cg.health(db, "core")
    by_area = {r["area"]: r for r in out["unresolvable"]}
    assert by_area["vercel env"]["outside_repo"] is False
    assert by_area["../other-repo/**"]["outside_repo"] is True
    # No verdict beyond that — no `missing_file`, no `not_a_path`.
    assert set(by_area["vercel env"]) == {"area", "items", "outside_repo"}


def test_health_counts_coverage_and_orphans(db):
    cg.upsert_node(db, project_id="core", path="linked.py", kind="file", name="l")
    cg.upsert_node(db, project_id="core", path="other.py", kind="file", name="o")
    cg.upsert_node(db, project_id="core", path="alone.py", kind="file", name="a")
    cg.upsert_edge(db, project_id="core", src="linked.py", dst="other.py", type_="imports")
    db.commit()
    out = cg.health(db, "core")
    assert out["described"] == 3 and out["edges"] == 1
    assert "alone.py" in out["orphans"]
    assert "linked.py" not in out["orphans"]
    assert out["kinds"]["file"] == 3


def test_health_never_retires_anything(client, auth, db):
    """GRPH-343's rule, transferred: output is a prompt for a human, never an automatic retire.

    An earlier version stale-marked a node and called `health` with NO item claiming it — so
    `stale_but_claimed` was empty, the retirement path was never reached, and a sabotage that
    deleted every node in that list PASSED. The fixture has to reach the code the test claims to
    guard, which is the failure mode this suite keeps turning up.
    """
    cg.upsert_node(db, project_id="core", path="gone.py", kind="file", name="g")
    db.commit()
    cg.mark_paths_stale(db, "core", ["gone.py"])
    db.commit()
    key = _key(client, auth)
    _mcp(client, key, "create_item", {"title": "claims the stale file",
                                      "touchpoints": ["gone.py"]})

    out = cg.health(db, "core")
    assert out["stale_but_claimed"], "the fixture must reach the retirement path"
    db.commit()
    assert [n.path for n in cg.list_nodes(db, "core")] == ["gone.py"]


def test_health_route_serves_a_member_and_refuses_anonymous(client, auth, db):
    cg.upsert_node(db, project_id="core", path="a.py", kind="file", name="a")
    db.commit()
    r = client.get("/api/agent/code/health?project_id=core", headers=auth)
    assert r.status_code == 200 and r.json()["ever_described"] is True
    assert client.get("/api/agent/code/health?project_id=core").status_code == 401


def test_health_is_not_on_the_mcp_surface(client, auth):
    """Not principle — the manifest ceiling. See the route docstring."""
    from app.mcp_server import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "graph_health" not in names
    gq = [t for t in TOOLS if t["name"] == "graph_query"][0]
    assert "health" not in gq["inputSchema"]["properties"]["query"]["enum"]
