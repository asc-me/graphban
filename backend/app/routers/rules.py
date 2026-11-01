"""Rule registry REST (GRPH-1155), and the library rule toggles (GRPH-1262).

The registry is read-only. Toggles are the one write here, and a write here is a person's:
these routes take a logged-in user, never an API key (PRD-P63 D10)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Organization, User
from app.errors import NotFound, Validation
from app.schemas import (ObservationBackfillOut, ObservationsOut, OrgRuleToggleOut,
                         RecommendedRulesOut, RefusalCoverageOut, RuleSuggestionOut,
                         RulesFlowOut, RulesOut, RuleToggleOut, RuleTogglesOut,
                         RuleToggleWrite, UnmappedRefusalsOut)
from app.security import authz
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import observations as observations_svc
from app.services import quotas
from app.services import rules as rules_svc
from app.services import rule_measure
from app.services import rules_org
from app.services import rule_toggles
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


# ---- library rule toggles (GRPH-1262 / PRD-P63 §8, D4) -----------------------------------
def _toggle_write(fn, *args, body: RuleToggleWrite, user: User, db: Session):
    changes = body.model_dump(exclude_unset=True)
    reason = changes.pop("reason", "")
    try:
        return fn(db, *args, changes, actor=user, reason=reason)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Validation as e:
        raise HTTPException(422, str(e) + (f" — {e.hint}" if e.hint else "")) from e


@router.get("/rules/toggles", response_model=RuleTogglesOut)
def rule_toggles_for_project(project_id: str = "core", db: Session = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """Every library rule's effective toggle and params for the project, each with its
    `source` (`project` | `org` | `platform default`) and the org's lock."""
    authz.require_readable(db, user.id, project_id)
    return {"library_loaded": rule_toggles.library_loaded(),
            "toggles": rule_toggles.effective(db, project_id)}


@router.put("/rules/toggles/{rule_id}", response_model=RuleToggleOut)
def set_project_rule_toggle(rule_id: str, body: RuleToggleWrite, project_id: str = "core",
                            db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """A project's own value. Every plan (D11). Refused naming `rules.org_lock_holds` when the
    org has locked the rule."""
    if "locked" in body.model_fields_set:
        raise HTTPException(422, "`locked` is an org setting; a project cannot lock a rule")
    _toggle_write(rule_toggles.set_project_toggle, project_id, rule_id, body=body, user=user,
                  db=db)
    return next(t for t in rule_toggles.effective(db, project_id) if t["rule_id"] == rule_id)


@router.put("/orgs/{org_id}/rule-toggles/{rule_id}", response_model=OrgRuleToggleOut)
def set_org_rule_toggle(org_id: str, rule_id: str, body: RuleToggleWrite,
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The org's value and lock, for every project in the org at once — one audit event
    naming the projects it changed. Org admins on `team` / `enterprise` (402 otherwise)."""
    return _toggle_write(rule_toggles.set_org_toggle, org_id, rule_id, body=body, user=user,
                         db=db)


# ---- measurement (GRPH-1265 / PRD-P63 S5, D8, D9) ----------------------------------------
# Read-only. Every measured value is {status, value, n, min_n, reason} (measured | insufficient
# | unavailable); a null value never means "not enough data".
def _measure(fn, *args):
    try:
        return fn(*args)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Validation as e:
        raise HTTPException(422, str(e)) from e


@router.get("/rules/effect")
def rules_effect(project_id: str = "core", rule_id: str | None = None,
                 profile: str | None = None, group_by: str | None = None,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Caught, escape rate before/after adoption, cost and retirement per library rule.
    `profile` filters by oversight profile; `group_by=profile` groups by it."""
    authz.require_readable(db, user.id, project_id)
    return _measure(rule_measure.project_effect, db, project_id, rule_id, profile, group_by)


@router.get("/orgs/{org_id}/rules/effect")
def org_rules_effect(org_id: str, rule_id: str | None = None, profile: str | None = None,
                     group_by: str | None = None, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """The same across the org's projects. Org admins on team/enterprise (402 otherwise)."""
    authz.require_org_admin(db, user.id, org_id)
    return _measure(rule_measure.org_effect, db, org_id, rule_id, profile, group_by)


@router.get("/orgs/{org_id}/rules/contribution")
def org_rules_contribution(org_id: str, db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """Exactly the counts this org would contribute if it opts in (`telemetry_share`)."""
    authz.require_org_admin(db, user.id, org_id)
    quotas.require_org_rules(db, org_id)
    return {"opted_in": bool(db.get(Organization, org_id).telemetry_share),
            "rules": rule_measure.contribution(db, org_id)}


@router.get("/orgs/{org_id}/rules/platform-effect")
def org_rules_platform_effect(org_id: str, db: Session = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """Platform effect rates: hosted, premium, and only for an org that opted in."""
    authz.require_org_admin(db, user.id, org_id)
    return rule_measure.platform_effect(db, org_id)
# ---- promotion to the org (GRPH-1264 / PRD-P63 §10, S4) ----------------------------------
class ProfileApply(BaseModel):
    reason: str = ""


def _org_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Validation as e:
        raise HTTPException(422, str(e) + (f" — {e.hint}" if e.hint else "")) from e


@router.get("/orgs/{org_id}/rules/profiles")
def org_profiles(org_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Oversight profiles from the loaded rule pack. Org admins on `team` / `enterprise`."""
    return rules_org.list_profiles(db, user, org_id)


@router.post("/orgs/{org_id}/rules/profiles/{profile_id}/apply")
def apply_org_profile(org_id: str, profile_id: str, body: ProfileApply,
                      db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Pin the profile's rules across the org, locked: one event per rule and one for the act."""
    return _org_call(rules_org.apply_profile, db, user, org_id, profile_id, reason=body.reason)


@router.get("/orgs/{org_id}/rules/profiles/{profile_id}/drift")
def org_profile_drift(org_id: str, profile_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Where projects differ from the profile; turning a pinned rule off is `weakening`."""
    return _org_call(rules_org.drift, db, user, org_id, profile_id)


@router.get("/orgs/{org_id}/rules/recommended")
def org_recommended_rules(org_id: str, refresh: bool = False, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """Library rules implied by at least ORG_MIN_PROJECTS projects. Computed weekly; the reply
    carries its age. Suggestions only."""
    return rules_org.recommended(db, user, org_id, refresh=refresh)
