"""Rule registry REST (GRPH-1155), and the library rule toggles (GRPH-1262).

The registry is read-only. Toggles are the one write here, and a write here is a person's:
these routes take a logged-in user, never an API key (PRD-P63 D10)."""
import base64

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Organization, User
from app.errors import NotFound, Refused, Validation
from app.schemas import (ObservationBackfillOut, ObservationsOut,
                         RecommendedRulesOut, RefusalCoverageOut, RuleSuggestionOut,
                         RulesFlowOut, RulesOut, RuleToggleBatchOut, RuleToggleBatchWrite,
                         RuleToggleOut, RuleTogglesOut, RuleToggleWrite, UnmappedRefusalsOut)
from app.security import authz
from app.security.deps import get_current_user, get_user_or_agent_key
from app.services import code_graph
from app.services import collision
from app.services import events as events_svc
from app.services import observations as observations_svc
from app.services import quotas
from app.services import rule_pack
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


@router.get("/rules/contract")
def rule_primitive_contract(principal=Depends(get_user_or_agent_key)):
    """The rule primitive contract this server runs (`rule_pack.contract()`), same JSON as
    `docs/rule-primitive-contract.json`.

    Either credential: pack CI, the consumer, holds an agent key, not a login. The body is
    server-wide static data, the same bytes every release tag already publishes in the repo,
    so no project scope is checked and nothing is withheld from a key that a person sees."""
    return rule_pack.contract()


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
def _toggle_write(fn, *args, body: RuleToggleWrite, user: User, db: Session, **kw):
    changes = body.model_dump(exclude_unset=True)
    reason = changes.pop("reason", "")
    try:
        return fn(db, *args, changes, actor=user, reason=reason, **kw)
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
    rule_pack.sync(db)
    return {"library_loaded": rule_toggles.library_loaded(),
            "library_reason": rule_pack.LOAD_REFUSAL or None,
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


@router.put("/rules/toggles", response_model=RuleToggleBatchOut)
def set_project_rule_toggles(body: RuleToggleBatchWrite, project_id: str = "core",
                             db: Session = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """Several of a project's own values under one reason, all-or-nothing (GRPH-1594). One
    refusal rolls the whole batch back and names the rule it refused; nothing was applied.
    Each rule changed gets its own `set_rule_toggle` event carrying the shared reason."""
    writes = [e.model_dump(exclude_unset=True) for e in body.toggles]
    try:
        rule_toggles.set_project_toggles(db, project_id, writes, actor=user,
                                         reason=body.reason)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Validation as e:
        raise HTTPException(422, str(e) + (f" — {e.hint}" if e.hint else "")) from e
    except rule_toggles.BatchRefused as e:
        err, prefix = e.error, f"{e.rule_id}: "
        if isinstance(err, Refused):
            # The server's own refusal, re-raised whole (its registry rule kept), naming the
            # library rule it refused in its detail and its audit meta.
            err.detail = prefix + str(err.detail)
            err.meta = {**err.meta, "refused_rule": e.rule_id, "batch": True}
            raise err from e
        status = 404 if isinstance(err, NotFound) else 422
        raise HTTPException(status, prefix + str(err) + (
            f" — {err.hint}" if getattr(err, "hint", None) else "")) from e
    by_id = {t["rule_id"]: t for t in rule_toggles.effective(db, project_id)}
    return {"toggles": [by_id[w["rule_id"]] for w in writes]}


# Org rule toggles are served by the console (GBC-P8). `set_org_toggle` / `org_view` stay
# for the box and for tests. The product routes are gone.

# ---- rule path helpers (GRPH-1597) -------------------------------------------------------
@router.get("/projects/{project_id}/paths/count")
def project_path_counts(project_id: str, path: list[str] = Query(default=[]),
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Files each prefix or glob matches in the project's indexed code graph. `files` is null
    (not 0) when the project has no indexed files."""
    authz.require_readable(db, user.id, project_id)
    return code_graph.path_counts(db, project_id, path)


@router.get("/rules/exclusive_areas/preview")
def exclusive_areas_preview(project_id: str = "core", path: list[str] = Query(default=[]),
                            db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """The open items the candidate prefixes would serialise. Read-only."""
    authz.require_readable(db, user.id, project_id)
    return collision.serialisation_preview(db, project_id, path)


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
            "rules": rule_measure.contribution(db, org_id),
            "observations": rule_measure.observation_contribution(db, org_id)}


@router.get("/orgs/{org_id}/rules/platform-effect")
def org_rules_platform_effect(org_id: str, db: Session = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """Platform effect rates: hosted, premium, and only for an org that opted in."""
    authz.require_org_admin(db, user.id, org_id)
    return rule_measure.platform_effect(db, org_id)
# ---- promotion to the org (GRPH-1264 / PRD-P63 §10, S4) ----------------------------------
class ProfileApply(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    reason: str = ""
    #: Rule ids to leave as they are.
    except_: list[str] = Field(default_factory=list, alias="except")
    #: Apply to the rest when the org has locked a rule the profile names.
    skip_locked: bool = False


class ProfileAccept(BaseModel):
    reason: str = ""
    #: The offered version being accepted; refused when the pack has moved on since it was read.
    version: str | None = None


def _org_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    except Validation as e:
        raise HTTPException(422, str(e) + (f" — {e.hint}" if e.hint else "")) from e


@router.get("/rules/profiles/updates")
def project_profile_updates(project_id: str = "core", db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """Applied profiles the loaded pack has a newer version of, with the diff. Reads only."""
    return _org_call(rules_org.project_updates, db, user, project_id)


@router.post("/rules/profiles/{profile_id}/updates/accept")
def accept_project_profile_update(profile_id: str, body: ProfileAccept, project_id: str = "core",
                                  db: Session = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    """Accept the offered version for one project: the ordinary apply, all-or-nothing."""
    return _org_call(rules_org.accept_project_update, db, user, project_id, profile_id,
                     reason=body.reason, version=body.version)


@router.get("/orgs/{org_id}/rules/profiles/updates")
def org_profile_updates(org_id: str, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Applied profiles the loaded pack has a newer version of, with the diff. Reads only."""
    return _org_call(rules_org.org_updates, db, user, org_id)


@router.post("/orgs/{org_id}/rules/profiles/{profile_id}/updates/accept")
def accept_org_profile_update(org_id: str, profile_id: str, body: ProfileAccept,
                              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Accept the offered version across the org: the ordinary apply, all-or-nothing."""
    return _org_call(rules_org.accept_org_update, db, user, org_id, profile_id,
                     reason=body.reason, version=body.version)


@router.post("/rules/profiles/{profile_id}/apply")
def apply_project_profile(profile_id: str, body: ProfileApply, project_id: str = "core",
                          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Apply the profile's rules to one project. Every plan (D11); a person's action (D13).
    Refused naming `rules.org_lock_holds` when the org has locked a rule it names."""
    return _org_call(rules_org.apply_project_profile, db, user, project_id, profile_id,
                     reason=body.reason, except_=body.except_, skip_locked=body.skip_locked)


@router.get("/rules/profiles/{profile_id}/drift")
def project_profile_drift(profile_id: str, project_id: str = "core",
                          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """How the project differs from the profile; loosening a pinned rule is `weakening`."""
    return _org_call(rules_org.project_drift, db, user, project_id, profile_id)


# Org profile list, applied, and apply are served by the console. Drift, updates, and the
# project apply stay here: the console does not serve them.

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


# Org rule-sync (`/rules/sync`) is served by the console. `rules_org.org_box_states` stays.

# ---- install a signed rule pack (GRPH-1329 / PRD-P63 D10, D12) ---------------------------
class PackUpload(BaseModel):
    """Either the artifact and its signature (base64), or a published `version` the server
    downloads itself. `sha256` is the offer's, echoed on Accept."""
    artifact_b64: str | None = None
    signature_b64: str | None = None
    version: str | None = None


class PackAccept(PackUpload):
    sha256: str


def _pack_bytes(body: PackUpload) -> tuple[bytes, bytes]:
    if body.version:
        try:
            return rule_pack.fetch_version(body.version)
        except rule_pack.PackFeedError as e:
            raise HTTPException(502, f"could not fetch the pack: {e}") from e
        except rule_pack.PackRefused as e:
            raise HTTPException(422, str(e)) from e
    if not body.artifact_b64 or not body.signature_b64:
        raise HTTPException(422, "send a version, or artifact_b64 and signature_b64")
    try:
        return (base64.b64decode(body.artifact_b64, validate=True),
                base64.b64decode(body.signature_b64, validate=True))
    except ValueError:
        raise HTTPException(422, "artifact_b64 and signature_b64 must be base64") from None


def _pack_person(user: User) -> User:
    """A pack is server-wide. Hosted, only the operator's allowlist installs one; self-hosted,
    any logged-in person (an API key never reaches here: `get_current_user`)."""
    if settings.hosted_mode and not quotas.is_platform_admin(user):
        raise Refused(403, "only a platform admin installs a rule pack on a hosted server",
                      rule_id=rule_toggles.PERSON_RULE)
    return user


@router.get("/rules/pack")
def rule_pack_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The installed pack's version, sha256, installed_at and installed_by, or the server's
    own words (`reason`) for why none is in force."""
    return rule_pack.status(db)


@router.get("/rules/pack/available")
def rule_pack_available(refresh: bool = False, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """The newest published pack this server can load: `state` current | available | unknown
    (`feed_error` set), `version`, `notes`, `installed_version`. Reads only (D10)."""
    _pack_person(user)
    return rule_pack.available(db, refresh=refresh)


@router.post("/rules/pack/offer")
def offer_rule_pack(body: PackUpload, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Verify a pack and return its diff against the pack in force and its `sha256`.
    Applies nothing. A person's request only (D10)."""
    artifact, sig = _pack_bytes(body)
    try:
        return rule_pack.offer_pack(db, _pack_person(user), artifact, sig)
    except rule_pack.PackRefused as e:
        raise HTTPException(422, str(e)) from e


@router.post("/rules/pack/accept")
def accept_rule_pack(body: PackAccept, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Install the offered pack. `sha256` must be the offer's, so what was previewed is what is
    applied (409 otherwise). A person's request only (D10); one audit event."""
    artifact, sig = _pack_bytes(body)
    try:
        return rule_pack.accept(db, _pack_person(user), artifact, sig, body.sha256)
    except rule_pack.PackMismatch as e:
        raise HTTPException(409, str(e)) from e
    except rule_pack.PackRefused as e:
        raise HTTPException(422, str(e)) from e


@router.get("/orgs/{org_id}/rules/recommended/dismissed")
def org_dismissed_recommendations(org_id: str, db: Session = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    """Org suggestions dismissed against their present evidence, so the panel can offer Restore."""
    return {"suggestions": rules_org.dismissed(db, user, org_id)}


@router.post("/orgs/{org_id}/rules/recommended/{suggestion_id}/dismiss", status_code=204)
def dismiss_org_recommendation(org_id: str, suggestion_id: str, db: Session = Depends(get_db),
                               user: User = Depends(get_current_user)):
    """Dismiss an org suggestion against its present evidence. Writes no rule toggle."""
    _org_call(rules_org.dismiss, db, user, org_id, suggestion_id)


@router.delete("/orgs/{org_id}/rules/recommended/{suggestion_id}/dismiss", status_code=204)
def restore_org_recommendation(org_id: str, suggestion_id: str, db: Session = Depends(get_db),
                               user: User = Depends(get_current_user)):
    _org_call(rules_org.restore, db, user, org_id, suggestion_id)
