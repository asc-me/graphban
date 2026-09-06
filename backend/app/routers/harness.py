"""The Harness page's server half (PRD-38 PR 2).

One read that answers the page's whole question — how each vendor x model x lane x tier x task
class x size band has actually turned out, week by week, with the sample count, the sampling
skew and the cost proxy attached to every number.

Session-authenticated, like the Fleet view beside it: the caller is a person deciding whether
to change a preference, not an agent working inside one. The supervisor's own view of the same
facts is `fleet_status.measured`, which it already reads, and `gbfleet doctor` prints.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import authz
from app.security.deps import get_agent_key, get_current_user
from app.services import events as events_svc
from app.services import harness as harness_svc
from app.services import harness_rules

router = APIRouter(prefix="/harness", tags=["harness"])


@router.get("")
def harness_report(project_id: str | None = None, org_id: str | None = None,
                   window_days: int | None = None,
                   versions: str = Query("current", pattern="^(current|all)$"),
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Every cell in the window, with its weekly series.

    `versions=current` (the default) keeps the newest binary version per vendor+model and names
    the rest in `versions_seen`; `versions=all` returns every version as its own cell, because
    two versions of one harness are two things and pooling them would hide a regression inside
    an average.
    """
    if window_days is not None and (window_days < 1 or window_days > 1000):
        raise HTTPException(422, "window_days must be between 1 and 1000")
    if org_id:
        # D12: org scope is for org ADMINS. A member sees the projects they can read, one at a
        # time, which is what project scope already is.
        authz.require_org_admin(db, user.id, org_id)
        return harness_svc.org_report(db, org_id, window_days=window_days, versions=versions)
    if not project_id:
        raise HTTPException(422, "name a project_id or an org_id")
    authz.require_readable(db, user.id, project_id)
    return harness_svc.report(db, project_id, window_days=window_days, versions=versions,
                              overlay=True)


@router.get("/recommendations")
def recommendations(project_id: str, window_days: int | None = None,
                    include_seen: bool = False,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Cards the four rules produce, with what this caller has already seen (D7).

    A card the caller accepted or dismissed at THIS evidence is hidden unless `include_seen`;
    when its numbers move its `evidence_hash` moves with them and it returns as a new card,
    saying what changed. That is how an accepted recommendation whose evidence later reverses
    gets back in front of the person who accepted it.

    Reading also drafts lesson candidates for cells that have just crossed the sample floor
    (D8). A candidate enters the review inbox and nothing here publishes one.
    """
    authz.require_readable(db, user.id, project_id)
    report = harness_svc.report(db, project_id, window_days=window_days, versions="all")
    drafted = harness_svc.lessons_for_crossings(db, project_id, report)
    cards = harness_rules.cards(db, project_id, window_days=window_days)
    marks = harness_svc.marks_for(db, user_id=user.id, scope="project", scope_id=project_id)
    db.commit()

    out = []
    for card in cards:
        mark = marks.get(card.key)
        seen = mark is not None and mark.evidence_hash == card.evidence_hash
        state = "new"
        if mark is not None:
            state = "accepted" if mark.accepted_at else "dismissed"
        payload = card.as_dict()
        payload["state"] = state if seen else "new"
        payload["previously"] = (
            {"state": "accepted" if mark.accepted_at else "dismissed",
             "at": (mark.accepted_at or mark.dismissed_at).isoformat(),
             "evidence_changed": not seen}
            if mark is not None else None)
        if seen and not include_seen:
            continue
        out.append(payload)
    return {"project_id": project_id, "cards": out, "rules": list(harness_rules.RULES),
            "lessons_drafted": drafted,
            "window_days": report["window_days"], "floor": report["floor"]}


class MarkIn(BaseModel):
    project_id: str
    card_key: str
    evidence_hash: str
    action: str  # accept | dismiss


@router.post("/recommendations/mark")
def mark_recommendation(body: MarkIn, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Record that this person accepted or dismissed a card at this evidence.

    **Applies nothing.** R1 and R2 accept by producing text for a commit somebody makes; R3
    and R4 accept by the caller PUTting the profile or policy through the PRD-37 routes. This
    endpoint moves no preference and edits no matrix — it only stops the card nagging until
    its numbers move.
    """
    authz.require_readable(db, user.id, body.project_id)
    try:
        row = harness_svc.mark_card(db, user_id=user.id, scope="project",
                                    scope_id=body.project_id, card_key=body.card_key,
                                    evidence_hash=body.evidence_hash, action=body.action)
    except harness_svc.AttemptRefused as e:
        raise HTTPException(e.status, str(e))
    lesson = None
    if body.action == "accept":
        # D8: an accepted card is a judgement worth keeping, so it drafts a candidate the same
        # way a floor crossing does. The text is built from the card's own cells rather than
        # from its key — a lesson reading "R1:gbagent:qwen3.6 was accepted" would teach an
        # agent nothing it could act on. A human still publishes it.
        card = next((c for c in harness_rules.cards(db, body.project_id)
                     if c.key == body.card_key), None)
        if card is not None:
            lesson = harness_svc.draft_lesson(
                db, body.project_id, text=harness_rules.lesson_text(card),
                provenance={"card": card.key, "why": "accepted"}, origin=f"user:{user.id}")
    events_svc.record_user(db, user, action=f"{body.action}_harness_recommendation",
                           target_type="recommendation", target_id=body.card_key,
                           project_id=body.project_id, meta={"rule": body.card_key.split(":")[0]})
    db.commit()
    return {"card_key": row.card_key, "state": "accepted" if row.accepted_at else "dismissed",
            "evidence_hash": row.evidence_hash, "lesson_drafted": lesson}


class ShareIn(BaseModel):
    org_id: str
    telemetry_share: bool


@router.put("/platform/share")
def set_telemetry_share(body: ShareIn, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Opt an organisation into (or out of) the platform average (D13).

    Off by default and hosted-only. **Opting out recomputes at once**: an org that leaves must
    not stay inside the aggregate anyone reads next, and opt-in that keeps your numbers after
    you leave is not opt-in. Nothing of the org's crosses the boundary either way except
    weekly cell counts — never a raw row, never an org id.
    """
    from app.config import settings
    from app.models import Organization

    if not settings.hosted_mode:
        raise HTTPException(404, "the platform average is a hosted-service feature")
    authz.require_org_admin(db, user.id, body.org_id)
    org = db.get(Organization, body.org_id)
    if org is None:
        raise HTTPException(404, "organization not found")
    org.telemetry_share = bool(body.telemetry_share)
    db.flush()
    rows = harness_svc.platform_roll(db)
    events_svc.record_user(db, user, action="set_telemetry_share", target_type="org",
                           target_id=body.org_id,
                           meta={"telemetry_share": org.telemetry_share, "cells": rows})
    db.commit()
    return {"org_id": org.id, "telemetry_share": org.telemetry_share,
            "platform_cells": rows}


@router.post("/platform/roll")
def roll_platform(db: Session = Depends(get_db), key=Depends(get_agent_key)):
    """Recompute the platform rollups. The nightly job's entry point (D13).

    API-key authenticated like `POST /api/learning/run`, because the caller is a scheduler.
    It reads only orgs that opted in and writes only aggregates.
    """
    from app.config import settings

    if not settings.hosted_mode:
        raise HTTPException(404, "the platform average is a hosted-service feature")
    rows = harness_svc.platform_roll(db)
    db.commit()
    return {"cells": rows}
