import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.providers import iter_reply
from app.schemas import (
    GrillApplyIn,
    GrillDeferIn,
    RebaselineIn,
    GrillApplyOut,
    GrillIn,
    PrdAiIn,
    PrdAiOut,
    PrdCreate,
    PrdLinkIn,
    CloseIn,
    PromoteIn,
    PrdOut,
    PrdSummary,
    PrdUpdate,
    PrdVersionIn,
    PrdVersionOut,
)
from app.security import authz
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import platform as platform_svc
from app.services import prds as prd_svc

router = APIRouter(prefix="/prds", tags=["prds"])


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _require_writable_prd(db: Session, user: User, prd_id: str) -> None:
    """Load-and-guard for PRD mutations: 404 unknown, 404/403 per membership."""
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise HTTPException(404, "prd not found")
    authz.require_writable(db, user.id, prd.project_id, "prd")


def _require_readable_prd(db: Session, user: User, prd_id: str):
    """Load-and-read-guard for PRD reads (tenant isolation, AL-70)."""
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise HTTPException(404, "prd not found")
    authz.require_readable(db, user.id, prd.project_id, "prd")
    return prd


@router.get("", response_model=list[PrdSummary])
def list_prds(project_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    return prd_svc.list_prds(db, project_id=project_id)


@router.post("", response_model=PrdOut, status_code=201)
def create_prd(body: PrdCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, body.project_id)
    return prd_svc.create_prd(
        db, title=body.title, template=body.template, project_id=body.project_id, body=body.body,
    )


@router.get("/{prd_id}/coverage")
def prd_coverage(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.coverage(db, prd)


@router.get("/{prd_id}/grill")
def prd_grill_state(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The grill as the SERVER knows it (AL-296) — no client transcript involved.

    This is the endpoint that proves the item: before it, "has this PRD been grilled?"
    was only answerable by whoever happened to be holding the conversation."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.grill_state(db, prd.id)


@router.post("/{prd_id}/grill/defer")
def prd_grill_defer(prd_id: str, body: GrillDeferIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Deliberately leave a dimension open (AL-298).

    Deferring is the author's decision, not the model's inference, so it gets an explicit
    route — and on a stub instance, which cannot detect a deferral in prose, it is the
    ONLY way to record one. `classify_grill` never downgrades it afterwards."""
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise HTTPException(404, "prd not found")
    authz.require_writable(db, user.id, prd.project_id, "prd")
    try:
        prd_svc.set_dimension(db, prd.id, body.dimension, "deferred", note=body.reason,
                              graded_by="author")
    except ValueError as e:
        raise HTTPException(422, str(e))
    prd_svc.sync_status(db, prd)  # a deferral can be what completes the grill
    events_svc.record_user(db, user, action="grill_defer", target_type="prd",
                           target_id=prd.id, project_id=prd.project_id,
                           meta={"dimension": body.dimension, "reason": body.reason})
    return prd_svc.grill_state(db, prd.id)


@router.post("/{prd_id}/rebaseline", response_model=PrdOut)
def request_rebaseline(prd_id: str, body: RebaselineIn, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Ask for new frozen intent (AL-241). Re-opens the grill; does NOT approve anything.

    The existing baseline stays governing until a new one is earned, so work in flight is
    still measured against something real while the spec is being re-interrogated."""
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise HTTPException(404, "prd not found")
    authz.require_writable(db, user.id, prd.project_id, "prd")
    try:
        return prd_svc.request_rebaseline(db, prd, reason_type=body.reason_type,
                                          reason=body.reason, requested_by=f"user:{user.id}")
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/{prd_id}/intent-diff")
def prd_intent_diff(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """What a rebaseline would change, section by section (GRPH-317).

    PRD-12: without this the approver "ratifies a decision already made in chat without
    seeing its effect on the spec." Approval is the grill, so this belongs beside it."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.intent_diff(db, prd)


@router.get("/{prd_id}/completeness")
def prd_completeness(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """What the governing baseline demands that has nothing delivered (GRPH-251).

    Distinct from `/coverage`, which asks whether the LIVING body is decomposed. This asks
    whether the AGREED spec was delivered, and only it can answer completeness — classifying
    work that exists can never surface work that was never done."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.completeness(db, prd)


@router.get("/{prd_id}/scope-drift")
def prd_scope_drift(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Mechanical scope drift — no LLM, no opinion (GRPH-243).

    The half of drift that is countable, so it works on a stub instance with no chat
    provider. `total` is preserved across a rebaseline rather than reset."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.scope_drift(db, prd)


@router.post("/{prd_id}/close")
def prd_close(prd_id: str, payload: CloseIn, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """Close a PRD — the terminal state (GRPH-244).

    409 when intent is left undispositioned: the request is well-formed and permitted, the
    PRD simply is not accountable for yet. Close gates on disposition, never on delivery."""
    _require_writable_prd(db, user, prd_id)
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise HTTPException(404, "prd not found")
    try:
        return prd_svc.close_prd(db, prd, dispositions=payload.dispositions,
                                 closed_by=f"user:{user.id}", verdict=payload.verdict,
                                 judge_reachable=payload.judge_reachable)
    except (prd_svc.CloseRefused, prd_svc.PrdClosed) as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/{prd_id}/close-report")
def prd_close_report(prd_id: str, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Delivered work against ORIGINAL intent (GRPH-245).

    Distinct from every other surface, which read the GOVERNING baseline. Reading the
    governing one here would make the report agree with itself by construction — that is
    where the spec ended up. No verdict, no score: the counts describe what happened and
    the judgement belongs to the reader."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.close_report(db, prd)


@router.get("/{prd_id}/close-readiness")
def prd_close_readiness(prd_id: str, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Whether this PRD can close, in which mode, and what is outstanding (GRPH-311/244)."""
    prd = _require_readable_prd(db, user, prd_id)
    out = prd_svc.close_readiness(db, prd)
    out["undispositioned"] = prd_svc.dropped_intent(db, prd) if out["can_close"] else []
    return out


@router.get("/{prd_id}/classifications")
def prd_classifications(prd_id: str, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """The platform judge's read on each completed item (GRPH-249).

    serves / enables / unrelated / undecidable, stamped with the baseline judged against.
    Stale rows recompute on read — that is the lazy half of the staleness design, so a
    reader is never shown numbers that are quietly out of date."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.classifications(db, prd)


@router.get("/{prd_id}/evidence")
def prd_evidence(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """What the delivered work offers as proof, bound to the intent it supports (GRPH-250).

    Two independent signals: receipts split by whether anyone but their author could check
    them, and structural corroboration of `touchpoints` against the code graph. No score —
    a weighted number would be an opinion wearing a measurement's clothes."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.evidence_rollup(db, prd)


@router.get("/{prd_id}/verdicts")
def prd_verdicts(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Sign-off verdicts recorded against this PRD (GRPH-253). Claims with provenance,
    never truth — `self_signed` and the citations are what make them arguable."""
    prd = _require_readable_prd(db, user, prd_id)
    return [
        {"id": v.id, "outcome": v.outcome, "reasoning": v.reasoning,
         "citations": v.citations or [], "signed_by": v.signed_by,
         "baseline_version": v.baseline_version, "self_signed": v.self_signed,
         "self_signed_items": v.self_signed_items or []}
        for v in prd_svc.verdicts(db, prd)
    ]


@router.get("/{prd_id}/lineage")
def prd_lineage(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Where this PRD's dropped intent came from and went (GRPH-246)."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.lineage(db, prd)


@router.post("/{prd_id}/promote")
def prd_promote(prd_id: str, payload: PromoteIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Promote dropped intent into a backlog item or a successor PRD (GRPH-246).

    422 rather than a silent no-op when the named sections have delivered work: writing a
    lineage record that says something was dropped when it shipped would corrupt the one
    artifact this feature exists to make trustworthy."""
    _require_writable_prd(db, user, prd_id)
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise HTTPException(404, "no such PRD")
    try:
        if payload.target == "prd":
            out = prd_svc.promote_to_prd(db, prd, payload.sections, title=payload.title)
            return {"target": "prd", "id": out.key, "promoted_sections": out.promoted_sections}
        if len(payload.sections) != 1:
            raise ValueError("promoting to an item takes exactly one section")
        item = prd_svc.promote_to_item(db, prd, payload.sections[0], title=payload.title)
        return {"target": "item", "id": item.key, "promoted_sections": [item.prd_section]}
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/{prd_id}/drift")
def prd_drift(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Structural divergence from the governing baseline (AL-240).

    The mechanical half of drift — no chat provider required. `governed: false` when the
    PRD has never been approved, because "no drift" and "nothing to drift from" are
    different facts and reporting the second as the first is the misleading green this
    feature exists to stop."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.baseline_drift(db, prd)


@router.get("/{prd_id}/baselines", response_model=list[PrdVersionOut])
def prd_baseline_chain(prd_id: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """The whole chain, oldest first — reading it is how you tell a spec that was
    corrected once from one that kept moving."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.baseline_chain(db, prd.id)


@router.post("/{prd_id}/decompose")
def decompose_prd(prd_id: str, create: bool = False, include_prose: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise HTTPException(404, "prd not found")
    if create:  # proposing tasks is a read; creating them is a write
        authz.require_writable(db, user.id, prd.project_id, "prd")
    return prd_svc.decompose(db, prd, create=create, include_prose=include_prose)


@router.get("/{prd_id}", response_model=PrdOut)
def get_prd(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _require_readable_prd(db, user, prd_id)


@router.patch("/{prd_id}", response_model=PrdOut)
def update_prd(prd_id: str, body: PrdUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_writable_prd(db, user, prd_id)
    try:
        prd = prd_svc.update_prd(db, prd_id, **body.model_dump(exclude_unset=True))
    except prd_svc.PrdClosed as e:
        # 409: the request is well-formed and the caller is permitted — the PRD is simply
        # terminal. A 403 would read as "you may not", when the answer is "nobody may".
        raise HTTPException(409, str(e))
    except prd_svc.RebaselineExpandsScope as e:
        # 409, like ApprovalNotEarned: the request is fine, the state says no.
        raise HTTPException(409, str(e))
    except prd_svc.ApprovalNotEarned as e:
        # 409, not 422: the request is well-formed and permitted, the PRD just is not
        # there yet. A validation error would read as "you sent something malformed".
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if prd is None:
        raise HTTPException(404, "prd not found")
    return prd


@router.get("/{prd_id}/versions", response_model=list[PrdVersionOut])
def list_versions(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    prd = _require_readable_prd(db, user, prd_id)
    return prd.versions


@router.get("/{prd_id}/baseline", response_model=PrdVersionOut | None)
def prd_baseline(prd_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The agreed spec, or null if this PRD has never been approved (AL-239).

    Every PRD-12 judgement cites this — "measured against v1.0" is only meaningful if the
    thing measured against is fetchable and immutable."""
    prd = _require_readable_prd(db, user, prd_id)
    return prd_svc.baseline_of(db, prd.id)


@router.post("/{prd_id}/versions", response_model=PrdOut, status_code=201)
def snapshot(prd_id: str, body: PrdVersionIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_writable_prd(db, user, prd_id)
    prd = prd_svc.create_version(db, prd_id, note=body.note)
    if prd is None:
        raise HTTPException(404, "prd not found")
    return prd


@router.post("/{prd_id}/link", response_model=PrdOut)
def link(prd_id: str, body: PrdLinkIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_writable_prd(db, user, prd_id)
    prd = prd_svc.link_item(db, prd_id, body.item_id, add=body.add)
    if prd is None:
        raise HTTPException(404, "prd not found")
    return prd


@router.post("/{prd_id}/ai", response_model=PrdAiOut)
def ai(prd_id: str, body: PrdAiIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_writable_prd(db, user, prd_id)
    try:
        return PrdAiOut(text=prd_svc.ai_command(db, prd_id, body.command))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/{prd_id}/grill/stream")
def grill_stream(prd_id: str, body: GrillIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Interactive grill (AL-67): SSE `delta` events then `done`. Read-only — the
    proposed edits land via grill/apply → save. Light-context (PRD-grounded only)."""
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise HTTPException(404, "prd not found")
    authz.require_readable(db, user.id, prd.project_id, "prd")
    # Record the caller's side BEFORE generating (AL-296). The server owns the
    # conversation now, so an answer must survive a stream that dies mid-reply — losing
    # it would silently roll back progress toward approval.
    client_history = [m.model_dump() for m in body.history]
    if body.message:
        client_history = client_history + [{"role": "user", "text": body.message}]
    prd_svc.record_grill_turns(db, prd.id, client_history, via="human", actor=user.id)

    # Prefer what the server holds over what the caller sent: it is the same
    # conversation plus anything a second session contributed. Scoped to the current
    # interrogation (GRPH-322) — after a rebaseline the questions are about the new spec,
    # and this value is fed back into `record_grill_turns`, which appends the suffix past
    # what it is given: hand it the whole of history and every earlier turn re-appends.
    history = prd_svc.grill_history(db, prd.id, since=prd_svc.grill_window(db, prd.id))
    context = prd_svc.grill_context(prd, history)
    question = body.message or "Begin — ask your opening clarifying questions about this PRD."

    # Resolve the project's provider eagerly, while the request DB session is open.
    provider, chat = platform_svc.resolve_chat(db, prd.project_id)

    def gen():
        # Accumulate the reply as it streams so the questions the grill ASKED are
        # recorded too — AL-297 has to classify what was put to the author, which it
        # cannot do from the answers alone. Same in-generator write the assistant
        # thread route already relies on.
        parts: list[str] = []
        if provider == "stub":
            # Offline: stream the deterministic opening questions.
            for line in prd_svc._stub_command("grill", prd).splitlines(keepends=True):
                parts.append(line)
                yield _sse("delta", json.dumps({"text": line}))
        else:
            for piece in iter_reply(chat, system=prd_svc.GRILL_CHAT_SYSTEM,
                                    context=context, question=question):
                parts.append(piece)
                yield _sse("delta", json.dumps({"text": piece}))
        reply = "".join(parts).strip()
        if reply:
            prd_svc.record_grill_turns(db, prd.id, history + [{"role": "agent", "text": reply}])
        # Grade the round (AL-298). Classification is a separate call from the streamed
        # conversation on purpose: the stream is for the author to read, this is the
        # state approval derives from, and a malformed token should not cost both.
        prd_svc.classify_grill(db, prd)
        yield _sse("done", "{}")

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{prd_id}/grill/apply", response_model=GrillApplyOut)
def grill_apply(prd_id: str, body: GrillApplyIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Fold the grill transcript's decisions into a proposed PRD body AND preserve
    each decision as a candidate memory shard (AL-69). Returns the body + how many
    decisions were captured; the author reviews the shards in Memory Review and
    reviews/saves the body separately. Mutates → writable."""
    prd = prd_svc.get_prd(db, prd_id)
    if prd is None:
        raise HTTPException(404, "prd not found")
    authz.require_writable(db, user.id, prd.project_id, "prd")
    # Catch anything the stream missed — a client that grilled elsewhere, or a reply
    # that died before its turn was written. Appends only what isn't already stored.
    prd_svc.record_grill_turns(db, prd.id, [m.model_dump() for m in body.history],
                               via="human", actor=user.id)
    history = prd_svc.grill_history(db, prd.id, since=prd_svc.grill_window(db, prd.id))
    proposed = prd_svc.grill_apply(db, prd_id, history)
    shards = prd_svc.capture_grill_decisions(db, prd, history)
    prd_svc.classify_grill(db, prd)
    if shards:
        events_svc.record_user(db, user, action="grill_capture", target_type="prd",
                               target_id=prd.id, project_id=prd.project_id,
                               meta={"decisions": len(shards)})
    return GrillApplyOut(body=proposed, decisions_captured=len(shards))
