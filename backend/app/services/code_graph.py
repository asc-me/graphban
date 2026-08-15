"""Code-structure graph service (feature: agent-described codebase map).

A queryable map of the code itself — `CodeNode`s (module/file/symbol, each with an
embedded summary) joined by typed `CodeEdge`s (imports/calls/owns/tested_by/references).

The producer is normally the *external coding agent* via the MCP `describe_code` tool:
it has the real repo in context, so it describes structure as ground truth. Graphban's
connected LLM is the *consumer* — `search_code` / `neighbors` / `get_code_map` are what it
(and the UI) read to reason about the codebase without holding a checkout.

Edges are keyed by path so they can point at a not-yet-described node. Item↔code links are
*not* stored here — items already carry `touchpoints`, so `neighbors` intersects those live
(one source of truth) rather than duplicating the relation.
"""
from __future__ import annotations

import fnmatch
import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.embeddings import cosine_similarity, get_embedder, safe_embed
from app.errors import NotFound
from app.models import CodeEdge, CodeNode, CodeRef, Item, Request
from app.services import items as items_svc
from app.services.clustering import _match

NODE_KINDS = ["module", "file", "symbol"]
EDGE_TYPES = ["imports", "calls", "owns", "tested_by", "references"]
REF_TYPES = ["item", "request"]
# How a piece of work relates to a code path (item/request → code node).
REF_RELATIONS = ["affects", "implements", "fixes", "tests", "references"]


def _embed_input(*, path: str, name: str, summary: str) -> str:
    """What we embed for semantic search: the human-meaningful text of the node."""
    return " ".join(p for p in (name, path, summary) if p)


def backfill_embeddings(db: Session) -> int:
    """Re-embed every code node with the current provider. Run after switching
    providers or changing the embedding dimension (AL-64)."""
    embedder = get_embedder()
    nodes = list(db.scalars(select(CodeNode)).all())
    for n in nodes:
        n.embedding = embedder.embed(_embed_input(path=n.path, name=n.name, summary=n.summary))
    db.commit()
    return len(nodes)


# ── describe (upsert) ─────────────────────────────────────────────────────────

def upsert_node(
    db: Session,
    *,
    project_id: str,
    path: str,
    kind: str = "file",
    name: str = "",
    lang: str = "",
    summary: str = "",
    content_hash: str = "",
    fresh: bool = True,
) -> CodeNode:
    """Create or update the node at (project_id, path). Re-embeds only when the embed
    input actually changed, so an unchanged re-describe is cheap."""
    path = path.strip()
    if kind not in NODE_KINDS:
        kind = "file"
    node = db.scalars(
        select(CodeNode).where(CodeNode.project_id == project_id, CodeNode.path == path)
    ).first()
    new_embed_input = _embed_input(path=path, name=name, summary=summary)

    if node is None:
        node = CodeNode(
            id="cn_" + uuid.uuid4().hex[:10],
            project_id=project_id,
            path=path,
            kind=kind,
            name=name,
            lang=lang,
            summary=summary,
            content_hash=content_hash,
            fresh=fresh,
            embedding=safe_embed(new_embed_input),
        )
        db.add(node)
    else:
        old_embed_input = _embed_input(path=node.path, name=node.name, summary=node.summary)
        node.kind = kind
        node.name = name
        node.lang = lang or node.lang
        node.summary = summary
        node.content_hash = content_hash or node.content_hash
        node.fresh = fresh
        if new_embed_input != old_embed_input:
            node.embedding = safe_embed(new_embed_input)
    return node


def delete_project_graph(db: Session, project_id: str) -> dict:
    """Purge a project's code graph — actually DELETE every node and edge (AL-137 D8: remove
    the synced graph from the cloud). Unlike prune/mark_paths_stale, this doesn't just flag
    stale; it removes. Returns the counts deleted."""
    nodes = db.scalars(select(CodeNode).where(CodeNode.project_id == project_id)).all()
    edges = db.scalars(select(CodeEdge).where(CodeEdge.project_id == project_id)).all()
    counts = {"deleted_nodes": len(nodes), "deleted_edges": len(edges)}
    for row in (*nodes, *edges):
        db.delete(row)
    return counts


def mark_paths_stale(db: Session, project_id: str, paths: list[str]) -> int:
    """Mark specific nodes stale (`fresh=False`) — the incremental-sync counterpart of
    describe_code's batch prune (AL-139). Used when a linked local instance reports paths it
    removed. Never deletes; a later describe re-freshens whatever is still real."""
    if not paths:
        return 0
    rows = db.scalars(
        select(CodeNode).where(CodeNode.project_id == project_id, CodeNode.path.in_(list(paths)))
    ).all()
    for node in rows:
        node.fresh = False
    return len(rows)


def upsert_edge(db: Session, *, project_id: str, src: str, dst: str, type_: str = "imports") -> CodeEdge | None:
    src, dst = src.strip(), dst.strip()
    if not src or not dst:
        return None
    if type_ not in EDGE_TYPES:
        type_ = "references"
    existing = db.scalars(
        select(CodeEdge).where(
            CodeEdge.project_id == project_id,
            CodeEdge.src == src,
            CodeEdge.dst == dst,
            CodeEdge.type == type_,
        )
    ).first()
    if existing is not None:
        return existing
    edge = CodeEdge(project_id=project_id, src=src, dst=dst, type=type_)
    db.add(edge)
    return edge


def describe_code(
    db: Session,
    *,
    project_id: str,
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
    prune: bool = False,
) -> dict:
    """Upsert a batch of nodes and edges the agent has described. Idempotent by
    (project_id, path) for nodes and (project_id, src, dst, type) for edges.

    `prune=True` marks any *existing* node in this project that wasn't in this batch as
    stale (`fresh=False`) — the invalidation half of the staleness handle. It never
    deletes; a later describe re-freshens whatever is still real.
    """
    nodes = nodes or []
    edges = edges or []
    seen_paths: set[str] = set()

    n_up = 0
    for n in nodes:
        path = str(n.get("path", "")).strip()
        if not path:
            continue
        upsert_node(
            db,
            project_id=project_id,
            path=path,
            kind=str(n.get("kind", "file")),
            name=str(n.get("name", "")),
            lang=str(n.get("lang", "")),
            summary=str(n.get("summary", "")),
            content_hash=str(n.get("content_hash", "")),
        )
        seen_paths.add(path)
        n_up += 1

    e_up = 0
    for e in edges:
        edge = upsert_edge(
            db,
            project_id=project_id,
            src=str(e.get("src", "")),
            dst=str(e.get("dst", "")),
            type_=str(e.get("type", "imports")),
        )
        if edge is not None:
            e_up += 1

    stale_paths: list[str] = []
    if prune and seen_paths:
        stale = db.scalars(
            select(CodeNode).where(
                CodeNode.project_id == project_id,
                CodeNode.path.notin_(seen_paths),
                CodeNode.fresh.is_(True),
            )
        ).all()
        for node in stale:
            node.fresh = False
            stale_paths.append(node.path)

    db.commit()
    # Echo the paths touched so the agent can verify the effect without a full
    # get_code_map round-trip (AL-47 — the describe_code verification edge).
    return {
        "nodes_upserted": n_up,
        "edges_upserted": e_up,
        "marked_stale": len(stale_paths),
        "upserted_paths": sorted(seen_paths),
        "stale_paths": sorted(stale_paths),
    }


# ── read ──────────────────────────────────────────────────────────────────────

def node_dict(node: CodeNode) -> dict:
    return {
        "id": node.id,
        "project_id": node.project_id,
        "path": node.path,
        "kind": node.kind,
        "name": node.name,
        "lang": node.lang,
        "summary": node.summary,
        "content_hash": node.content_hash,
        "fresh": node.fresh,
    }


def _edge_dict(edge: CodeEdge) -> dict:
    return {"src": edge.src, "dst": edge.dst, "type": edge.type}


def list_nodes(db: Session, project_id: str, kind: str | None = None) -> list[CodeNode]:
    stmt = select(CodeNode).where(CodeNode.project_id == project_id)
    if kind:
        stmt = stmt.where(CodeNode.kind == kind)
    return list(db.scalars(stmt.order_by(CodeNode.path)).all())


def list_edges(db: Session, project_id: str) -> list[CodeEdge]:
    return list(
        db.scalars(select(CodeEdge).where(CodeEdge.project_id == project_id)).all()
    )


def get_code_map(db: Session, project_id: str, kind: str | None = None) -> dict:
    nodes = list_nodes(db, project_id, kind=kind)
    edges = list_edges(db, project_id)
    if kind:
        # When filtered to a kind, keep only edges wholly inside the filtered node set.
        keep = {n.path for n in nodes}
        edges = [e for e in edges if e.src in keep and e.dst in keep]
    return {
        "nodes": [node_dict(n) for n in nodes],
        "edges": [_edge_dict(e) for e in edges],
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


def _items_touching(db: Session, project_id: str, path: str) -> list[dict]:
    """Work items whose touchpoints relate to `path` — reuses the clustering matcher so
    'code the agent described' and 'work that touches it' agree on what 'related' means."""
    out = []
    for it in items_svc.list_items(db, project_id=project_id):
        if any(_match(path, tp) for tp in (it.touchpoints or [])):
            out.append({"id": it.id, "title": it.title, "status": it.status})
    return out


def neighbors(db: Session, project_id: str, path: str) -> dict:
    """The relations around `path`: outgoing/incoming edges grouped by type, the work items
    whose *touchpoints* touch it (fuzzy/derived), and the items/requests **explicitly linked**
    to it via CodeRef (curated/typed). Returns a structure even for an unknown path
    (node = null) so an agent can still see what points *at* an undescribed file."""
    path = path.strip()
    node = db.scalars(
        select(CodeNode).where(CodeNode.project_id == project_id, CodeNode.path == path)
    ).first()
    edges = list_edges(db, project_id)
    outgoing = [{"dst": e.dst, "type": e.type} for e in edges if e.src == path]
    incoming = [{"src": e.src, "type": e.type} for e in edges if e.dst == path]
    linked = linked_work_for_path(db, project_id, path)
    return {
        "path": path,
        "node": node_dict(node) if node else None,
        "outgoing": outgoing,
        "incoming": incoming,
        "items_touching": _items_touching(db, project_id, path),
        "linked_items": linked["items"],
        "linked_requests": linked["requests"],
    }


# ── item/request ↔ code bridge (CodeRef) ──────────────────────────────────────

def _resolve_ref(db: Session, project_id: str, ref_id: str, ref_type: str | None):
    """Resolve a tracker ref to (ref_type, object). Infers the type from whichever table
    holds the id when ref_type is omitted. Raises if it isn't in this project."""
    ref_id = str(ref_id).strip()
    if ref_type in (None, "item"):
        it = db.get(Item, ref_id)
        if it is not None and it.project_id == project_id:
            return "item", it
        if ref_type == "item":
            raise NotFound(f"unknown item in project: {ref_id}")
    if ref_type in (None, "request"):
        rq = db.get(Request, ref_id)
        if rq is not None and rq.project_id == project_id:
            return "request", rq
        if ref_type == "request":
            raise NotFound(f"unknown request in project: {ref_id}")
    raise NotFound(f"unknown item or request in project: {ref_id}")


def ref_dict(ref: CodeRef) -> dict:
    return {
        "id": ref.id,
        "ref_type": ref.ref_type,
        "ref_id": ref.ref_id,
        "path": ref.path,
        "relation": ref.relation,
    }


def link_code(
    db: Session,
    *,
    project_id: str,
    ref_id: str,
    path: str,
    relation: str = "affects",
    ref_type: str | None = None,
) -> CodeRef:
    """Link a tracker item/request to a code path. Idempotent by the natural key; validates
    the ref exists in the project. The path need not be a described node yet."""
    rtype, _obj = _resolve_ref(db, project_id, ref_id, ref_type)
    if relation not in REF_RELATIONS:
        relation = "references"
    path = path.strip()
    if not path:
        raise ValueError("path is required")
    existing = db.scalars(
        select(CodeRef).where(
            CodeRef.project_id == project_id,
            CodeRef.ref_type == rtype,
            CodeRef.ref_id == ref_id,
            CodeRef.path == path,
            CodeRef.relation == relation,
        )
    ).first()
    if existing is not None:
        return existing
    ref = CodeRef(project_id=project_id, ref_type=rtype, ref_id=ref_id, path=path, relation=relation)
    db.add(ref)
    db.commit()
    db.refresh(ref)
    return ref


def unlink_code(
    db: Session,
    *,
    project_id: str,
    ref_id: str,
    path: str,
    relation: str | None = None,
) -> int:
    """Remove links from a ref to a path. When `relation` is None, removes every relation for
    that (ref, path) pair. Returns the number removed."""
    stmt = select(CodeRef).where(
        CodeRef.project_id == project_id,
        CodeRef.ref_id == str(ref_id).strip(),
        CodeRef.path == path.strip(),
    )
    if relation:
        stmt = stmt.where(CodeRef.relation == relation)
    rows = db.scalars(stmt).all()
    for r in rows:
        db.delete(r)
    db.commit()
    return len(rows)


def linked_work_for_path(db: Session, project_id: str, path: str) -> dict:
    """Items and requests explicitly linked to `path` (both directions of the bridge, code
    side). Titles/status resolved live so the caller gets display-ready rows."""
    refs = db.scalars(
        select(CodeRef).where(CodeRef.project_id == project_id, CodeRef.path == path.strip())
    ).all()
    items, requests = [], []
    for r in refs:
        if r.ref_type == "item":
            it = db.get(Item, r.ref_id)
            if it is not None:
                items.append({"id": it.id, "title": it.title, "status": it.status, "relation": r.relation})
        else:
            rq = db.get(Request, r.ref_id)
            if rq is not None:
                requests.append(
                    {"id": rq.id, "title": rq.title, "type": rq.type, "status": rq.status, "relation": r.relation}
                )
    return {"items": items, "requests": requests}


def code_for_ref(db: Session, project_id: str, ref_id: str, ref_type: str | None = None) -> list[dict]:
    """The code paths an item/request is linked to (the work side of the bridge). Each row
    carries the described node when it exists, else node=null (dangling link)."""
    rtype, _obj = _resolve_ref(db, project_id, ref_id, ref_type)
    refs = db.scalars(
        select(CodeRef).where(
            CodeRef.project_id == project_id,
            CodeRef.ref_type == rtype,
            CodeRef.ref_id == str(ref_id).strip(),
        )
    ).all()
    out = []
    for r in refs:
        node = db.scalars(
            select(CodeNode).where(CodeNode.project_id == project_id, CodeNode.path == r.path)
        ).first()
        out.append({"path": r.path, "relation": r.relation, "node": node_dict(node) if node else None})
    return out


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def search_code(
    db: Session, query: str, project_id: str, top_k: int = 5
) -> list[tuple[CodeNode, float]]:
    """Semantic search over node summaries, ranked by cosine similarity (best first).
    pgvector `<=>` in prod; Python cosine fallback on SQLite — mirrors search_memory."""
    qvec = get_embedder().embed(query)

    if not settings.is_sqlite:
        sql = text(
            """
            SELECT id, (embedding <=> (:qv)::vector) AS distance
            FROM code_nodes
            WHERE embedding IS NOT NULL AND project_id = :pid
            ORDER BY distance ASC
            LIMIT :k
            """
        )
        rows = db.execute(sql, {"qv": _vector_literal(qvec), "k": top_k, "pid": project_id}).all()
        out: list[tuple[CodeNode, float]] = []
        for row in rows:
            node = db.get(CodeNode, row.id)
            if node is not None:
                out.append((node, 1.0 - float(row.distance)))
        return out

    scored = [
        (n, cosine_similarity(qvec, n.embedding))
        for n in list_nodes(db, project_id)
        if n.embedding is not None
    ]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:top_k]


def export_graph(db: Session, project_id: str) -> dict:
    """Portable dump of the project's code graph (for backup / migration parity)."""
    return get_code_map(db, project_id)


def area_matches(area: str, path: str) -> bool:
    """Does a reservation `area` cover the code node at `path`? (PRD-20 D4)

    **Deliberately NOT `clustering._match`, and that is the point.** The divvy and the graph
    want the same vocabulary and the OPPOSITE failure preference. Over-matching is *safe* for
    collision avoidance — you over-block, you never collide — and is a *lie* for presence,
    because it claims an agent is somewhere it is not. `_match`'s third rule treats any two
    paths sharing a parent directory as related, which measured against the live graph turns
    one file area into 25 nodes and `web/src/features/*` into 41. PRD-20 section 5.1 originally
    claimed reusing `_match` meant the two "cannot drift"; that was backwards, and this
    function is the correction.

    Three rules, in order:
      1. exact
      2. glob — the AREA is the pattern (`web/src/features/*`), never the path
      3. directory prefix — an area naming a directory covers everything beneath it

    `::`-aware throughout: a symbol node `items.py::claim_next` is covered by anything that
    covers `items.py`. There are zero symbol nodes on the live graph today (GRPH-382), so this
    is written now rather than discovered as a gap the day the first one appears.
    """
    area = (area or "").strip()
    path = (path or "").strip()
    if not area or not path:
        return False
    if area == path:
        return True

    # A symbol is covered by whatever covers the file it lives in.
    #
    # This deliberately does NOT also require `"::" not in area`. That guard was here first and
    # a sabotage pass proved it guards nothing: an area carrying `::` cannot match the stripped
    # file path under any of the three rules below, so the recursion is already a no-op for it
    # and `a.py::x` still does not cover `a.py::y`. A condition that cannot change an outcome
    # reads as protection and provides none, which is worse than its absence.
    if "::" in path and area_matches(area, path.split("::", 1)[0]):
        return True

    if fnmatch.fnmatch(path, area):
        return True
    return path.startswith(area.rstrip("/") + "/")


# ── structural queries (PRD-20 D8) ────────────────────────────────────────────
#
# The three questions a graph exists to answer — which node is load-bearing, which things move
# together, and what connects these two — over data we already hold. Reads only: no new table,
# no background job, no write path.
#
# **Every one of these is deterministic**, and that is a requirement rather than a nicety. The
# layout is hand-written precisely because stability across renders is what lets a person keep
# their place, and a structural overlay that reshuffled underneath it would give that away for
# nothing. So: candidate sets are sorted, ties break on the id, and traversal visits neighbours
# in sorted order. Nothing here consults a random seed or dict insertion order.


def _graph_paths(db: Session, project_id: str, edge_types: list[str] | None):
    """(sorted node ids, filtered edges). Ids include edge endpoints that were never described,
    because an undescribed file that many modules import is exactly the load-bearing node this
    is meant to surface — dropping it would hide the answer."""
    nodes = list_nodes(db, project_id)
    edges = list_edges(db, project_id)
    if edge_types:
        wanted = set(edge_types)
        edges = [e for e in edges if e.type in wanted]
    ids = {n.path for n in nodes}
    for e in edges:
        ids.add(e.src)
        ids.add(e.dst)
    return sorted(ids), edges


def hubs(
    db: Session,
    project_id: str,
    *,
    edge_types: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Nodes ranked by INBOUND degree — "what would break the most if this changed".

    Inbound rather than total: a file importing forty things is complicated, while a file that
    forty things import is load-bearing, and only the second is the single point of failure the
    question is really asking about. Both counts are returned so a caller can see the
    difference rather than take our word for it.
    """
    ids, edges = _graph_paths(db, project_id, edge_types)
    incoming: dict[str, int] = {p: 0 for p in ids}
    outgoing: dict[str, int] = {p: 0 for p in ids}
    for e in edges:
        if e.dst in incoming:
            incoming[e.dst] += 1
        if e.src in outgoing:
            outgoing[e.src] += 1
    described = {n.path: n for n in list_nodes(db, project_id)}
    ranked = sorted(ids, key=lambda p: (-incoming[p], -outgoing[p], p))
    return [
        {
            "path": p,
            "inbound": incoming[p],
            "outbound": outgoing[p],
            "kind": described[p].kind if p in described else None,
            "described": p in described,
        }
        for p in ranked[: max(0, limit)]
    ]


def components(
    db: Session,
    project_id: str,
    *,
    edge_types: list[str] | None = None,
) -> list[dict]:
    """Connected components — "which things move together".

    **Connected components only. Modularity is deliberately out.** Every practical community
    method (Louvain, Leiden) is stochastic and order-dependent, which contradicts the reason
    this project hand-writes its layout instead of adopting d3-force. Components are O(V+E),
    exactly reproducible, and answer the question adequately at this graph size. If modularity
    is ever wanted it has to arrive with a named algorithm, a fixed seed, and a documented node
    ordering — not as an unqualified "cluster the big one".

    Ordered largest-first with ties broken by the first member, and members sorted. `anchor` is
    the highest-inbound member: the label a collapsed component wears in the galaxy view (D9).
    """
    ids, edges = _graph_paths(db, project_id, edge_types)
    adj: dict[str, list[str]] = {p: [] for p in ids}
    for e in edges:
        if e.src in adj and e.dst in adj and e.src != e.dst:
            adj[e.src].append(e.dst)
            adj[e.dst].append(e.src)
    for p in adj:
        adj[p].sort()

    inbound: dict[str, int] = {p: 0 for p in ids}
    for e in edges:
        if e.dst in inbound:
            inbound[e.dst] += 1

    seen: set[str] = set()
    found: list[list[str]] = []
    for start in ids:  # already sorted, so the walk order is fixed
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        member: list[str] = []
        while stack:
            cur = stack.pop()
            member.append(cur)
            for nb in adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        found.append(sorted(member))

    found.sort(key=lambda m: (-len(m), m[0]))
    # Anchor ties break on the id so a component's label cannot change between identical reads.
    return [
        {"anchor": sorted(m, key=lambda p: (-inbound[p], p))[0], "size": len(m), "members": m}
        for m in found
    ]


def path(
    db: Session,
    project_id: str,
    a: str,
    b: str,
    *,
    edge_types: list[str] | None = None,
) -> dict:
    """Shortest path between two code paths — "what connects these two".

    **Traversed UNDIRECTED, reported with direction.** Asking what connects two files is a
    reachability question, and answering it directionally would report "not connected" for two
    modules that plainly are, merely because the arrows between them point the wrong way. Each
    hop carries `forward`, so a reader still sees which way the real edge runs.

    Returns `found: False` rather than raising, and distinguishes no-route from an endpoint
    that is not in the graph at all (`missing`). "Nothing connects these" and "you named a file
    I have never heard of" are different answers, and a caller acts differently on each.
    """
    a, b = a.strip(), b.strip()
    ids, edges = _graph_paths(db, project_id, edge_types)
    id_set = set(ids)
    missing = [p for p in (a, b) if p not in id_set]
    if missing:
        return {"a": a, "b": b, "found": False, "missing": missing, "hops": []}
    if a == b:
        return {"a": a, "b": b, "found": True, "missing": [], "hops": []}

    adj: dict[str, list[tuple[str, str, bool]]] = {p: [] for p in ids}
    for e in edges:
        if e.src in adj and e.dst in adj:
            adj[e.src].append((e.dst, e.type, True))
            adj[e.dst].append((e.src, e.type, False))
    for p in adj:
        adj[p].sort()  # deterministic tie-break between equally short routes

    prev: dict[str, tuple[str, str, bool]] = {}
    seen = {a}
    frontier = [a]
    while frontier and b not in seen:
        nxt: list[str] = []
        for cur in frontier:
            for dst, etype, forward in adj[cur]:
                if dst in seen:
                    continue
                seen.add(dst)
                prev[dst] = (cur, etype, forward)
                nxt.append(dst)
        frontier = nxt

    if b not in prev:
        return {"a": a, "b": b, "found": False, "missing": [], "hops": []}

    hops = []
    cur = b
    while cur != a:
        src, etype, forward = prev[cur]
        hops.append({"src": src, "dst": cur, "type": etype, "forward": forward})
        cur = src
    hops.reverse()
    return {"a": a, "b": b, "found": True, "missing": [], "hops": hops}


def analysis(
    db: Session,
    project_id: str,
    *,
    edge_types: list[str] | None = None,
    limit: int = 10,
    a: str | None = None,
    b: str | None = None,
) -> dict:
    """The three structural answers in one read, for the graph view's overlay panel."""
    return {
        "hubs": hubs(db, project_id, edge_types=edge_types, limit=limit),
        "components": components(db, project_id, edge_types=edge_types),
        "path": path(db, project_id, a, b, edge_types=edge_types) if a and b else None,
    }
