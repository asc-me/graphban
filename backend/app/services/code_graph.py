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

import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.embeddings import cosine_similarity, get_embedder, safe_embed
from app.errors import NotFound
from app.models import CodeEdge, CodeNode, CodeRef, Item, Request
from app.services import items as items_svc
from app.services.clustering import _match

# What each kind MEANS (GRPH-382). The enum existed without definitions, so callers guessed —
# and the live graph guessed the same way 119 times out of 123, labelling every file a module
# and never once producing a symbol. A vocabulary nobody can look up is one nobody applies
# consistently.
#
#   module — a PACKAGE or DIRECTORY: `backend/app/services`, `web/src/lib`. A thing that
#            CONTAINS other things, and therefore has no file extension.
#   file   — ONE SOURCE FILE: `backend/app/services/items.py`.
#   symbol — a NAMED THING INSIDE a file, written `path::name`:
#            `backend/app/services/items.py::claim_next`.
#
# The distinction is not cosmetic. PRD-20 D5 encodes kind in node fill and argues presence
# clouds are safe because they use a different channel; on a graph that is 97% one value, that
# argument defends something that is not there.
NODE_KINDS = ["module", "file", "symbol"]

# Extensions that make a path a FILE rather than a package. Deliberately a suffix check and not
# a filesystem probe: the server holds no checkout, and `describe_code`'s caller is the only
# thing that does — which is why the server can validate the SHAPE of a claim but never its
# truth.
_SOURCE_SUFFIXES = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".rb", ".java",
    ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".sh", ".sql",
    ".css", ".scss", ".html", ".vue", ".svelte", ".toml", ".yaml", ".yml", ".json", ".md",
)


def kind_for_path(path: str) -> str:
    """The kind a path's SHAPE implies: `path::name` is a symbol, a suffixed path is a file,
    anything else is a package.

    Used to validate a caller's claim at write time, and to reclassify the rows written before
    the vocabulary above was written down.
    """
    p = (path or "").strip()
    if "::" in p:
        return "symbol"
    if p.lower().endswith(_SOURCE_SUFFIXES):
        return "file"
    return "module"


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
    # Was: `if kind not in NODE_KINDS: kind = "file"`.
    #
    # Two different failures were being handled as one. An UNRECOGNISED kind is invalid input
    # and now refuses — silently rewriting it to `file` is why a caller passing junk got a
    # plausible node back and never learned. A kind that CONTRADICTS the path's shape is the
    # common case (119 of 123 live nodes are files labelled `module`), and refusing it would
    # break every agent currently describing code. So it is corrected to the shape the path
    # implies, and `describe_code` REPORTS the correction — the data gets right immediately and
    # the caller is told, which is the part silent coercion never did.
    if kind not in NODE_KINDS:
        raise ValueError(f"unknown kind {kind!r} for {path!r}; valid: {', '.join(NODE_KINDS)}")
    kind = kind_for_path(path)
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
    corrected: list[dict] = []
    for n in nodes:
        path = str(n.get("path", "")).strip()
        if not path:
            continue
        asked = str(n.get("kind", "file"))
        upsert_node(
            db,
            project_id=project_id,
            path=path,
            kind=asked,
            name=str(n.get("name", "")),
            lang=str(n.get("lang", "")),
            summary=str(n.get("summary", "")),
            content_hash=str(n.get("content_hash", "")),
        )
        # Reported, not silent (GRPH-382). Coercing without saying so is how the live graph
        # became 97% one value with nobody noticing; a caller that learns it mislabelled can
        # stop doing it, and one that is never told cannot.
        implied = kind_for_path(path)
        if asked in NODE_KINDS and asked != implied:
            corrected.append({"path": path, "asked": asked, "stored": implied})
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
        # Empty when every kind agreed with its path. Non-empty means "these were stored as
        # something other than what you asked for, and here is which" — the signal the old
        # silent coercion never gave.
        "kind_corrections": sorted(corrected, key=lambda c: c["path"]),
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
