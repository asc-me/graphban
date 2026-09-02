"""Agent chat — retrieval-grounded, backed by the configured ChatModel provider.

The router assembles context (project state + top-k memory shards); the provider
turns it into a reply. The default stub provider composes a deterministic answer
offline; Ollama/Anthropic providers generate one.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import errors
from app.db import get_db
from app.models import ApiKey, User
from app.providers import iter_reply
from app.schemas import (
    ChatIn,
    ChatOut,
    CodeAnswerOut,
    CodeForRefRow,
    CodeHit,
    CodeMapOut,
    CodeNeighborsOut,
    CodeNodeOut,
    CodeRefIn,
    CodeRefOut,
    CodeUnlinkIn,
    ShardHit,
    ShardOut,
)
from app.security import authz
from app.security.deps import get_current_user, get_user_or_agent_key
from app.services import agent_chat as agent_chat_svc
from app.services import code_graph as code_svc
from app.services import galaxy as galaxy_svc
from app.services import platform as platform_svc
from app.services.projects import resolve_project_id

router = APIRouter(prefix="/agent", tags=["agent"])


def _principal_readable_pid(db: Session, principal, project_id: str | None) -> str:
    """`_readable_pid`, for a caller that may be a user OR an agent key (GRPH-405).

    Same shape and same guarantee: the fallback for an omitted id is bounded to the
    caller's own projects, and the resolved project must be readable by *that* caller. A
    key is scoped by `authz.key_readable_ids`, which already refuses another tenant's
    project — so widening the credential does not widen the reach.
    """
    if isinstance(principal, ApiKey):
        readable = authz.key_readable_ids(db, principal)
        pid = resolve_project_id(db, project_id, allowed_ids=readable)
        if pid not in readable:
            # 404 rather than 403, matching `require_readable`: a project the caller
            # cannot see must be indistinguishable from one that does not exist.
            raise HTTPException(404, "project not found")
        return pid
    return _readable_pid(db, principal, project_id)


def _readable_pid(db: Session, user: User, project_id: str | None) -> str:
    """Resolve a project for a scoped read and enforce membership (AL-71).

    The fallback for an omitted id is bounded to the caller's own projects, and the
    resolved project must be readable — so these agent/code reads can't be aimed at
    another tenant's data by naming (or omitting) a project_id."""
    readable = authz.readable_project_ids(db, user.id)
    pid = resolve_project_id(db, project_id, allowed_ids=readable)
    authz.require_readable(db, user.id, pid)
    return pid


CODE_SYSTEM = (
    "You are Graphban's codebase agent. Answer questions about the code's structure and "
    "relations using ONLY the supplied code graph — the described modules/files/symbols and "
    "their imports / calls / ownership edges. Cite paths. If the graph doesn't cover what's "
    "asked, say so plainly and suggest the coding agent run describe_code for that area — do "
    "not guess at code you weren't given."
)


@router.post("/chat", response_model=ChatOut)
def chat(body: ChatIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pid = _readable_pid(db, user, body.project_id)
    out = agent_chat_svc.reply(db, project_id=pid, message=body.message)
    return ChatOut(
        reply=out["reply"],
        shards=[ShardHit(shard=ShardOut.model_validate(s), score=round(sc, 4))
                for s, sc in out["hits"]],
    )


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


@router.post("/chat/stream")
def chat_stream(body: ChatIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Server-Sent Events: a `shards` event, then `delta` events, then `done` (F3)."""
    pid = _readable_pid(db, user, body.project_id)
    chat, context, hits = agent_chat_svc.assemble(db, project_id=pid, message=body.message)
    shards = [
        ShardHit(shard=ShardOut.model_validate(s), score=round(sc, 4)).model_dump(mode="json")
        for s, sc in hits
    ]

    def gen():
        yield _sse("shards", json.dumps(shards))
        for piece in iter_reply(chat, system=agent_chat_svc.SYSTEM, context=context,
                                question=body.message):
            yield _sse("delta", json.dumps({"text": piece}))
        yield _sse("done", "{}")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── code-graph agent: the connected-LLM consumer of the code structure graph ──

CODE_TOP_K = 5
CODE_EXPAND = 3  # how many top hits to expand with their edges + touching items


def _build_code_context(db: Session, project_id: str, hits) -> str:
    """Ground the ChatModel in the code graph: the semantically-matched nodes, then the
    dependency edges and touching work around the strongest hits — so the model can answer
    'what depends on X' from real edges instead of prose."""
    # COUNTS, not the graph. This built the entire map — every node with its summary, every
    # edge — to print two integers, on a path that runs per chat message. Two COUNT queries
    # answer the same question without materialising a row (GRPH-146, and the read GRPH-55
    # names first).
    parts = [
        f"Code graph: {code_svc.count_nodes(db, project_id)} described nodes, "
        f"{code_svc.count_edges(db, project_id)} edges."
    ]
    if not hits:
        parts.append(
            "No code nodes matched the question — the relevant area may not be described yet."
        )
        return "\n".join(parts)

    parts.append("Relevant code (semantic match):")
    parts += [
        f"  · ({score:.2f}) [{node.kind}] {node.path} — {node.summary}"
        for node, score in hits
    ]

    for node, _ in hits[:CODE_EXPAND]:
        nb = code_svc.neighbors(db, project_id, node.path)
        if nb["outgoing"]:
            parts.append(
                f"{node.path} depends on: "
                + ", ".join(f"{e['dst']} ({e['type']})" for e in nb["outgoing"])
            )
        if nb["incoming"]:
            parts.append(
                f"{node.path} is used by: "
                + ", ".join(f"{e['src']} ({e['type']})" for e in nb["incoming"])
            )
        if nb["items_touching"]:
            parts.append(
                f"Work touching {node.path}: "
                + ", ".join(f"{t['id']} {t['title']}" for t in nb["items_touching"][:3])
            )
        linked = nb["linked_items"] + nb["linked_requests"]
        if linked:
            parts.append(
                f"Linked work on {node.path}: "
                + ", ".join(f"{t['id']} {t['relation']} ({t['title']})" for t in linked[:4])
            )
    return "\n".join(parts)


def _code_hits(db: Session, message: str, project_id: str):
    return code_svc.search_code(db, message, project_id=project_id, top_k=CODE_TOP_K)


@router.get("/code/map", response_model=CodeMapOut)
def code_map(project_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The project's code graph — every described node and typed edge. Powers the graph view.

    **Deliberately unbounded, and the only caller that should be** (GRPH-146). The view draws
    the whole graph with a force-directed layout; a page would produce a picture of an
    arbitrary fifth of the codebase with no way to tell from looking. The MCP tool bounds by
    default because an agent pays for it in context and can ask for the next page; a canvas
    cannot. If this becomes a problem it needs viewport culling, not a limit parameter.
    """
    pid = _readable_pid(db, user, project_id)
    payload = code_svc.get_code_map(db, pid)
    # D4: the arrows out ride along with the map the graph already fetches, rather than a
    # second request the view would have to sequence against the first.
    payload["outbound"] = galaxy_svc.outbound_stubs(db, pid)
    return payload


@router.get("/code/health")
def code_health(
    project_id: str | None = None,
    limit: int = 40,
    db: Session = Depends(get_db),
    principal=Depends(get_user_or_agent_key),
):
    """Is the code graph still true (GRPH-404) — coverage, stale nodes open work still claims,
    and touchpoints that resolve to nothing.

    **A JWT or an agent key** (GRPH-405). The key half is not a widening: every input this
    computes is already readable with an agent key — `get_code_map` returns each node's
    `fresh` flag and `get_backlog(fields="full")` returns `touchpoints` — so an agent could
    always join the two and arrive at these numbers. The gate withheld the convenience of
    the answer and charged two round trips for it, which is the wrong thing to charge an
    agent deciding whether the graph is worth trusting before a refactor.

    `/api/fleet/presence` stays JWT-only and this is not a precedent for it: presence names
    which human is editing which file, and its inputs are not already agent-readable.

    **REST only, no MCP tool, and the reason is the manifest ceiling rather than principle.**
    Folding `health` into `graph_query` as a fourth mode cost ~20 tokens more than the surface
    had left, after trimming everything in that entry that could go. The footprint guard's own
    procedure says trim before raising, and the trimming was done; a sixth raise for a
    maintenance read that a HUMAN runs before a describe pass is the wrong thing to spend the
    last of that budget on. When progressive disclosure lands (GRPH-48 / GRPH-146) this can join
    the manifest for what it actually costs.

    Retires nothing, and reports `ever_described` so "nothing is stale" and "nothing has ever
    been described" are distinguishable answers.
    """
    pid = _principal_readable_pid(db, principal, project_id)
    return code_svc.health(db, pid, limit=limit)


@router.get("/code/analysis")
def code_analysis(
    project_id: str | None = None,
    edge_types: str | None = None,
    limit: int = 10,
    a: str | None = None,
    b: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Structural answers over the code graph — hubs, components, and optionally a path between
    two nodes (PRD-20 D8). Read-only, and deterministic for a given graph. `edge_types` is a
    comma-separated subset of `code_graph.EDGE_TYPES`."""
    pid = _readable_pid(db, user, project_id)
    types = [t.strip() for t in edge_types.split(",") if t.strip()] if edge_types else None
    if types:
        unknown = [t for t in types if t not in code_svc.EDGE_TYPES]
        if unknown:
            # Silently dropping an unknown type would answer a narrower question than the one
            # asked and return it looking like a real result — the absence-reads-as-clean
            # failure this codebase keeps naming.
            raise HTTPException(422, f"unknown edge type(s): {', '.join(sorted(unknown))}")
    return code_svc.analysis(db, pid, edge_types=types, limit=limit, a=a, b=b)


@router.get("/code/neighbors", response_model=CodeNeighborsOut)
def code_neighbors(path: str, project_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The relations around one code path — in/out edges, work touching it, and items/requests
    explicitly linked to it. Powers the node inspector."""
    pid = _readable_pid(db, user, project_id)
    return code_svc.neighbors(db, pid, path)


@router.get("/code/for", response_model=list[CodeForRefRow])
def code_for_ref(ref_id: str, ref_type: str | None = None, project_id: str | None = None,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The code paths a tracker item/request is linked to — the work→code direction of the
    bridge. Powers the "Linked code" section on an item/request."""
    pid = _readable_pid(db, user, project_id)
    try:
        return code_svc.code_for_ref(db, pid, ref_id, ref_type)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/code/link", response_model=CodeRefOut, status_code=201)
def code_link(body: CodeRefIn, db: Session = Depends(get_db),
              user: User = Depends(get_current_user), project_id: str | None = None):
    """Link a tracker item/request to a code path (the explicit bridge)."""
    pid = resolve_project_id(db, project_id, allowed_ids=authz.writable_project_ids(db, user.id))
    authz.require_writable(db, user.id, pid)
    try:
        ref = code_svc.link_code(
            db, project_id=pid, ref_id=body.ref_id, path=body.path,
            relation=body.relation, ref_type=body.ref_type,
        )
    except errors.NotFound as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return code_svc.ref_dict(ref)


@router.post("/code/unlink")
def code_unlink(body: CodeUnlinkIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user), project_id: str | None = None):
    """Remove a link from an item/request to a code path."""
    pid = resolve_project_id(db, project_id, allowed_ids=authz.writable_project_ids(db, user.id))
    authz.require_writable(db, user.id, pid)
    removed = code_svc.unlink_code(db, project_id=pid, ref_id=body.ref_id, path=body.path, relation=body.relation)
    return {"removed": removed}


@router.post("/code", response_model=CodeAnswerOut)
def code_chat(body: ChatIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Ask about the codebase. The connected LLM answers grounded in the code graph the
    coding agent described (nodes + typed edges), never from an actual checkout."""
    pid = _readable_pid(db, user, body.project_id)
    hits = _code_hits(db, body.message, pid)
    context = _build_code_context(db, pid, hits)
    reply = platform_svc.chat_model_for(db, pid).chat(system=CODE_SYSTEM, context=context, question=body.message)
    return CodeAnswerOut(
        reply=reply,
        nodes=[CodeHit(node=CodeNodeOut.model_validate(n), score=round(sc, 4)) for n, sc in hits],
    )


@router.post("/code/stream")
def code_chat_stream(body: ChatIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """SSE variant: a `nodes` event (the code the answer is grounded in), then `delta`
    events, then `done`. Mirrors /agent/chat/stream."""
    pid = _readable_pid(db, user, body.project_id)
    hits = _code_hits(db, body.message, pid)
    context = _build_code_context(db, pid, hits)
    nodes = [
        CodeHit(node=CodeNodeOut.model_validate(n), score=round(sc, 4)).model_dump(mode="json")
        for n, sc in hits
    ]

    chat = platform_svc.chat_model_for(db, pid)  # resolve while the request DB session is open

    def gen():
        yield _sse("nodes", json.dumps(nodes))
        for piece in iter_reply(chat, system=CODE_SYSTEM, context=context, question=body.message):
            yield _sse("delta", json.dumps({"text": piece}))
        yield _sse("done", "{}")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
