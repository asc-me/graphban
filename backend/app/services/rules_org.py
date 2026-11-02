"""Promotion to the org (GRPH-1264 / PRD-P63 §10, S4): oversight profiles and org recommendations.

Everything here is org-admin and premium (D11: `quotas.require_org_rules`, self-host included).

- **A profile pins rules.** `apply_profile` writes each rule the pack's profile names as the
  org's value, LOCKED, through `rule_toggles.set_org_toggle` — so every rule is one audit event
  naming the projects it changed — plus one `apply_rule_profile` event for the whole act. It is
  all-or-nothing: a profile naming a rule the loaded library cannot enforce (absent, or drawn
  unmeasured) is refused before anything is written, never half-applied.
- **Drift is read, not stored.** `drift` compares what the profile pins with what each project
  resolves to now. Loosening a pin is `weakening` (D13): a pinned-on rule now off, or a pinned
  rule no longer locked (any project can then override it). Any other difference, such as an
  off pin turned on or different params, is `changed`. A profile with no differences says `clean`; a profile the pack does
  not have says so, rather than reading as clean.
- **Org recommendations** run the project engine over the org's projects (D6) and keep a rule
  only when at least `ORG_MIN_PROJECTS` projects carry it. Computed weekly and returned with
  their age (D5). The "org already has it on" filter runs at read time, so adopting a rule
  drops it from the list at once rather than a week later. Suggestions only: nothing here turns a rule on.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound, Refused, Validation
from app.models import Item, Project, RuleToggle
from app.security import authz
from app.services import events as events_svc
from app.services import quotas
from app.services import rule_pack
from app.services import rule_toggles
from app.services import rules_recommend

#: The floor of D6's `ORG_MIN_PROJECTS`: two is the smallest signal a defect is not local.
MIN_PROJECTS_FLOOR = 2
MIN_PROJECTS_SHARE = 0.2
ACTIVE_DAYS = 30
CACHE_TTL_SECONDS = 7 * 86400.0

# org_id -> (computed_at_monotonic, computed_at_iso, payload)
_cache: dict[str, tuple[float, str, dict]] = {}


def clear_cache(org_id: str | None = None) -> None:
    if org_id is None:
        _cache.clear()
    else:
        _cache.pop(org_id, None)


def _admin(db: Session, user, org_id: str) -> None:
    authz.require_org_admin(db, user.id, org_id)
    quotas.require_org_rules(db, org_id)


def _profile(profile_id: str) -> dict:
    pack = rule_pack.CURRENT
    prof = pack.profiles.get(profile_id) if pack else None
    if prof is None:
        raise NotFound(f"{profile_id!r} is not a profile of the loaded rule pack",
                       hint="profiles come from the pack; none is loaded" if pack is None
                       else f"known: {', '.join(sorted(pack.profiles)) or 'none'}")
    return prof


def _pins(prof: dict) -> dict[str, dict]:
    rules = prof.get("rules")
    return {k: v for k, v in rules.items() if isinstance(v, dict)} if isinstance(rules, dict) else {}


def list_profiles(db: Session, user, org_id: str) -> dict:
    _admin(db, user, org_id)
    pack = rule_pack.CURRENT
    return {"pack_loaded": pack is not None, "pack_version": pack.version if pack else None,
            "profiles": [{"id": pid, "title": p.get("title", pid), "summary": p.get("summary", ""),
                          "version": p.get("version"), "rules": sorted(_pins(p))}
                         for pid, p in sorted((pack.profiles if pack else {}).items())]}


def _pin_changes(rule_id: str, pin: dict) -> dict:
    state = pin.get("state", "on")
    if state not in ("on", "off"):
        raise Validation(f"profile pin for {rule_id} has state {state!r}; use 'on' or 'off'")
    changes: dict = {"on": state == "on", "locked": True}
    if "params" in pin:
        changes["params"] = pin["params"]
    return changes


def _plan(profile_id: str) -> tuple[dict, dict[str, dict]]:
    """The profile and its validated pins as toggle changes. Every pin is checked before the
    first write, so a bad param on the last rule cannot leave the first ones applied."""
    prof = _profile(profile_id)
    pins = _pins(prof)
    if not pins:
        raise Validation(f"profile {profile_id!r} pins no rules")
    bad = {rid: ("not in the library" if rid not in rule_toggles.LIBRARY
                 else rule_toggles.LIBRARY[rid].unmeasured_reason)
           for rid in pins
           if rid not in rule_toggles.LIBRARY or rule_toggles.LIBRARY[rid].unmeasured_reason}
    if bad:
        raise Validation("profile " + repr(profile_id) + " names rules this server cannot enforce: "
                         + "; ".join(f"{k} ({v})" for k, v in sorted(bad.items())),
                         hint="nothing was applied")
    plan = {}
    for rid, pin in pins.items():
        changes = _pin_changes(rid, pin)
        if "params" in changes:
            rule_toggles._check_params(rule_toggles.LIBRARY[rid], changes["params"])
        plan[rid] = changes
    return prof, plan


def apply_project_profile(db: Session, user, project_id: str, profile_id: str, *,
                          reason: str | None) -> dict:
    """Apply a profile's rules to ONE project (D11 every plan, D13 a person's action): each
    rule's project toggle and params as the profile lists them, one `set_rule_toggle` event per
    rule plus one `apply_rule_profile` for the act. The project cannot lock, so `locked` is not
    applied. Refused whole, before any write, when the org has locked a rule the profile names."""
    rule_toggles._require_person(db, user)
    # Who before what: a caller outside the project is refused as such, not for its reason.
    authz.require_writable(db, user.id, project_id)
    if authz.project_role(db, user.id, project_id) not in rule_toggles.PROJECT_ADMIN_ROLES:
        raise Refused(403, "applying a profile needs the project's owner or an admin",
                      rule_id=rule_toggles.ADMIN_RULE)
    why = rule_toggles._reason(reason)
    prof, plan = _plan(profile_id)
    locked = sorted(rid for rid in plan if rule_toggles.resolve(db, rid, project_id)["locked"])
    if locked:
        raise Refused(409, f"profile {profile_id!r} names rules the org has locked: "
                      + ", ".join(locked) + "; a project cannot override them",
                      rule_id=rule_toggles.LOCK_RULE,
                      meta={"locked_rules": locked, "project_id": project_id},
                      hint="nothing was applied; ask an org admin to change the org's value "
                           "or unlock the rule")
    for rid, changes in sorted(plan.items()):
        rule_toggles.set_project_toggle(
            db, project_id, rid, {k: v for k, v in changes.items() if k != "locked"},
            actor=user, reason=f"profile {profile_id} v{prof.get('version', '?')}: {why}")
    events_svc.record_user(
        db, user, action="apply_rule_profile", target_type="project", target_id=project_id,
        project_id=project_id,
        meta={"profile": profile_id, "profile_version": prof.get("version"),
              "pack_version": rule_pack.CURRENT.version, "reason": why, "rules": sorted(plan)})
    return {"project_id": project_id, "profile": profile_id, "applied": sorted(plan)}


def project_drift(db: Session, user, project_id: str, profile_id: str) -> dict:
    """How one project differs from the profile. A rule counts once however it differs;
    turning a pinned-on rule off is `weakening` (D13). The lock is not compared: a project
    cannot set it."""
    authz.require_readable(db, user.id, project_id)
    prof = _profile(profile_id)
    rows = []
    for rid, pin in sorted(_pins(prof).items()):
        if rid not in rule_toggles.LIBRARY or rule_toggles.LIBRARY[rid].unmeasured_reason:
            rows.append({"rule_id": rid, "kind": "unmeasured",
                         "detail": "the loaded library cannot enforce this rule"})
            continue
        want_on = pin.get("state", "on") == "on"
        want_params = {**rule_toggles.LIBRARY[rid].default_params, **pin.get("params", {})}
        r = rule_toggles.resolve(db, rid, project_id)
        on, params = r["on"]["value"], r["params"]["value"]
        if on != want_on:
            rows.append({"rule_id": rid, "kind": "weakening" if want_on else "changed",
                         "detail": f"pinned {'on' if want_on else 'off'}, is "
                                   f"{'on' if on else 'off'} ({r['on']['source']})"})
        elif params != want_params:
            rows.append({"rule_id": rid, "kind": "changed",
                         "detail": f"pinned params {want_params}, are {params}"})
    title = prof.get("title", profile_id)
    n = len(rows)
    return {"project_id": project_id, "profile": profile_id,
            "state": "clean" if not rows else "drifted", "differs_in": n,
            "summary": f"matches {title}" if not n else
            f"differs from {title} in {n} rule{'s' if n != 1 else ''}",
            "weakening": sum(r["kind"] == "weakening" for r in rows), "drift": rows}


def apply_profile(db: Session, user, org_id: str, profile_id: str, *, reason: str | None) -> dict:
    """Pin a profile's rules across the org, locked. Refused whole when any rule it names
    cannot be enforced by the loaded library."""
    # D10 before anything else: the engine has no user id for the org-admin check to read, so a
    # caller that is not a person is refused as such rather than failing on the lookup.
    rule_toggles._require_person(db, user)
    _admin(db, user, org_id)
    why = rule_toggles._reason(reason)
    prof, plan = _plan(profile_id)
    applied = {}
    for rid, changes in sorted(plan.items()):
        applied[rid] = rule_toggles.set_org_toggle(
            db, org_id, rid, changes, actor=user,
            reason=f"profile {profile_id} v{prof.get('version', '?')}: {why}")["projects_changed"]
    events_svc.record_user(
        db, user, action="apply_rule_profile", target_type="org", target_id=org_id,
        meta={"profile": profile_id, "profile_version": prof.get("version"),
              "pack_version": rule_pack.CURRENT.version, "reason": why,
              "rules": sorted(plan), "projects_changed": applied})
    return {"org_id": org_id, "profile": profile_id, "applied": sorted(plan),
            "projects_changed": applied}


def drift(db: Session, user, org_id: str, profile_id: str) -> dict:
    """Where the org's projects differ from what the profile pins, per project and rule."""
    _admin(db, user, org_id)
    pins = _pins(_profile(profile_id))
    project_ids = list(db.scalars(select(Project.id).where(Project.org_id == org_id)
                                  .order_by(Project.id)))
    rows = []
    for rid, pin in sorted(pins.items()):
        if rid not in rule_toggles.LIBRARY or rule_toggles.LIBRARY[rid].unmeasured_reason:
            rows.append({"rule_id": rid, "project_id": None, "kind": "unmeasured",
                         "detail": "the loaded library cannot enforce this rule"})
            continue
        want_on = pin.get("state", "on") == "on"
        want_params = {**rule_toggles.LIBRARY[rid].default_params, **pin.get("params", {})}
        for pid in project_ids:
            r = rule_toggles.resolve(db, rid, pid)
            on, params = r["on"]["value"], r["params"]["value"]
            # One row per rule and project, `weakening` when any difference loosens the pin.
            details, weakening = [], False
            if on != want_on:
                weakening |= want_on
                details.append(f"pinned {'on' if want_on else 'off'}, is "
                               f"{'on' if on else 'off'} ({r['on']['source']})")
            elif params != want_params:
                details.append(f"pinned params {want_params}, are {params}")
            if not r["locked"]:
                weakening = True
                details.append("pinned locked, is unlocked: the project can override it")
            if details:
                rows.append({"rule_id": rid, "project_id": pid,
                             "kind": "weakening" if weakening else "changed",
                             "detail": "; ".join(details)})
    return {"org_id": org_id, "profile": profile_id, "projects": len(project_ids),
            "state": "clean" if not rows else "drifted", "drift": rows}


# ---- org recommendations (D6) -------------------------------------------------------------

def active_projects(db: Session, org_id: str) -> list[str]:
    """Projects with at least one item moved in the last 30 days."""
    since = datetime.now(timezone.utc) - timedelta(days=ACTIVE_DAYS)
    return sorted(set(db.scalars(
        select(Item.project_id).join(Project, Project.id == Item.project_id)
        .where(Project.org_id == org_id, Item.updated_at >= since))))


def org_min_projects(active: int) -> int:
    """The larger of 2 projects and 20% of the org's active projects."""
    return max(MIN_PROJECTS_FLOOR, math.ceil(MIN_PROJECTS_SHARE * active))


def _compute(db: Session, org_id: str, *, refresh: bool = False) -> dict:
    project_ids = list(db.scalars(select(Project.id).where(Project.org_id == org_id)
                                  .order_by(Project.id)))
    active = active_projects(db, org_id)
    floor = org_min_projects(len(active))
    by_rule: dict[str, dict] = {}
    unread = []
    for pid in project_ids:
        if refresh:
            rules_recommend.clear_cache(pid)  # a refresh reads the evidence again, not a minute-old copy
        suggestions, sources = rules_recommend._cached(db, pid)
        if not any(s["state"] == "read" and s["source"] == "fleet" for s in sources):
            unread.append(pid)
        for s in suggestions:
            if s["kind"] != "rule" or not s["rule_id"]:
                continue
            g = by_rule.setdefault(s["rule_id"], {"suggestion": s, "projects": {}})
            g["projects"][pid] = s["score"] or 0
    out = []
    for rid, g in sorted(by_rule.items()):
        n = len(g["projects"])
        if n < floor:
            continue
        s = g["suggestion"]
        out.append({"id": f"org.rule.{rid}", "rule_id": rid, "stage": s["stage"],
                    "level": s["level"], "title": s["title"],
                    "why": f"{n} of the org's projects carry observations implying this rule. "
                           "Nothing turns it on but an org admin.",
                    "projects": n, "project_ids": sorted(g["projects"]),
                    "score": round(sum(g["projects"].values()), 2), "effect": s["effect"]})
    out.sort(key=lambda r: -r["score"])
    return {"org_min_projects": floor, "active_projects": len(active),
            "projects": len(project_ids), "suggestions": out,
            # Projects whose fleet ledger was empty or unreadable: an org with a short list
            # because nobody looked must not read like an org with nothing to suggest.
            "projects_without_observations": unread}


def recommended(db: Session, user, org_id: str, *, refresh: bool = False) -> dict:
    _admin(db, user, org_id)
    hit = _cache.get(org_id)
    if refresh or hit is None or time.monotonic() - hit[0] >= CACHE_TTL_SECONDS:
        hit = (time.monotonic(), datetime.now(timezone.utc).isoformat(), _compute(db, org_id, refresh=refresh))
        _cache[org_id] = hit
    # The org's own toggles are read live, not from the weekly copy: an admin who adopts a
    # suggestion must not keep being offered it until the cache turns over.
    org_on = set(db.scalars(select(RuleToggle.rule_id).where(
        RuleToggle.org_id == org_id, RuleToggle.on.is_(True))))
    return {**hit[2], "suggestions": [s for s in hit[2]["suggestions"] if s["rule_id"] not in org_on],
            "computed_at": hit[1], "age_seconds": int(time.monotonic() - hit[0])}
