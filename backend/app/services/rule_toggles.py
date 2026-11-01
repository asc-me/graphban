"""Library rule toggles (GRPH-1262 / PRD-P63 §8, S2a): state and params, layered.

A library rule is one with a real primitive behind it and a switch in front of it (D3). This
module holds the switch: per rule id, an on/off state and its params at three scopes. The
effective value resolves **project → org → platform default** (D4) and every read says which
layer it came from, like gitops linking (PRD-31). The platform default is the library's own
declaration (`LIBRARY`); it has no row.

- **A lock fixes the value for projects, not for the org admin** (D4). A project write on a
  locked rule is refused naming `LOCK_RULE`. An org write applies to every project at once and
  is ONE audit event naming the rule, the old and new values, and the projects it changed.
- **A person adopts; nothing else does** (D10). Every write takes the acting `User`; any other
  identity — an API key, the recommendation engine's — is refused naming `PERSON_RULE`. That is
  why no MCP tool writes a toggle: an MCP call is an agent's, and an agent able to switch off
  the rule that reviews it would make the rule decoration.
- **Every change says who and why.** A write without a `reason` is refused.
- **Org-level writes are premium** (D11): `quotas.require_org_rules`, on self-host too.

Templates reach `LIBRARY` through `register` — the signed pack loader (GRPH-1267) calls it;
until a pack is loaded the library is empty and so is every read, which `library_loaded` says
rather than leaving an empty list to read as "no rules".
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound, Refused, Validation
from app.models import ApiKey, Project, RuleToggle, User
from app.security import authz
from app.services import events as events_svc
from app.services import quotas

PERSON_RULE = "rules.person_adopts"
LOCK_RULE = "rules.org_lock_holds"
ADMIN_RULE = "rules.toggle_needs_admin"

#: The `source` vocabulary of every resolved value, strongest layer first.
SOURCES = ("project", "org", "platform default")
PROJECT_ADMIN_ROLES = ("owner", "admin")


@dataclass(frozen=True)
class LibraryRule:
    """A library rule as its template declares it, bound to its primitive's level (D3)."""

    id: str
    stage: str
    title: str
    summary: str
    #: What the PRIMITIVE provides (`enforced` | `checked`), never what a template claims.
    enforcement: str
    #: The primitive: `rules.Enforcer`, so the registry draws it like any other rule.
    enforcer: object
    default_on: bool = False
    default_params: dict = field(default_factory=dict)


#: rule id -> its declaration. The platform default layer.
LIBRARY: dict[str, LibraryRule] = {}


def register(rule: LibraryRule) -> None:
    """Add (or replace) a library rule. Called by the pack loader, and by tests."""
    LIBRARY[rule.id] = rule


def library_loaded() -> bool:
    return bool(LIBRARY)


def _rule(rule_id: str) -> LibraryRule:
    rule = LIBRARY.get(rule_id)
    if rule is None:
        raise NotFound(f"{rule_id!r} is not a library rule",
                       hint="only rules in the loaded pack have a toggle")
    return rule


def _row(db: Session, rule_id: str, *, org_id: str | None = None,
         project_id: str | None = None) -> RuleToggle | None:
    if org_id is None and project_id is None:
        return None
    q = select(RuleToggle).where(RuleToggle.rule_id == rule_id)
    q = q.where(RuleToggle.org_id == org_id) if org_id else q.where(
        RuleToggle.project_id == project_id)
    return db.scalar(q)


def _org_of(db: Session, project_id: str | None) -> str | None:
    p = db.get(Project, project_id) if project_id else None
    return p.org_id if p else None


def _resolve(rule: LibraryRule, proj: RuleToggle | None, org: RuleToggle | None,
             org_id: str | None) -> dict:
    locked = bool(org is not None and org.locked)
    # A lock takes the project layer out of resolution; the project's own row is kept, so
    # unlocking gives the project back what it had set.
    layers = ([] if locked else [("project", proj)]) + [("org", org)]

    def first(attr: str, default):
        for source, row in layers:
            if row is not None and getattr(row, attr) is not None:
                return {"value": copy.deepcopy(getattr(row, attr)), "source": source}
        return {"value": copy.deepcopy(default), "source": "platform default"}

    return {"rule_id": rule.id, "on": first("on", rule.default_on),
            "params": first("params", rule.default_params),
            "locked": locked, "locked_by": org_id if locked else None}


def resolve(db: Session, rule_id: str, project_id: str | None) -> dict:
    """The effective state and params of one rule for one project, each with its source."""
    rule = _rule(rule_id)
    org_id = _org_of(db, project_id)
    return _resolve(rule, _row(db, rule_id, project_id=project_id),
                    _row(db, rule_id, org_id=org_id), org_id)


def effective(db: Session, project_id: str | None) -> list[dict]:
    """Every library rule's effective toggle for a project, in library order."""
    org_id = _org_of(db, project_id)
    rows = {(r.rule_id, "org" if r.org_id else "project"): r for r in db.scalars(
        select(RuleToggle).where(
            (RuleToggle.project_id == project_id) if not org_id else
            ((RuleToggle.project_id == project_id) | (RuleToggle.org_id == org_id))))
    } if project_id else {}
    return [dict(_resolve(r, rows.get((r.id, "project")), rows.get((r.id, "org")), org_id),
                 stage=r.stage, title=r.title)
            for r in LIBRARY.values()]


# ---- writes -------------------------------------------------------------------------------

def _require_person(db: Session, actor: object) -> User:
    """D10: only a person's request sets a toggle. An API key is an agent's credential, and the
    recommendation engine has no user at all — both are refused, never quietly attributed."""
    if isinstance(actor, User) and db.get(User, actor.id) is not None:
        return actor
    who = ("an API key" if isinstance(actor, ApiKey)
           else f"{actor[1]!r}" if isinstance(actor, tuple) and len(actor) > 1
           else type(actor).__name__)
    raise Refused(403, f"only a person sets a rule toggle; this request came from {who}",
                  rule_id=PERSON_RULE,
                  hint="recommendations and agents suggest; a person adopts from the Rules page")


def _reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise Validation("a toggle change needs a `reason`",
                         hint="say why, in a sentence; it is the audit record")
    return text


def _check_params(rule: LibraryRule, params: dict) -> dict:
    """Params are the template's own keys, each the type of its default. An unknown key is
    refused rather than stored, where it would read as a setting the primitive honours."""
    if not isinstance(params, dict):
        raise Validation("`params` must be an object")
    unknown = sorted(set(params) - set(rule.default_params))
    if unknown:
        raise Validation(f"{rule.id} has no param(s) {', '.join(unknown)}",
                         hint=f"known: {', '.join(sorted(rule.default_params)) or 'none'}")
    for k, v in params.items():
        want = type(rule.default_params[k])
        if not isinstance(v, want) or (want is int and isinstance(v, bool)):
            raise Validation(f"{rule.id} param {k!r} must be {want.__name__}")
    return {**rule.default_params, **params}


def _apply(row: RuleToggle, rule: LibraryRule, changes: dict) -> None:
    if "on" in changes:
        if changes["on"] is not None and not isinstance(changes["on"], bool):
            raise Validation("`on` must be true, false or null (inherit)")
        row.on = changes["on"]
    if "params" in changes:
        row.params = None if changes["params"] is None else _check_params(rule, changes["params"])


def _snapshot(row: RuleToggle | None) -> dict:
    if row is None:
        return {"on": None, "params": None, "locked": False}
    return {"on": row.on, "params": copy.deepcopy(row.params), "locked": bool(row.locked)}


def _value(r: dict) -> dict:
    return {"on": r["on"]["value"], "params": r["params"]["value"]}


def set_project_toggle(db: Session, project_id: str, rule_id: str, changes: dict, *,
                       actor: object, reason: str | None) -> dict:
    """A project's own value for a rule. `changes` holds `on` and/or `params`; `None` for
    either clears the project's value so it inherits again. Every plan (D11)."""
    user = _require_person(db, actor)
    rule = _rule(rule_id)
    why = _reason(reason)
    authz.require_writable(db, user.id, project_id)
    if authz.project_role(db, user.id, project_id) not in PROJECT_ADMIN_ROLES:
        raise Refused(403, "setting a rule toggle needs the project's owner or an admin",
                      rule_id=ADMIN_RULE)
    unknown = sorted(set(changes) - {"on", "params"})
    if unknown or not changes:
        raise Validation(f"a project toggle sets `on` and/or `params`"
                         + (f"; not {', '.join(unknown)}" if unknown else ""))
    before = resolve(db, rule_id, project_id)
    if before["locked"]:
        raise Refused(409, f"{rule_id} is locked by the org; a project cannot override it",
                      rule_id=LOCK_RULE, meta={"locked_rule": rule_id, "org_id": before["locked_by"]},
                      hint="ask an org admin to change the org's value or unlock the rule")
    row = _row(db, rule_id, project_id=project_id)
    old = _snapshot(row)
    if row is None:
        row = RuleToggle(rule_id=rule_id, project_id=project_id)
        db.add(row)
    _apply(row, rule, changes)
    row.updated_by = user.id
    db.commit()
    after = resolve(db, rule_id, project_id)
    events_svc.record_user(
        db, user, action="set_rule_toggle", target_type="rule", target_id=rule_id,
        project_id=project_id,
        meta={"scope": "project", "reason": why, "old": old, "new": _snapshot(row),
              "effective_before": _value(before), "effective_after": _value(after)})
    return after


def set_org_toggle(db: Session, org_id: str, rule_id: str, changes: dict, *, actor: object,
                   reason: str | None) -> dict:
    """The org's value for a rule, and its lock. Applies to every project that inherits it —
    every project at all, when locked — recorded as ONE event naming the projects it changed."""
    user = _require_person(db, actor)
    rule = _rule(rule_id)
    why = _reason(reason)
    authz.require_org_admin(db, user.id, org_id)
    quotas.require_org_rules(db, org_id)
    unknown = sorted(set(changes) - {"on", "params", "locked"})
    if unknown or not changes:
        raise Validation(f"an org toggle sets `on`, `params` and/or `locked`"
                         + (f"; not {', '.join(unknown)}" if unknown else ""))
    if "locked" in changes and not isinstance(changes["locked"], bool):
        raise Validation("`locked` must be true or false")
    project_ids = list(db.scalars(select(Project.id).where(Project.org_id == org_id)
                                  .order_by(Project.id)))
    before = {pid: _value(resolve(db, rule_id, pid)) for pid in project_ids}
    row = _row(db, rule_id, org_id=org_id)
    old = _snapshot(row)
    if row is None:
        row = RuleToggle(rule_id=rule_id, org_id=org_id)
        db.add(row)
    _apply(row, rule, changes)
    if "locked" in changes:
        row.locked = changes["locked"]
    if row.locked:
        # A lock fixes a value. Locking with nothing set would leave projects "locked" to the
        # platform default with the org shown as nobody — so the default is written down.
        if row.on is None:
            row.on = rule.default_on
        if row.params is None:
            row.params = dict(rule.default_params)
    row.updated_by = user.id
    db.commit()
    changed = [pid for pid in project_ids if _value(resolve(db, rule_id, pid)) != before[pid]]
    events_svc.record_user(
        db, user, action="set_rule_toggle", target_type="rule", target_id=rule_id,
        meta={"scope": "org", "org_id": org_id, "reason": why, "old": old,
              "new": _snapshot(row), "projects_changed": changed})
    return {"rule_id": rule_id, "org_id": org_id, **_snapshot(row), "projects_changed": changed}
