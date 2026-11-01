"""Rule registry REST (GRPH-1155), and the library rule toggles (GRPH-1262).

The registry is read-only. Toggles are the one write here, and a write here is a person's:
these routes take a logged-in user, never an API key (PRD-P63 D10)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.errors import NotFound, Validation
from app.schemas import (OrgRuleToggleOut, RecommendedRulesOut, RefusalCoverageOut,
                         RuleSuggestionOut, RulesFlowOut, RulesOut, RuleToggleOut,
                         RuleTogglesOut, RuleToggleWrite, UnmappedRefusalsOut)
from app.security import authz
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import quotas
from app.services import rules as rules_svc
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
