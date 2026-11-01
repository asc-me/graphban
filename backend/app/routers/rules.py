"""Rule registry REST (GRPH-1155). Read-only; the view renders this."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.schemas import ObservationBackfillOut, ObservationsOut, RecommendedRulesOut, RefusalCoverageOut, RuleSuggestionOut, RulesFlowOut, RulesOut, UnmappedRefusalsOut
from app.security import authz
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import observations as observations_svc
from app.services import quotas
from app.services import rules as rules_svc
from app.services import rules_recommend
from app.version import __version__

router = APIRouter(tags=["rules"])


@router.get("/rules", response_model=RulesOut)
def list_rules(project_id: str = "core", db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    authz.require_readable(db, user.id, project_id)
    return {"version": __version__, "rules": rules_svc.registry(db, project_id)}


@router.get("/rules/flow", response_model=RulesFlowOut)
def rules_flow(project_id: str = "core", db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """The registry placed on the seven fixed stages (GRPH-1156), for the Rules map. Stage
    state and not-used are decided here so the view never recomputes them."""
    authz.require_readable(db, user.id, project_id)
    return {"version": __version__, **rules_svc.flow(db, project_id, user.id)}


@router.get("/rules/refusal-coverage", response_model=RefusalCoverageOut)
def refusal_coverage(project_id: str = "core", db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """"N of M refusal sites name their rule", as this build measured it (GRPH-1192). Read from
    package data, never computed here; an unpublished build says so with null numbers."""
    authz.require_readable(db, user.id, project_id)
    return rules_svc.refusal_coverage()


@router.get("/rules/recommended", response_model=RecommendedRulesOut)
def recommended_rules(project_id: str = "core", db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Suggestions only — nothing here writes a rule. `sources` always says what was read."""
    authz.require_readable(db, user.id, project_id)
    return rules_recommend.recommended(db, project_id)


@router.post("/rules/recommended/{suggestion_id}/dismiss", status_code=204)
def dismiss_recommendation(suggestion_id: str, project_id: str = "core",
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, project_id)
    if not rules_recommend.dismiss(db, project_id, suggestion_id):
        raise HTTPException(404, "No such suggestion")


@router.get("/rules/recommended/dismissed", response_model=list[RuleSuggestionOut])
def dismissed_recommendations(project_id: str = "core", db: Session = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """Suggestions currently dismissed, so the panel can offer Restore."""
    authz.require_readable(db, user.id, project_id)
    return rules_recommend.dismissed(db, project_id)


@router.delete("/rules/recommended/{suggestion_id}/dismiss", status_code=204)
def restore_recommendation(suggestion_id: str, project_id: str = "core",
                           db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    authz.require_writable(db, user.id, project_id)
    if not rules_recommend.restore(db, project_id, suggestion_id):
        raise HTTPException(404, "That suggestion is not dismissed")


@router.get("/rules/refusals/unmapped", response_model=UnmappedRefusalsOut)
def unmapped_refusals(project_id: str = "core", days: int = Query(7, ge=1, le=90),
                      db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Unmapped refusals grouped and ranked over the whole window (GRPH-1195).

    The platform bucket — refusals that named no project — is cross-tenant data, so it goes
    only to a platform admin, or, on a self-hosted instance (one tenant), to the project's
    owner. A hosted owner is not enough: anyone who creates a project owns it."""
    authz.require_readable(db, user.id, project_id)
    owner = authz.project_role(db, user.id, project_id) == "owner"
    include_platform = (not settings.hosted_mode and owner) or quotas.is_platform_admin(user)
    return events_svc.unmapped_refusals(db, project_id, days=days, include_platform=include_platform)


@router.get("/rules/observations", response_model=ObservationsOut)
def project_observations(project_id: str = "core", stage: str | None = None,
                         kind: str | None = None, limit: int = Query(100, ge=1, le=500),
                         offset: int = Query(0, ge=0), db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """The fleet observations ledger for one project (PRD-63 S1): typed facts, never prose."""
    authz.require_readable(db, user.id, project_id)
    try:
        return observations_svc.list_observations(
            db, project_ids=[project_id], stage=stage, kind=kind, limit=limit, offset=offset)
    except observations_svc.BadFilter as e:
        raise HTTPException(422, str(e))


@router.post("/rules/observations/backfill", response_model=ObservationBackfillOut)
def backfill_observations(project_id: str = "core", db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """Derive observations from the project's history. Idempotent: a fact already recorded,
    live or by an earlier run, is not added again."""
    authz.require_writable(db, user.id, project_id)
    return observations_svc.backfill(db, project_id)


@router.get("/orgs/{org_id}/rules/observations", response_model=ObservationsOut)
def org_observations(org_id: str, stage: str | None = None, kind: str | None = None,
                     limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Observations across the org's projects the caller can read."""
    authz.require_org_member(db, user.id, org_id)
    readable = set(authz.readable_project_ids(db, user.id))
    pids = [p for p in observations_svc.org_project_ids(db, org_id) if p in readable]
    try:
        return observations_svc.list_observations(
            db, project_ids=pids, stage=stage, kind=kind, limit=limit, offset=offset)
    except observations_svc.BadFilter as e:
        raise HTTPException(422, str(e))
