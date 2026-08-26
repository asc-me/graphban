"""In-app AI assistant chat surface (AL-175).

The boundary contract for the assistant: create/list threads, stream a message turn over
SSE, and approve/reject the writes it proposes. The message loop ties the epic together —
the AL-172 tool-calling session (driven by the AL-176-resolved provider for the thread),
the AL-173 tool surface, the AL-177 propose-then-approve executor, and AL-174 persistence.

The assistant's text streams token-level as `delta` events (AL-183); tool-call arguments
stay buffered for AL-180 parity, and tool activity surfaces as `tool_call` /
`tool_result` / `proposed_action`.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import QuotaExceeded
from app.models import AssistantProposedAction, User
from app.providers.toolcall import MAX_ITERS
from app.security import authz
from app.security.deps import get_current_user
from app.services import assistant as asst_svc
from app.services import assistant_approval as approval
from app.services import assistant_tools as at
from app.services import platform as platform_svc
from app.services import quotas

router = APIRouter(prefix="/assistant", tags=["assistant"])

_SYSTEM = (
    "You are Graphban's in-app assistant, scoped to a single item or PRD. Help the user "
    "brainstorm, review, and refine it. Use the tools to read context and to PROPOSE changes "
    "— proposed writes are shown to the user for approval and are NOT applied automatically. "
    "Be concise and cite item / PRD ids."
)
_TRANSCRIPT_TURNS = 6  # prior messages folded into context for continuity


# ---- schemas (local to this surface) ----
class ThreadCreateIn(BaseModel):
    project_id: str = "core"
    entity_type: str  # item | prd
    entity_id: str
    provider: str = ""
    title: str = ""


class ThreadOut(BaseModel):
    id: str
    project_id: str
    entity_type: str
    entity_id: str
    provider: str
    model: str
    title: str
    input_tokens: int = 0
    output_tokens: int = 0


class MessageOut(BaseModel):
    id: str
    seq: int
    role: str
    content: str
    tool_calls: list = []
    tool_results: list = []
    proposed_actions: list = []


class ThreadDetailOut(ThreadOut):
    messages: list[MessageOut] = []


class MessageIn(BaseModel):
    message: str


class ModelIn(BaseModel):
    provider: str
    model: str = ""


def _thread_out(t) -> dict:
    return {"id": t.id, "project_id": t.project_id, "entity_type": t.entity_type,
            "entity_id": t.entity_id, "provider": t.provider, "model": t.model, "title": t.title,
            "input_tokens": t.input_tokens, "output_tokens": t.output_tokens}


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _run_turn(session, tools):
    """One model turn, streamed when the session supports it (AL-183): forwards each text
    delta as an SSE `delta` frame and returns the completed ToolTurn. Falls back to the
    buffered `run_turn` (emitting the whole text as one delta) for sessions that can't
    stream."""
    if hasattr(session, "stream_turn"):
        gen = session.stream_turn(tools)
        try:
            while True:
                yield _sse("delta", json.dumps({"text": next(gen)}))
        except StopIteration as stop:
            return stop.value
    turn = session.run_turn(tools)
    if turn.text:
        yield _sse("delta", json.dumps({"text": turn.text}))
    return turn


# ---- threads ----
@router.post("/threads", response_model=ThreadOut)
def create_thread(body: ThreadCreateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, body.project_id)
    # default the thread to the project's active provider when the caller doesn't pick one
    provider = body.provider or platform_svc.resolve_chat(db, body.project_id).provider_id
    try:
        t = asst_svc.create_thread(db, project_id=body.project_id, entity_type=body.entity_type,
                                   entity_id=body.entity_id, provider=provider, title=body.title)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _thread_out(t)


@router.get("/threads", response_model=list[ThreadOut])
def list_threads(project_id: str = "core", entity_type: str | None = None, entity_id: str | None = None,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    return [_thread_out(t) for t in asst_svc.list_threads(
        db, project_id=project_id, entity_type=entity_type, entity_id=entity_id)]


@router.get("/threads/{thread_id}", response_model=ThreadDetailOut)
def get_thread(thread_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = asst_svc.get_thread(db, thread_id)
    if t is None:
        raise HTTPException(404, "thread not found")
    authz.require_readable(db, user.id, t.project_id)
    out = _thread_out(t)
    out["messages"] = [
        {"id": m.id, "seq": m.seq, "role": m.role, "content": m.content,
         "tool_calls": m.tool_calls or [], "tool_results": m.tool_results or [],
         "proposed_actions": m.proposed_actions or []}
        for m in t.messages
    ]
    return out


@router.post("/threads/{thread_id}/model", response_model=ThreadOut)
def set_model(thread_id: str, body: ModelIn, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    t = asst_svc.get_thread(db, thread_id)
    if t is None:
        raise HTTPException(404, "thread not found")
    authz.require_readable(db, user.id, t.project_id)
    t = asst_svc.set_thread_model(db, thread_id, provider=body.provider, model=body.model)
    return _thread_out(t)


@router.get("/providers")
def providers(project_id: str = "core", db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """The model picker's catalog for this project (AL-176)."""
    authz.require_readable(db, user.id, project_id)
    return {"providers": platform_svc.selectable_providers(db, project_id)}


# ---- the message loop (SSE) ----
def _context(db: Session, thread) -> str:
    base = asst_svc.thread_context(db, thread)
    prior = [m for m in thread.messages[:-1] if m.content][-_TRANSCRIPT_TURNS:]  # exclude the new user msg
    if prior:
        base += "\n\nConversation so far:\n" + "\n".join(f"{m.role}: {m.content}" for m in prior)
    return base


@router.post("/threads/{thread_id}/message")
def send_message(thread_id: str, body: MessageIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    thread = asst_svc.get_thread(db, thread_id)
    if thread is None:
        raise HTTPException(404, "thread not found")
    authz.require_readable(db, user.id, thread.project_id)

    asst_svc.add_message(db, thread.id, role="user", content=body.message)
    db.refresh(thread)  # pick up the just-added message for context
    provider, chat = platform_svc.resolve_chat_for(db, thread.project_id, thread.provider or "stub")
    ctx = at.ToolContext(db=db, user_id=user.id, project_id=thread.project_id,
                         entity_type=thread.entity_type, entity_id=thread.entity_id,
                         thread_id=thread.id)
    tools = at.available_tools(ctx)
    # Resolve context + session eagerly while the request DB session is open.
    session = chat.tool_session(system=_SYSTEM, context=_context(db, thread), question=body.message)

    org_id = quotas.org_id_for_project(db, thread.project_id)

    def gen():
        # Meter this turn against the org's hosted call quota (AL-75); no-op when
        # hosted_mode is off. On quota exhaustion, explain it — the thread stays readable.
        try:
            quotas.meter_call(db, org_id)
        except QuotaExceeded as e:
            yield _sse("error", json.dumps({"message": str(e)}))
            yield _sse("done", "{}")
            return
        if provider == "stub":  # graceful degradation, not a blank error
            yield _sse("notice", json.dumps({"message":
                "No AI model is active — replies use the offline stub. Pick a model above or configure one in Settings."}))

        final_text, calls, results_log, proposals = "", [], [], []
        try:
            for _ in range(MAX_ITERS):
                turn = yield from _run_turn(session, tools)  # text streams token-level here
                if turn.usage:  # meter tokens per conversation
                    thread.input_tokens += turn.usage.get("input", 0)
                    thread.output_tokens += turn.usage.get("output", 0)
                    yield _sse("usage", json.dumps({
                        "input": turn.usage.get("input", 0), "output": turn.usage.get("output", 0),
                        "thread_input": thread.input_tokens, "thread_output": thread.output_tokens}))
                if turn.text:
                    final_text = turn.text
                if not turn.wants_tools or not turn.tool_calls:
                    break
                fed_back = []
                for call in turn.tool_calls:
                    yield _sse("tool_call", json.dumps({"id": call.id, "name": call.name, "input": call.input}))
                    calls.append({"id": call.id, "name": call.name, "input": call.input})
                    result, action = approval.process_call(db, thread, user.id, call)
                    fed_back.append(result)
                    if action is not None:  # a staged write awaiting approval
                        ev = {"id": action.id, "tool": action.tool, "summary": action.summary,
                              "status": action.status}
                        proposals.append(ev)
                        yield _sse("proposed_action", json.dumps(ev))
                    else:
                        tr = {"id": result.id, "content": result.content, "is_error": result.is_error}
                        results_log.append(tr)
                        yield _sse("tool_result", json.dumps(tr))
                session.add_results(fed_back)
        except Exception as e:  # noqa: BLE001 — surface, don't sever the stream mid-thought
            yield _sse("error", json.dumps({"message": f"{type(e).__name__}: {e}"}))
        asst_svc.add_message(db, thread.id, role="assistant", content=final_text,
                             tool_calls=calls, tool_results=results_log, proposed_actions=proposals)
        yield _sse("done", "{}")

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- approve / reject a proposed write ----
def _action_or_404(db: Session, user: User, action_id: str) -> AssistantProposedAction:
    action = db.get(AssistantProposedAction, action_id)
    if action is None:
        raise HTTPException(404, "proposed action not found")
    authz.require_writable(db, user.id, action.project_id, "item")
    return action


@router.post("/actions/{action_id}/apply")
def apply_action(action_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _action_or_404(db, user, action_id)
    action, result = approval.apply(db, action_id, user_id=user.id)
    return {"status": action.status if action else "not_found", "result": result}


@router.post("/actions/{action_id}/reject")
def reject_action(action_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _action_or_404(db, user, action_id)
    action = approval.reject(db, action_id)
    return {"status": action.status if action else "not_found"}
