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

import hashlib
import json
import math
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import NotFound, Refused, Validation
from app.models import (
    ApiKey, BoxRuleSync, Event, Item, Organization, OrgRuleSuggestionDismissal, OrgRuleVersion,
    Project, RuleDecisionPull,
    RuleToggle, utcnow,
)
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


#: Strictness order the Quick configure picker shows; profiles the pack adds follow by id.
STRICTNESS = ("exploratory", "standard", "regulated")


def _enforceable(rule_id: str) -> bool:
    r = rule_toggles.LIBRARY.get(rule_id)
    return r is not None and not r.unmeasured_reason


def project_profiles(db: Session, user, project_id: str) -> dict:
    """The pack's profiles with their pins, and the project's applied level (the latest
    `apply_rule_profile` event for it, or None). Readable by anyone who can read the project."""
    authz.require_readable(db, user.id, project_id)
    pack = rule_pack.CURRENT
    profiles = pack.profiles if pack else {}
    order = [p for p in STRICTNESS if p in profiles] + sorted(p for p in profiles if p not in STRICTNESS)
    ev = _last_applied_any(db, "project", project_id)
    meta = (ev.meta or {}) if ev else {}
    applied = None
    if ev is not None:
        applied = {"profile": meta.get("profile"), "version": meta.get("profile_version"),
                   "pack_version": meta.get("pack_version"),
                   "exceptions": list(meta.get("exceptions", [])),
                   "skipped": list(meta.get("skipped", [])),
                   "by": ev.actor_label or ev.actor_id, "at": ev.ts}
    return {"project_id": project_id, "pack_loaded": pack is not None,
            "pack_version": pack.version if pack else None, "order": order,
            "profiles": [{"id": pid, "title": profiles[pid].get("title", pid),
                          "summary": profiles[pid].get("summary", ""),
                          "version": profiles[pid].get("version"),
                          "usable": not _repo_specific_pins(_pins(profiles[pid])),
                          "unusable_reason": ("pins repo-specific rules: "
                                              + "; ".join(_repo_specific_pins(_pins(profiles[pid]))))
                          if _repo_specific_pins(_pins(profiles[pid])) else None,
                          "pins": [{"rule_id": rid, "state": pin.get("state", "on"),
                                    "params": pin.get("params", {}), "enforceable": _enforceable(rid)}
                                   for rid, pin in sorted(_pins(profiles[pid]).items())]}
                         for pid in order],
            "applied": applied}


def _repo_specific_pins(pins: dict[str, dict]) -> list[str]:
    """The pins that would overwrite a repo's own values: a level never touches exclusive areas,
    generated files, path classes or the test-scope engines. Each entry names rule and param."""
    out = []
    for rid, pin in sorted(pins.items()):
        params = pin.get("params") if isinstance(pin.get("params"), dict) else {}
        if rid in (rule_toggles.EXCLUSIVE_AREAS_RULE, rule_toggles.GENERATED_FILES_RULE,
                   rule_toggles.PATH_CLASSES_RULE):
            out.append(f"{rid} ({', '.join(sorted(params)) or 'state'})")
        elif rid == rule_toggles.TEST_SCOPE_RULE and "engines" in params:
            out.append(f"{rid} (params.engines)")
    return out


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
    repo = _repo_specific_pins(pins)
    if repo:
        raise Validation(f"profile {profile_id!r} pins repo-specific rules a level never touches: "
                         + "; ".join(repo), hint="nothing was applied")
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
                          reason: str | None, except_: list[str] | None = None,
                          skip_locked: bool = False) -> dict:
    """Apply a profile's rules to ONE project (D11 every plan, D13 a person's action): each
    rule's project toggle and params as the profile lists them, one `set_rule_toggle` event per
    rule plus one `apply_rule_profile` for the act. The project cannot lock, so `locked` is not
    applied. Rules in `except_` are left as they are. Refused whole, before any write, when the
    org has locked a rule the profile names, unless `skip_locked`: then the rest is applied and
    the locked rules are listed as skipped. Only rules whose effective value differs are
    written; params the profile does not name keep their current value."""
    rule_toggles._require_person(db, user)
    # Who before what: a caller outside the project is refused as such, not for its reason.
    authz.require_writable(db, user.id, project_id)
    if authz.project_role(db, user.id, project_id) not in rule_toggles.PROJECT_ADMIN_ROLES:
        raise Refused(403, "applying a profile needs the project's owner or an admin",
                      rule_id=rule_toggles.ADMIN_RULE)
    why = rule_toggles._reason(reason)
    prof, plan = _plan(profile_id)
    excepted = sorted(set(except_ or []))
    unknown = [rid for rid in excepted if rid not in plan]
    if unknown:
        raise Validation(f"except names rules profile {profile_id!r} does not pin: "
                         + ", ".join(unknown), hint="nothing was applied")
    plan = {rid: c for rid, c in plan.items() if rid not in excepted}
    locked = sorted(rid for rid in plan if rule_toggles.resolve(db, rid, project_id)["locked"])
    if locked and not skip_locked:
        raise Refused(409, f"profile {profile_id!r} names rules the org has locked: "
                      + ", ".join(locked) + "; a project cannot override them",
                      rule_id=rule_toggles.LOCK_RULE,
                      meta={"locked_rules": locked, "project_id": project_id},
                      hint="nothing was applied; ask an org admin to change the org's value "
                           "or unlock the rule, or apply with skip_locked")
    plan = {rid: c for rid, c in plan.items() if rid not in locked}
    written = []
    for rid, changes in sorted(plan.items()):
        r = rule_toggles.resolve(db, rid, project_id)
        write = {k: v for k, v in changes.items() if k not in ("locked", "params")}
        if write.get("on") == r["on"]["value"]:
            write.pop("on")
        if "params" in changes:
            want = {**r["params"]["value"], **changes["params"]}
            if want != r["params"]["value"]:
                write["params"] = changes["params"]
        if not write:
            continue
        rule_toggles.set_project_toggle(
            db, project_id, rid, write,
            actor=user, reason=f"profile {profile_id} v{prof.get('version', '?')}: {why}")
        written.append(rid)
    events_svc.record_user(
        db, user, action="apply_rule_profile", target_type="project", target_id=project_id,
        project_id=project_id,
        meta={"profile": profile_id, "profile_version": prof.get("version"),
              "pack_version": rule_pack.CURRENT.version, "reason": why, "rules": sorted(plan),
              "written": written, "exceptions": excepted, "skipped": locked,
              "pins": _pins(prof)})
    return {"project_id": project_id, "profile": profile_id, "applied": written,
            "excepted": excepted, "skipped_locked": locked}


def _loses_entries(now: dict, want: dict) -> bool:
    """True when a list param now holds fewer entries than the level pinned (the hand edit
    removed one); adding entries is not a loosening."""
    for k, pinned in want.items():
        cur = now.get(k)
        if isinstance(pinned, list) and isinstance(cur, list) and any(e not in cur for e in pinned):
            return True
    return False


def project_drift(db: Session, user, project_id: str, profile_id: str) -> dict:
    """How one project differs from the profile. A rule counts once however it differs;
    turning a pinned-on rule off is `weakening` (D13). The lock is not compared: a project
    cannot set it."""
    authz.require_readable(db, user.id, project_id)
    prof = _profile(profile_id)
    ev = _last_applied(db, "project", project_id).get(profile_id)
    meta = (ev.meta or {}) if ev else {}
    intended = set(meta.get("exceptions", [])) | set(meta.get("skipped", []))
    rows = []
    for rid, pin in sorted(_pins(prof).items()):
        if rid in intended:  # left out on purpose at apply: intended, not drift
            continue
        if rid not in rule_toggles.LIBRARY or rule_toggles.LIBRARY[rid].unmeasured_reason:
            rows.append({"rule_id": rid, "kind": "unmeasured",
                         "detail": "the loaded library cannot enforce this rule"})
            continue
        want_on = pin.get("state", "on") == "on"
        r = rule_toggles.resolve(db, rid, project_id)
        on, params = r["on"]["value"], r["params"]["value"]
        want_params = {**params, **pin.get("params", {})}  # unnamed params are not drift
        if on != want_on:
            rows.append({"rule_id": rid, "kind": "weakening" if want_on else "changed",
                         "detail": f"pinned {'on' if want_on else 'off'}, is "
                                   f"{'on' if on else 'off'} ({r['on']['source']})"})
        elif params != want_params:
            rows.append({"rule_id": rid,
                         "kind": "weakening" if _loses_entries(params, want_params) else "changed",
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
              "rules": sorted(plan), "projects_changed": applied, "pins": _pins(prof)})
    note_published(db, org_id)
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


def applied_profiles(db: Session, user, org_id: str) -> dict:
    """The profile each project in the org has applied (GRPH-1330), and where it came from.

    Read from `apply_rule_profile` events: the latest one wins per project, whether it was an
    apply to that project alone (`source: project`) or the org's apply (`source: org`,
    inherited). A project with neither says `profile: null, source: null`. `drift_path` is the
    read that measures the project against the reported profile."""
    _admin(db, user, org_id)
    project_ids = list(db.scalars(select(Project.id).where(Project.org_id == org_id)
                                  .order_by(Project.id)))
    latest: dict[str, tuple[tuple, str, Event]] = {}

    def keep(pid: str, ev: Event, source: str) -> None:
        if not (ev.meta or {}).get("profile"):
            return
        rank = (ev.ts, ev.id)
        if pid not in latest or rank > latest[pid][0]:
            latest[pid] = (rank, source, ev)

    org_ev = _last_applied_any(db, "org", org_id)
    for pid in project_ids:
        if org_ev is not None:
            keep(pid, org_ev, "org")
    for ev in db.scalars(select(Event).where(
            Event.action == "apply_rule_profile", Event.target_type == "project",
            Event.target_id.in_(project_ids or [""])).order_by(Event.ts, Event.id)):
        keep(ev.target_id, ev, "project")
    rows = []
    for pid in project_ids:
        if pid not in latest:
            rows.append({"project_id": pid, "profile": None, "profile_version": None,
                         "source": None, "applied_at": None, "drift_path": None})
            continue
        _, source, ev = latest[pid]
        prof = ev.meta["profile"]
        rows.append({"project_id": pid, "profile": prof,
                     "profile_version": ev.meta.get("profile_version"), "source": source,
                     "applied_at": ev.ts.isoformat() if ev.ts else None,
                     "drift_path": f"/api/rules/profiles/{prof}/drift?project_id={pid}"})
    return {"org_id": org_id, "projects": rows}


def _last_applied_any(db: Session, target_type: str, target_id: str) -> Event | None:
    """The single latest `apply_rule_profile` event for an org or project, any profile."""
    return db.scalars(select(Event).where(
        Event.action == "apply_rule_profile", Event.target_type == target_type,
        Event.target_id == target_id).order_by(Event.ts.desc(), Event.id.desc()).limit(1)).first()


# ---- offered profile updates (GRPH-1328 / GBC-P6 §2, D10, D12, D13) -----------------------
#
# A newer pack that changes a profile is OFFERED with its diff; nothing is written until a
# person accepts it. Loading a pack touches no toggle. The applied version is read from the
# `apply_rule_profile` event (which also keeps the pins it applied, to diff against).

def _last_applied(db: Session, target_type: str, target_id: str) -> dict[str, Event]:
    """Latest `apply_rule_profile` event per profile for one org or project."""
    out: dict[str, Event] = {}
    for ev in db.scalars(select(Event).where(
            Event.action == "apply_rule_profile", Event.target_type == target_type,
            Event.target_id == target_id).order_by(Event.id)):
        pid = (ev.meta or {}).get("profile")
        if pid:
            out[pid] = ev
    return out


def _pin_diff(old: dict[str, dict], new: dict[str, dict]) -> list[dict]:
    rows = []
    for rid in sorted(set(old) | set(new)):
        a, b = old.get(rid), new.get(rid)
        if a is None:
            rows.append({"rule_id": rid, "change": "added", "kind": "changed", "to": b})
        elif b is None:
            # Dropping a pin lets the rule be loosened: weakening, as drift counts it.
            rows.append({"rule_id": rid, "change": "removed", "kind": "weakening", "from": a})
        else:
            sa, sb = a.get("state", "on"), b.get("state", "on")
            if sa != sb:
                rows.append({"rule_id": rid, "change": "state_changed",
                             "kind": "weakening" if sa == "on" else "changed",
                             "from": sa, "to": sb})
            if a.get("params", {}) != b.get("params", {}):
                rows.append({"rule_id": rid, "change": "params_changed", "kind": "changed",
                             "from": a.get("params", {}), "to": b.get("params", {})})
    return rows


def _offers(applied: dict[str, Event]) -> list[dict]:
    pack = rule_pack.CURRENT
    out = []
    for pid, ev in sorted(applied.items()):
        prof = pack.profiles.get(pid) if pack else None
        meta = ev.meta or {}
        if prof is None or str(prof.get("version")) == str(meta.get("profile_version")):
            continue
        old = meta.get("pins")
        rows = _pin_diff(old, _pins(prof)) if isinstance(old, dict) else None
        out.append({"profile": pid, "applied_version": meta.get("profile_version"),
                    "offered_version": prof.get("version"), "pack_version": pack.version,
                    # None: applied before pins were recorded, so there is nothing to diff against.
                    "diff": rows,
                    "weakening": sum(r["kind"] == "weakening" for r in rows) if rows is not None else None})
    return out


def org_updates(db: Session, user, org_id: str) -> dict:
    _admin(db, user, org_id)
    return {"org_id": org_id, "updates": _offers(_last_applied(db, "org", org_id))}


def project_updates(db: Session, user, project_id: str) -> dict:
    authz.require_readable(db, user.id, project_id)
    return {"project_id": project_id, "updates": _offers(_last_applied(db, "project", project_id))}


def _require_offer(offers: list[dict], profile_id: str, version: str | None) -> None:
    offer = next((o for o in offers if o["profile"] == profile_id), None)
    if offer is None:
        raise NotFound(f"profile {profile_id!r} has no update on offer",
                       hint="it was never applied here, or the applied version is the loaded one")
    if version is not None and str(version) != str(offer["offered_version"]):
        raise Validation(f"profile {profile_id!r} is now offered at version "
                         f"{offer['offered_version']}, not {version}", hint="read the diff again")


def accept_org_update(db: Session, user, org_id: str, profile_id: str, *, reason: str | None,
                      version: str | None = None) -> dict:
    """Apply the offered version through the ordinary org apply: all-or-nothing, a person only."""
    rule_toggles._require_person(db, user)
    _admin(db, user, org_id)
    _require_offer(_offers(_last_applied(db, "org", org_id)), profile_id, version)
    return apply_profile(db, user, org_id, profile_id, reason=reason)


def accept_project_update(db: Session, user, project_id: str, profile_id: str, *,
                          reason: str | None, version: str | None = None) -> dict:
    """As `accept_org_update` for one project, but carries the last apply's exceptions and
    skips (rather than refuses on) a rule the org has locked; the event records both."""
    rule_toggles._require_person(db, user)
    authz.require_writable(db, user.id, project_id)
    last = _last_applied(db, "project", project_id)
    _require_offer(_offers(last), profile_id, version)
    # Carry the earlier exceptions (those the new version still pins) and skip locked rules,
    # so accepting an update does not forget what the person left out.
    kept = sorted(set((last[profile_id].meta or {}).get("exceptions", []))
                  & set(_pins(_profile(profile_id))))
    return apply_project_profile(db, user, project_id, profile_id, reason=reason,
                                 except_=kept, skip_locked=True)


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
            g = by_rule.setdefault(s["rule_id"], {"suggestion": s, "projects": {}, "prints": {}})
            g["projects"][pid] = s["score"] or 0
            g["prints"][pid] = s["fingerprint"]
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
                    "score": round(sum(g["projects"].values()), 2), "effect": s["effect"],
                    # Any contributing project's evidence moving, or the set of projects
                    # changing, changes this: that is what re-opens a dismissal.
                    "fingerprint": hashlib.sha256("\n".join(
                        f"{p}:{f}" for p, f in sorted(g["prints"].items())).encode()).hexdigest()[:16]})
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
    marks = _marks(db, org_id)
    return {**hit[2], "suggestions": [s for s in hit[2]["suggestions"]
                                      if s["rule_id"] not in org_on
                                      and marks.get(s["id"]) != s["fingerprint"]],
            "computed_at": hit[1], "age_seconds": int(time.monotonic() - hit[0])}


def _marks(db: Session, org_id: str) -> dict[str, str]:
    return {d.suggestion_id: d.fingerprint for d in db.scalars(
        select(OrgRuleSuggestionDismissal).where(OrgRuleSuggestionDismissal.org_id == org_id))}


def _current(db: Session, org_id: str) -> list[dict]:
    hit = _cache.get(org_id)
    if hit is None or time.monotonic() - hit[0] >= CACHE_TTL_SECONDS:
        hit = (time.monotonic(), datetime.now(timezone.utc).isoformat(), _compute(db, org_id))
        _cache[org_id] = hit
    return hit[2]["suggestions"]


def dismiss(db: Session, user, org_id: str, suggestion_id: str) -> None:
    """Dismiss against the evidence as it stands now. Writes no toggle (D10)."""
    _admin(db, user, org_id)
    current = next((s for s in _current(db, org_id) if s["id"] == suggestion_id), None)
    if current is None:
        raise NotFound("No such suggestion")
    row = db.get(OrgRuleSuggestionDismissal, (org_id, suggestion_id))
    if row is None:
        db.add(OrgRuleSuggestionDismissal(org_id=org_id, suggestion_id=suggestion_id,
                                          fingerprint=current["fingerprint"]))
    else:
        row.fingerprint = current["fingerprint"]
        row.dismissed_at = datetime.now(timezone.utc)
    db.commit()


def dismissed(db: Session, user, org_id: str) -> list[dict]:
    """Suggestions dismissed against their present evidence, for Restore."""
    _admin(db, user, org_id)
    marks = _marks(db, org_id)
    return [s for s in _current(db, org_id) if marks.get(s["id"]) == s["fingerprint"]]


def restore(db: Session, user, org_id: str, suggestion_id: str) -> None:
    _admin(db, user, org_id)
    row = db.get(OrgRuleSuggestionDismissal, (org_id, suggestion_id))
    if row is None:
        raise NotFound("That suggestion is not dismissed")
    db.delete(row)
    db.commit()


# ---- linked boxes pull the org's decisions (GRPH-1297) -----------------------------------
#
# "The box pushes; the control plane never reaches in." The cloud only PUBLISHES a versioned
# record on the sync credential; the box pulls it, applies it, and reports the version it
# applied. The cloud's "last applied" is therefore only ever what the box said.

#: A box is flagged when it has not applied the published version this long after the
#: decision changed (it pulls on its sync cadence).
EXPECTED_INTERVAL = timedelta(hours=1)


def _version(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _decisions(db: Session, org_id: str) -> dict:
    org = db.get(Organization, org_id)
    toggles = [{"rule_id": r.rule_id, "on": r.on, "params": r.params, "locked": bool(r.locked)}
               for r in db.scalars(select(RuleToggle).where(RuleToggle.org_id == org_id)
                                   .order_by(RuleToggle.rule_id))]
    ev = db.scalar(select(Event).where(Event.action == "apply_rule_profile",
                                       Event.target_type == "org", Event.target_id == org_id)
                   .order_by(Event.id.desc()).limit(1))
    profile = ({"id": (ev.meta or {}).get("profile"), "version": (ev.meta or {}).get("profile_version")}
               if ev is not None else None)
    return {"org_id": org_id, "org_name": org.name if org else "", "toggles": toggles,
            "profile": profile}


def note_published(db: Session, org_id: str) -> OrgRuleVersion:
    """CLOUD: stamp the org's published version, moving `changed_at` only when the version
    moved. Called after every org decision write, and on every read as a backstop, so a
    dropped toggle or an applied profile moves it just as a set does."""
    version = _version(_decisions(db, org_id))
    row = db.get(OrgRuleVersion, org_id)
    if row is None:
        row = OrgRuleVersion(org_id=org_id, version=version, changed_at=utcnow())
        db.add(row)
        db.commit()
    elif row.version != version:
        row.version, row.changed_at = version, utcnow()
        db.commit()
    return row


def published_decisions(db: Session, org_id: str) -> dict:
    """CLOUD: the org's rule decisions as one versioned record — toggles with params, locks,
    and the profile last applied. The version is a hash of the decisions, so it moves exactly
    when a decision does; `changed_at` is when it last moved."""
    body = _decisions(db, org_id)
    stamp = note_published(db, org_id)
    return {**body, "version": _version(body), "changed_at": stamp.changed_at}


def record_box_applied(db: Session, key: ApiKey, org_id: str, version: str) -> BoxRuleSync:
    """CLOUD: a box reports the version it applied. Per sync key, latest report wins."""
    row = db.get(BoxRuleSync, key.id)
    now = utcnow()
    if row is None:
        row = BoxRuleSync(key_id=key.id, org_id=org_id)
        db.add(row)
    if row.applied_version != version:
        row.applied_at = now
    row.org_id, row.applied_version, row.reported_at = org_id, version, now
    db.commit()
    return row


def box_rule_state(db: Session, key_id: str, published: dict) -> dict:
    """CLOUD: one box's rule-sync state against the published record — what the console
    reads as "pending: last synced <time>". Never claims a version the box did not report."""
    row = db.get(BoxRuleSync, key_id)
    applied = row.applied_version if row else None
    current = applied == published["version"]
    overdue = False
    if not current:
        since = published.get("changed_at")
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        overdue = bool(since and utcnow() - since > EXPECTED_INTERVAL)
    return {"applied_version": applied, "applied_at": row.applied_at if row else None,
            "published_version": published["version"],
            "pending": not current, "overdue": overdue}


def org_box_states(db: Session, user, org_id: str) -> dict:
    """CLOUD: every linked box's rule-sync state for an org admin (JWT), the same
    `box_rule_state` a box reads with its sync key. Boxes are grouped by deployment name,
    as the box window groups them. Reads only what boxes reported; never reaches into one."""
    from app.services import galaxy as galaxy_svc  # galaxy imports items → avoid a cycle

    _admin(db, user, org_id)
    published = published_decisions(db, org_id)
    groups: dict[str, dict] = {}
    for d in galaxy_svc.deployments(db, org_id):
        if d["revoked"]:
            continue
        g = groups.setdefault(d["label"], {"name": d["label"], "projects": set(),
                                           "rules": None})
        if g["rules"] is None:
            g["rules"] = box_rule_state(db, d["credential_id"], published)
        g["projects"].add(d["project_id"])
    boxes = [{"name": g["name"], "projects": sorted(g["projects"]), **g["rules"]}
             for g in groups.values()]
    return {"org_id": org_id, "published_version": published["version"],
            "published_at": published["changed_at"], "boxes": boxes}


def pulled_org_id(db: Session) -> str | None:
    """BOX: the org whose decisions this box has applied, if any."""
    row = db.get(RuleDecisionPull, "instance")
    return row.org_id if row is not None and row.org_id and row.applied_version else None


def apply_decisions(db: Session, record: dict) -> RuleDecisionPull:
    """BOX: write a pulled record as the org layer of this box's own resolution (project →
    org → platform, locks hold). Replaces the org's rows wholesale, so a toggle the org has
    dropped stops applying. Records the version applied and when."""
    org_id, version = record.get("org_id"), record.get("version")
    if not org_id or not version or not isinstance(record.get("toggles"), list):
        raise Validation("a rule-decision record needs org_id, version and toggles")
    if db.get(Organization, org_id) is None:
        db.add(Organization(id=org_id, name=record.get("org_name") or org_id))
        db.flush()
    wanted = {t["rule_id"]: t for t in record["toggles"]}
    for row in db.scalars(select(RuleToggle).where(RuleToggle.org_id == org_id)):
        if row.rule_id not in wanted:
            db.delete(row)
    existing = {r.rule_id: r for r in db.scalars(
        select(RuleToggle).where(RuleToggle.org_id == org_id))}
    for rid, t in wanted.items():
        row = existing.get(rid) or RuleToggle(rule_id=rid, org_id=org_id)
        row.on, row.params, row.locked = t.get("on"), t.get("params"), bool(t.get("locked"))
        db.add(row)
    pull = db.get(RuleDecisionPull, "instance") or RuleDecisionPull(id="instance")
    prof = record.get("profile") or {}
    pull.org_id, pull.applied_version, pull.applied_at = org_id, version, utcnow()
    pull.profile, pull.profile_version, pull.reported = prof.get("id"), prof.get("version"), False
    db.add(pull)
    db.commit()
    return pull
