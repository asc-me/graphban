"""Retrieval-grounded project chat — the `/api/agent/chat` path (GRPH-643).

Lived in the router. Evals have to call the same function the route does, or a
green golden set would prove a helper nobody serving traffic uses. One service
layer: the router assembles auth and the payload; this module builds context
and asks the model.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import items as items_svc
from app.services import memory as mem_svc
from app.services import platform as platform_svc

SYSTEM = (
    "You are Graphban's project agent. Answer using the supplied project state and "
    "memory shards. Be concise and cite item ids where relevant."
)


def build_context(db: Session, project_id: str | None, hits) -> str:
    all_items = items_svc.list_items(db, project_id=project_id)
    by_status: dict[str, int] = {}
    for it in all_items:
        by_status[it.status] = by_status.get(it.status, 0) + 1
    parts = []
    summary = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(by_status.items()))
    parts.append(f"Project state: {summary or 'no items yet'}.")
    in_progress = [it for it in all_items if it.status == "in_progress"]
    if in_progress:
        parts.append("In progress: " + "; ".join(f"{it.id} {it.title}" for it in in_progress[:3]) + ".")
    nxt = items_svc.suggest_next(db, project_id=project_id)
    if nxt:
        parts.append(f"Suggested next: {nxt.id} — {nxt.title}.")
    if hits:
        parts.append("Relevant memory:")
        parts += [f"  · ({score:.2f}) {s.text}" for s, score in hits]
    else:
        parts.append("No matching memory shards found.")
    return "\n".join(parts)


def assemble(db: Session, *, project_id: str, message: str):
    """The retrieval half: hits + context + the chat model. Stream and reply share it."""
    hits = mem_svc.search_memory(db, message, top_k=3, project_id=project_id)
    context = build_context(db, project_id, hits)
    chat = platform_svc.chat_model_for(db, project_id)
    return chat, context, hits


def reply(db: Session, *, project_id: str, message: str) -> dict:
    """One grounded answer. The CALL `POST /api/agent/chat` makes."""
    chat, context, hits = assemble(db, project_id=project_id, message=message)
    text = chat.chat(system=SYSTEM, context=context, question=message)
    return {"reply": text, "context": context, "hits": hits}
