"""The Harness page's server half (PRD-38 PR 2).

One read that answers the page's whole question — how each vendor x model x capability x size
band has actually turned out, week by week, with the sample count, the sampling skew, the cost
proxy, the capability enum and the derivation's coverage attached to every number.

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

    `versions=current` (the default) keeps the newest binary version per vendor+model and the
    previous one when both exist (PRD-41 D13); `versions=all` returns every version as its own
    cell, because two versions of one harness are two things and pooling them would hide a
    regression inside an average. The payload also carries `capability_set` (the §5 enum and
    family map) and `coverage` (attempts with ≥1 leaf / attempts).
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
def recommendations(project_id: str | None = None, org_id: str | None = None,
                    window_days: int | None = None,
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
    if org_id:
        authz.require_org_admin(db, user.id, org_id)
        report = harness_svc.org_report(db, org_id, window_days=window_days, versions="all")
        drafted = 0
        cards = harness_rules.cards(db, org_id=org_id, window_days=window_days)
        marks = harness_svc.marks_for(db, user_id=user.id, scope="org", scope_id=org_id)
        scope, scope_id = "org", org_id
    else:
        if not project_id:
            raise HTTPException(422, "name a project_id or an org_id")
        authz.require_readable(db, user.id, project_id)
        report = harness_svc.report(db, project_id, window_days=window_days, versions="all")
        drafted = harness_svc.lessons_for_crossings(db, project_id, report)
        cards = harness_rules.cards(db, project_id, window_days=window_days)
        marks = harness_svc.marks_for(db, user_id=user.id, scope="project", scope_id=project_id)
        scope, scope_id = "project", project_id
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
    payload = {"cards": out, "rules": list(harness_rules.RULES),
               "lessons_drafted": drafted,
               "window_days": report["window_days"], "floor": report["floor"],
               "scope": scope}
    if scope == "org":
        payload["org_id"] = scope_id
        payload["projects"] = report.get("projects") or []
    else:
        payload["project_id"] = scope_id
    return payload


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
    org_id: str | None = None
    telemetry_share: bool


@router.put("/platform/share")
def set_telemetry_share(body: ShareIn, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Opt into (or out of) the platform average (D13 / D11).

    Hosted: org admin, `org_id` required. Self-hosted: the instance toggle, posted over
    the deployment-sync credential; opting out stops posting and the hosted recompute
    drops the contributor.
    """
    from app.config import settings
    from app.models import Organization

    if not settings.hosted_mode:
        out = harness_svc.set_instance_share(db, body.telemetry_share)
        events_svc.record_user(db, user, action="set_telemetry_share", target_type="instance",
                               target_id="sync_link",
                               meta={"telemetry_share": out["telemetry_share"]})
        db.commit()
        return out
    if not body.org_id:
        raise HTTPException(422, "org_id is required on a hosted service")
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


@router.post("/contribute")
def contribute(db: Session = Depends(get_db), key=Depends(get_agent_key)):
    """Nightly self-hosted contribution. Posts D11 rollups over the sync credential."""
    out = harness_svc.post_contributions(db)
    db.commit()
    return out


@router.post("/snapshot/fetch")
def fetch_snapshot(db: Session = Depends(get_db), key=Depends(get_agent_key)):
    """Pull the hosted capability snapshot into capability_priors."""
    out = harness_svc.fetch_snapshot(db)
    db.commit()
    return out


@router.post("/platform/snapshot")
def roll_snapshot(db: Session = Depends(get_db), key=Depends(get_agent_key)):
    """Nightly capability_snapshot after the platform roll. Hosted only."""
    from app.config import settings

    if not settings.hosted_mode:
        raise HTTPException(404, "the platform snapshot is a hosted-service feature")
    harness_svc.platform_roll(db)
    out = harness_svc.publish_snapshot(db)
    db.commit()
    return out


@router.get("/probe/candidates")
def probe_candidates(project_id: str, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Closed items with a red sabotage, grouped by leaf, family fallback (PRD-41 D7).

    The estimated token cost from the panel's history is in the payload so a person can
    see it before they start a run. Nothing here starts a probe.
    """
    authz.require_readable(db, user.id, project_id)
    return harness_svc.probe_candidates(db, project_id)


class ProbeRunIn(BaseModel):
    project_id: str
    vendor: str
    model: str
    capability: str
    item_ids: list[str]
    trigger: str = "new_row"
    binary_version: str = ""


@router.post("/probe/runs")
def start_probe_run(body: ProbeRunIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Start a probe panel: scratch project, delegations with `sampled = probe`.

    One model and one leaf at a time, under the project's caps. Refused if a run for
    that model is already open.
    """
    authz.require_writable(db, user.id, body.project_id)
    try:
        out = harness_svc.start_probe_run(
            db, project_id=body.project_id, user_id=user.id, vendor=body.vendor,
            model=body.model, capability=body.capability, item_ids=body.item_ids,
            trigger=body.trigger, binary_version=body.binary_version)
    except harness_svc.AttemptRefused as e:
        raise HTTPException(e.status, str(e))
    db.commit()
    return out


@router.post("/review-checks/run")
def run_review_checks(project_id: str | None = None, db: Session = Depends(get_db),
                      key=Depends(get_agent_key)):
    """Recompute review checks over the 14-day window. The nightly job's entry point (D6)."""
    pid = project_id or getattr(key, "project_id", None)
    if not pid:
        raise HTTPException(422, "name a project_id")
    authz.require_writable(db, key.user_id, pid)
    rows = harness_svc.check_reviews(db, pid)
    db.commit()
    return {"checks": rows}


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
