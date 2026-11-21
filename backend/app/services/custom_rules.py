"""Custom rules: a rule a person writes from a library primitive (GRPH-1774 / P91 D1).

A custom rule is a template the project writes. It is validated by the same
`rule_pack.validate_draft` the pack loader uses, and it resolves like a library rule — one
`resolve()` / `effective()` read — but its layer is the project's own row, so every read says
`source: custom`.

- **A person writes it** (`rules.person_adopts`): `create` takes the acting `User`; an API key
  or any other identity is refused naming `PERSON_RULE`. No MCP tool writes one.
- **Takes effect at once.** A person authoring directly saves it `active`.
- **Never half-enforced.** The draft is re-validated on every read against the primitive API it
  was written for. A primitive that no longer loads (major break, primitive gone) draws the rule
  UNMEASURED with the reason and resolves it off.
- **Org scope** (GRPH-1779 / P91 D7, premium: `quotas.require_org_rules`). One org-owned row
  (`org_id` set) is resolved into each project through project -> org; it is never copied onto
  a project. A project row of the same id is a kept *override* (drawn as one) until the org
  locks the rule: the lock retires every override, with a history entry per project, and a
  project write against it is refused naming `rule_toggles.LOCK_RULE`. The lock lives on the
  org's `RuleToggle` row for the id, as for a library rule.
"""
from __future__ import annotations

import copy
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound, Refused, Validation
from app.models import CustomRule, Project, RuleToggle, User
from app.security import authz
from app.services import quotas
from app.services import events as events_svc
from app.services import rule_pack
from app.services import rule_toggles as T

PREFIX = "custom."
STATES = ("observing", "active")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SOURCE = "custom"


def is_custom(rule_id: str) -> bool:
    return rule_id.startswith(PREFIX)


def _draft(row: CustomRule) -> dict:
    return {"id": row.rule_id, "stage": row.stage, "title": row.title, "summary": row.why,
            "primitive": row.primitive, "claims": row.claims, "params": row.params or {}}


def unmeasured_reason(row: CustomRule) -> str:
    """Why this rule no longer loads against the primitive API it was written for, or ""."""
    # The stored stage was the contract's at write time; the live contract may have moved it.
    prim = rule_pack.PRIMITIVES.get(row.primitive)
    draft = _draft(row)
    if prim is not None:
        draft["stage"] = prim.stage
    return rule_pack.validate_draft(draft, api=row.primitive_api)


def library_rule(row: CustomRule) -> T.LibraryRule:
    reason = unmeasured_reason(row)
    prim = rule_pack.PRIMITIVES.get(row.primitive)
    ok = not reason
    return T.LibraryRule(
        id=row.rule_id, stage=row.stage.lower(), title=row.title, summary=row.why,
        enforcement=prim.level if ok and prim else "unmeasured",
        enforcer=prim.enforcer if prim else rule_pack.Enforcer("doc", "docs/data-model.md"),
        default_on=ok and row.state == "active",
        default_params=copy.deepcopy(row.params or {}) if ok else {},
        unmeasured_reason=reason)


def _row(db: Session, rule_id: str, project_id: str | None) -> CustomRule | None:
    """The project's OWN row for the id (an override when the org also owns the rule)."""
    if project_id is None:
        return None
    return db.scalar(select(CustomRule).where(CustomRule.rule_id == rule_id,
                                              CustomRule.project_id == project_id))


def _org_row(db: Session, rule_id: str, org_id: str | None) -> CustomRule | None:
    if org_id is None:
        return None
    return db.scalar(select(CustomRule).where(CustomRule.rule_id == rule_id,
                                              CustomRule.org_id == org_id))


def _lock_row(db: Session, rule_id: str, org_id: str | None) -> RuleToggle | None:
    if org_id is None:
        return None
    return db.scalar(select(RuleToggle).where(RuleToggle.rule_id == rule_id,
                                              RuleToggle.org_id == org_id))


def _is_locked(db: Session, rule_id: str, org_id: str | None) -> bool:
    lock = _lock_row(db, rule_id, org_id)
    return bool(lock is not None and lock.locked and _org_row(db, rule_id, org_id) is not None)


def layer(db: Session, rule_id: str, project_id: str | None) -> tuple[CustomRule, dict]:
    """The row that holds for a project, and how it holds: `{source, locked, locked_by,
    override}`. A lock puts the org's row first; otherwise project -> org."""
    own = _row(db, rule_id, project_id)
    org_id = T._org_of(db, project_id)
    org = _org_row(db, rule_id, org_id)
    locked = org is not None and _is_locked(db, rule_id, org_id)
    if locked:
        return org, {"source": "org", "locked": True, "locked_by": org_id, "override": False}
    if own is not None:
        return own, {"source": SOURCE, "locked": False, "locked_by": None,
                     "override": org is not None}
    if org is not None:
        return org, {"source": "org", "locked": False, "locked_by": None, "override": False}
    raise NotFound(f"{rule_id!r} is not a custom rule of this project")


def get(db: Session, rule_id: str, project_id: str | None) -> CustomRule:
    return layer(db, rule_id, project_id)[0]


def refuse_if_locked(db: Session, rule_id: str, project_id: str) -> None:
    """Refuse a project-level write to a custom rule its org has locked, naming `LOCK_RULE`."""
    org_id = T._org_of(db, project_id)
    if _is_locked(db, rule_id, org_id):
        raise Refused(409, f"{rule_id} is locked by the org; a project cannot override it",
                      rule_id=T.LOCK_RULE, meta={"locked_rule": rule_id, "org_id": org_id},
                      hint="ask an org admin to change the org's rule or unlock it")


def resolve_row(row: CustomRule, how: dict | None = None) -> dict:
    """`rule_toggles.resolve`'s shape for a custom rule. `source` is `custom` for the project's
    own row and `org` for the org's; an UNMEASURED rule resolves off."""
    how = how or {"source": SOURCE, "locked": False, "locked_by": None, "override": False}
    src = how["source"]
    lib = library_rule(row)
    on = lib.default_on
    return {"rule_id": row.rule_id,
            "on": {"value": on, "source": src},
            "params": {"value": copy.deepcopy(row.params or {}), "source": src},
            "inherits": {"on": src, "params": src},
            "inherited": {"on": on, "params": copy.deepcopy(row.params or {})},
            "locked": how["locked"], "locked_by": how["locked_by"],
            "override": how["override"], "scope": "org" if row.org_id else "project",
            "state": row.state, "unmeasured_reason": lib.unmeasured_reason or None}


def _observe(db: Session, row: CustomRule, out: dict, project_id: str | None) -> dict:
    if row.state == "observing":  # GRPH-1777: what it recorded, and whether a decision is due
        from app.services import rule_observations
        out["observation"] = rule_observations.summary(db, project_id, row.rule_id,
                                                       row.created_at)
    return out


def resolve(db: Session, rule_id: str, project_id: str | None) -> dict:
    row, how = layer(db, rule_id, project_id)
    return _observe(db, row, resolve_row(row, how), project_id)


def rows_for_project(db: Session, project_id: str | None) -> list[CustomRule]:
    """The rows that hold for a project: its own, plus the org's it does not override (a lock
    puts the org's row in place of the project's)."""
    if project_id is None:
        return []
    own = list(db.scalars(select(CustomRule).where(CustomRule.project_id == project_id)
                          .order_by(CustomRule.id)))
    org_id = T._org_of(db, project_id)
    out = {r.rule_id: r for r in own}
    if org_id:
        for o in db.scalars(select(CustomRule).where(CustomRule.org_id == org_id)
                            .order_by(CustomRule.id)):
            if o.rule_id not in out or _is_locked(db, o.rule_id, org_id):
                out[o.rule_id] = o
    return list(out.values())


def effective(db: Session, project_id: str | None) -> list[dict]:
    out = []
    for r in rows_for_project(db, project_id):
        row, how = layer(db, r.rule_id, project_id)
        out.append(dict(_observe(db, row, resolve_row(row, how), project_id),
                        stage=row.stage.lower(), title=row.title))
    return out


def _author(db: Session, row: CustomRule) -> str | None:
    u = db.get(User, row.author_id) if row.author_id else None
    return (u.handle or u.name) if u else None


def view(db: Session, row: CustomRule, project_id: str | None = None) -> dict:
    row, how = layer(db, row.rule_id, project_id or row.project_id) if (
        project_id or row.project_id) else (row, None)
    r = resolve_row(row, how)
    return {"rule_id": row.rule_id, "primitive": row.primitive, "stage": row.stage,
            "title": row.title, "why": row.why, "claims": row.claims, "params": row.params,
            "state": row.state, "author": _author(db, row), "reason": row.reason,
            "created_at": row.created_at, "on": r["on"]["value"], "source": r["on"]["source"],
            "scope": r["scope"], "locked": r["locked"], "override": r["override"],
            "unmeasured_reason": r["unmeasured_reason"]}


def list_for_project(db: Session, project_id: str) -> list[dict]:
    return [view(db, r, project_id) for r in rows_for_project(db, project_id)]


def _validated(slug: str, primitive: str, title: str, why: str, params: dict | None,
               claims: str | None) -> tuple[str, dict, object]:
    if not SLUG.match(slug or ""):
        raise Validation("`slug` must be lower-case letters, digits, `-` or `_`, 1-64 long")
    if not (title or "").strip() or not (why or "").strip():
        raise Validation("a custom rule needs a `title` and a `why`")
    prim = rule_pack.PRIMITIVES.get(primitive)
    if prim is None:
        raise Validation(f"primitive {primitive!r} does not exist in primitive API "
                         f"{rule_pack.PRIMITIVE_API}")
    rule_id = PREFIX + slug
    draft = {"id": rule_id, "stage": prim.stage, "title": title.strip(), "summary": why.strip(),
             "primitive": primitive, "claims": claims or prim.level, "params": params or {}}
    bad = rule_pack.validate_draft(draft)
    if bad:
        raise Validation(bad)
    return rule_id, draft, prim


def create(db: Session, actor: object, project_id: str, *, slug: str, primitive: str,
           title: str, why: str, reason: str, params: dict | None = None,
           claims: str | None = None) -> CustomRule:
    """Save a custom rule, `active` at once. A person's write, validated by `validate_draft`.
    When the org owns the same id this becomes the project's kept override; when the org has
    locked it, the write is refused naming `rules.org_lock_holds`."""
    user = T._check_project_writer(db, actor, project_id)
    reason = T._reason(reason)
    rule_id, draft, _ = _validated(slug, primitive, title, why, params, claims)
    refuse_if_locked(db, rule_id, project_id)
    if _row(db, rule_id, project_id) is not None:
        raise Validation(f"{rule_id} already exists in this project",
                         hint="pick another slug")
    row = CustomRule(rule_id=rule_id, project_id=project_id, primitive=primitive,
                     primitive_api=rule_pack.PRIMITIVE_API, stage=draft["stage"],
                     params=copy.deepcopy(draft["params"]), title=draft["title"],
                     why=draft["summary"], claims=draft["claims"], state="active",
                     author_id=user.id, reason=reason)
    db.add(row)
    db.flush()
    events_svc.record_user(db, user, action="create_custom_rule", target_type="rule",
                           target_id=rule_id, project_id=project_id,
                           meta={"primitive": primitive, "params": row.params, "reason": reason})
    db.commit()
    return row


# ---- org scope (GRPH-1779 / P91 D7) ------------------------------------------------------

def _org_admin(db: Session, actor: object, org_id: str) -> User:
    """A person who administers the org, on a plan with org rules (`require_org_rules`)."""
    user = T._require_person(db, actor)
    authz.require_org_admin(db, user.id, org_id)
    quotas.require_org_rules(db, org_id)
    return user


def _org_projects(db: Session, org_id: str) -> list[str]:
    return list(db.scalars(select(Project.id).where(Project.org_id == org_id)
                           .order_by(Project.id)))


def stage_org_row(db: Session, user: User, org_id: str, *, rule_id: str, primitive: str,
                  stage: str, params: dict, title: str, why: str, claims: str, state: str,
                  reason: str) -> CustomRule:
    """Add the org's one row for `rule_id`, uncommitted. Refused when the org has one already."""
    if _org_row(db, rule_id, org_id) is not None:
        raise Validation(f"{rule_id} already exists in this org", hint="pick another slug")
    row = CustomRule(rule_id=rule_id, org_id=org_id, primitive=primitive,
                     primitive_api=rule_pack.PRIMITIVE_API, stage=stage,
                     params=copy.deepcopy(params or {}), title=title, why=why, claims=claims,
                     state=state, author_id=user.id, reason=reason)
    db.add(row)
    db.flush()
    return row


def org_effect(db: Session, org_id: str, rule_id: str) -> dict:
    """Which of the org's projects take the org's rule, and which keep an override."""
    pids = _org_projects(db, org_id)
    own = {p for p in pids if _row(db, rule_id, p) is not None}
    return {"projects_changed": [p for p in pids if p not in own],
            "overrides_kept": sorted(own)}


def create_org(db: Session, actor: object, org_id: str, *, slug: str, primitive: str,
               title: str, why: str, reason: str, params: dict | None = None,
               claims: str | None = None) -> CustomRule:
    """An org admin authors the org's one rule `custom.<slug>`; it takes effect at once. It
    resolves into every project; a project that adopted it first keeps its own row as an
    override. One audit event names the projects it changed."""
    user = _org_admin(db, actor, org_id)
    why_reason = T._reason(reason)
    rule_id, draft, _ = _validated(slug, primitive, title, why, params, claims)
    row = stage_org_row(db, user, org_id, rule_id=rule_id, primitive=primitive,
                        stage=draft["stage"], params=draft["params"], title=draft["title"],
                        why=draft["summary"], claims=draft["claims"], state="active",
                        reason=why_reason)
    db.commit()
    events_svc.record_user(
        db, user, action="create_org_custom_rule", target_type="rule", target_id=rule_id,
        meta={"scope": "org", "org_id": org_id, "reason": why_reason, "primitive": primitive,
              "params": row.params, **org_effect(db, org_id, rule_id)})
    return row


def list_for_org(db: Session, org_id: str) -> list[dict]:
    rows = db.scalars(select(CustomRule).where(CustomRule.org_id == org_id)
                      .order_by(CustomRule.rule_id)).all()
    return [view(db, r) for r in rows]


def get_org(db: Session, org_id: str, rule_id: str) -> CustomRule:
    row = _org_row(db, rule_id, org_id)
    if row is None:
        raise NotFound(f"{rule_id!r} is not a custom rule of this org")
    return row


def promote_org(db: Session, actor: object, org_id: str, rule_id: str, reason: str) -> CustomRule:
    """An org admin promotes the org's observing rule to `active`."""
    user = _org_admin(db, actor, org_id)
    why = T._reason(reason)
    row = get_org(db, org_id, rule_id)
    if row.state != "observing":
        raise Validation(f"{rule_id} is not observing")
    row.state = "active"
    db.commit()
    events_svc.record_user(db, user, action="promote_rule", target_type="rule",
                           target_id=rule_id,
                           meta={"scope": "org", "org_id": org_id, "reason": why,
                                 **org_effect(db, org_id, rule_id)})
    return row


def set_org_toggle(db: Session, user: User, org_id: str, rule_id: str, changes: dict, *,
                   reason: str | None, dry_run: bool = False) -> dict:
    """Lock or unlock the org's custom rule (the only org toggle a custom rule has). Locking
    retires every project override of it — each with a history entry naming
    `rules.org_lock_holds` — and is ONE org audit event naming the projects it changed."""
    why = T._reason(reason)
    unknown = sorted(set(changes) - {"locked"})
    if unknown or "locked" not in changes or not isinstance(changes["locked"], bool):
        raise Validation("an org custom rule toggle sets `locked` (true or false) only")
    org_rule = get_org(db, org_id, rule_id)
    if changes["locked"] and org_rule.state == "observing":
        raise Validation(f"{rule_id} is observing and cannot be locked",
                         hint="promote it first: a lock would retire project overrides for a rule "
                              "that holds nothing")
    lock = _lock_row(db, rule_id, org_id)
    old = {"locked": bool(lock is not None and lock.locked)}
    if lock is None:
        lock = RuleToggle(rule_id=rule_id, org_id=org_id)
        db.add(lock)
    lock.locked = changes["locked"]
    lock.updated_by = user.id
    retired: list[dict] = []
    if lock.locked and not old["locked"]:
        for pid in _org_projects(db, org_id):
            prow = _row(db, rule_id, pid)
            if prow is not None:
                retired.append({"project_id": pid, "params": prow.params, "state": prow.state})
                db.delete(prow)
    changed = [r["project_id"] for r in retired]
    out = {"rule_id": rule_id, "org_id": org_id, "locked": lock.locked,
           "projects_changed": changed, "retired_overrides": retired}
    if dry_run:
        db.rollback()
        return out
    db.commit()
    events_svc.record_user(
        db, user, action="set_rule_toggle", target_type="rule", target_id=rule_id,
        meta={"scope": "org", "org_id": org_id, "reason": why, "old": old,
              "new": {"locked": lock.locked}, "projects_changed": changed,
              "retired_overrides": retired})
    for r in retired:
        events_svc.record_user(
            db, user, action="retire_rule_override", target_type="rule", target_id=rule_id,
            project_id=r["project_id"],
            meta={"rule_id": T.LOCK_RULE, "reason": why, "override": r,
                  "history": f"project override retired: the org locked {rule_id} "
                             f"({T.LOCK_RULE})"})
    return out
