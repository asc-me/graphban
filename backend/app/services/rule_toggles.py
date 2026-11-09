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
import time
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
    #: Set by the pack loader when the template was refused (D12): the rule is drawn
    #: UNMEASURED with this reason and is never half-enforced.
    unmeasured_reason: str = ""


#: rule id -> its declaration. The platform default layer.
LIBRARY: dict[str, LibraryRule] = {}

#: GRPH-1405: where the test suites run is a project decision. Mode -> (label, what it means).
TEST_SCOPE_RULE = "build.test_scope"
TEST_SCOPE_DEFAULT = "agent_and_pr"
#: GRPH-1429: the engines a full local run must cover under `agent_full`.
TEST_SCOPE_ENGINES_DEFAULT = ["postgres", "sqlite"]
TEST_SCOPE_MODES: dict[str, tuple[str, str]] = {
    "agent_full": ("The agent runs CI",
                   "The builder runs the full suites; the PR's CI is not required."),
    "agent_and_pr": ("The agent and the PR run CI",
                     "The builder runs the full suites and the PR's CI runs them again."),
    "agent_targeted": ("The agent runs partial CI, only what changed",
                       "The builder runs the tests it wrote or changed, the module's existing "
                       "tests and the sabotage check; the PR's CI runs the full suites."),
}
#: GRPH-1425: whether a branch with a file outside every declared touchpoint is held back from
#: a PR or only warned about. The merge-time `covering` check (GRPH-1408) is not this rule's.
UNDECLARED_FILES_RULE = "build.undeclared_files"
UNDECLARED_FILES_DEFAULT = "hold"
UNDECLARED_FILES_MODES: dict[str, tuple[str, str]] = {
    "hold": ("Hold the PR",
             "The PR is held and a wait names the files until the touchpoints are widened."),
    "warn": ("Open the PR and warn",
             "The PR is opened; the files are recorded as a wait and a note on the item, and "
             "the merge still refuses a file no signed-off item covers."),
}

#: GRPH-1423: the paths whose leases are exclusive, because git cannot see two agents collide
#: there (two new migrations merge cleanly and leave two heads). A project sets its own list.
EXCLUSIVE_AREAS_RULE = "plan.exclusive_areas"
EXCLUSIVE_AREAS_DEFAULT: tuple[str, ...] = ("backend/alembic/versions",)

#: GRPH-1424: generated files the supervisor's undeclared-file hold ignores. Only the
#: supervisor honours it (level `checked`); the default is the built-in list it always had.
GENERATED_FILES_RULE = "build.generated_files"
GENERATED_FILES_DEFAULT: list[str] = [
    ".agents/skills/", ".claude/agents/", ".claude/skills/", ".codex/agents/",
    ".cursor/agents/", ".cursor/plugins/graphban/", ".cursor/rules/graphban.mdc",
    "AGENTS.md", "backend/tests/shard_weights.json",
]
#: A string param that takes one of a closed set: rule id -> param -> allowed values.
PARAM_CHOICES: dict[str, dict[str, tuple[str, ...]]] = {
    TEST_SCOPE_RULE: {"mode": tuple(TEST_SCOPE_MODES)},
    UNDECLARED_FILES_RULE: {"mode": tuple(UNDECLARED_FILES_MODES)},
}


#: GRPH-1426: the predicates a project's `done` needs, over the deployment's floor.
DONE_PREDICATES_RULE = "merge.done_predicates"
#: Required for any item with a linked PR (`items.refuse_if_pr_cooling_down`); locked in a set
#: list until GRPH-1059 decides whether it can ever be dropped.
CI_FLOOR_PREDICATE = "suite_green"


def register(rule: LibraryRule) -> None:
    """Add (or replace) a library rule. Called by the pack loader, and by tests."""
    LIBRARY[rule.id] = rule


#: GRPH-1418: seconds a worker trusts its loaded library before re-reading the installed pack's
#: sha. Each uvicorn worker has its own `LIBRARY`; another worker's install shows up here.
SYNC_TTL = 2.0
_last_sync = 0.0


def sync_library(db: Session) -> None:
    """Reload this worker's `LIBRARY` when another worker installed a different pack.

    Called ONCE, at the start of a request (`app.db.get_db`), before anything reads `LIBRARY`
    — the gates' `RULE not in LIBRARY` guards included. Never mid-request: a reload between a
    guard and the `resolve` behind it would 404 a gate on a rule the new pack dropped. One
    indexed read at most every `SYNC_TTL` seconds; a no-op when no pack is installed."""
    global _last_sync
    now = time.monotonic()
    if now - _last_sync < SYNC_TTL:
        return
    _last_sync = now
    from app.services import rule_pack
    rule_pack.sync(db)


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
    if p is None:
        return None
    if p.org_id:
        return p.org_id
    # A linked box's own projects have no org: the org layer is the cloud's decision it pulled.
    from app.services import rules_org
    return rules_org.pulled_org_id(db)


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


def test_scope_mode(db: Session, project_id: str | None) -> str:
    """The project's test-scope mode. A project that never set it — or a box whose pack does
    not carry the rule — reads `agent_and_pr`, today's behaviour."""
    if TEST_SCOPE_RULE not in LIBRARY:
        return TEST_SCOPE_DEFAULT
    mode = resolve(db, TEST_SCOPE_RULE, project_id)["params"]["value"].get("mode")
    return mode if mode in TEST_SCOPE_MODES else TEST_SCOPE_DEFAULT


def test_scope_engines(db: Session, project_id: str | None) -> list[str]:
    """The engines `agent_full` needs full passing local runs on (GRPH-1429). Unset, or a value
    that is not a non-empty list of strings, reads the default: postgres and sqlite."""
    if TEST_SCOPE_RULE not in LIBRARY:
        return list(TEST_SCOPE_ENGINES_DEFAULT)
    value = resolve(db, TEST_SCOPE_RULE, project_id)["params"]["value"].get("engines")
    if (isinstance(value, list) and value
            and all(isinstance(e, str) and e.strip() for e in value)):
        return [e.strip().lower() for e in value]
    return list(TEST_SCOPE_ENGINES_DEFAULT)


def undeclared_files_mode(db: Session, project_id: str | None) -> str:
    """`hold` (today's behaviour) or `warn`. A project that never set it, or a box whose pack
    does not carry the rule, reads `hold`."""
    if UNDECLARED_FILES_RULE not in LIBRARY:
        return UNDECLARED_FILES_DEFAULT
    mode = resolve(db, UNDECLARED_FILES_RULE, project_id)["params"]["value"].get("mode")
    return mode if mode in UNDECLARED_FILES_MODES else UNDECLARED_FILES_DEFAULT


def exclusive_paths(db: Session, project_id: str | None) -> tuple[str, ...]:
    """The project's exclusive file areas. A project that never set the rule — or a box whose
    pack does not carry it — reads the default, today's behaviour."""
    if EXCLUSIVE_AREAS_RULE not in LIBRARY:
        return EXCLUSIVE_AREAS_DEFAULT
    paths = resolve(db, EXCLUSIVE_AREAS_RULE, project_id)["params"]["value"].get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return EXCLUSIVE_AREAS_DEFAULT
    return tuple(paths)


def generated_files(db: Session, project_id: str | None) -> list[str]:
    """The project's generated-file paths; today's list when unset or the rule is absent."""
    if GENERATED_FILES_RULE not in LIBRARY:
        return list(GENERATED_FILES_DEFAULT)
    paths = resolve(db, GENERATED_FILES_RULE, project_id)["params"]["value"].get("paths")
    return list(paths) if isinstance(paths, list) else list(GENERATED_FILES_DEFAULT)


def done_predicate_list(db: Session, project_id: str | None) -> list[dict]:
    """Each predicate `done` needs for a project with where it came from: `deployment` (the
    environment setting, read live, and `suite_green` for an item with a linked PR) or
    `project` (added through `merge.done_predicates`). Unset follows the deployment."""
    out = [{"name": n, "source": "deployment"} for n in deployment_predicates()]
    out.append({"name": CI_FLOOR_PREDICATE, "source": "deployment", "when": "item has a linked PR"}
               if CI_FLOOR_PREDICATE not in deployment_predicates() else None)
    out = [o for o in out if o]
    if DONE_PREDICATES_RULE in LIBRARY:
        state = resolve(db, DONE_PREDICATES_RULE, project_id)
        if state["on"]["value"]:
            have = {o["name"] for o in out}
            for n in (state["params"]["value"] or {}).get("predicates") or []:
                if n not in have:
                    out.append({"name": n, "source": "project"})
                    have.add(n)
    return out


def done_predicate_names(db: Session, project_id: str | None) -> list[str]:
    """What the done gate requires of every item: the deployment's list plus the project's
    additions. `suite_green` for a linked PR is the cooldown's own gate, not repeated here."""
    return [p["name"] for p in done_predicate_list(db, project_id) if "when" not in p]


def effective(db: Session, project_id: str | None) -> list[dict]:
    """Every library rule's effective toggle for a project, in library order."""
    org_id = _org_of(db, project_id)
    rows = {(r.rule_id, "org" if r.org_id else "project"): r for r in db.scalars(
        select(RuleToggle).where(
            (RuleToggle.project_id == project_id) if not org_id else
            ((RuleToggle.project_id == project_id) | (RuleToggle.org_id == org_id))))
    } if project_id else {}
    out = [dict(_resolve(r, rows.get((r.id, "project")), rows.get((r.id, "org")), org_id),
                stage=r.stage, title=r.title)
           for r in LIBRARY.values()]
    for t in out:
        if t["rule_id"] == DONE_PREDICATES_RULE:  # GRPH-1426: the read names each source
            t["effective_predicates"] = done_predicate_list(db, project_id)
    return out


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


def deployment_predicates() -> list[str]:
    from app.config import settings
    return list(settings.required_predicate_list)


def _check_done_predicates(value: object) -> list[str]:
    if not all(isinstance(p, str) and p.strip() for p in value):
        raise Validation(f"{DONE_PREDICATES_RULE} param 'predicates' must be a list of names")
    names = list(dict.fromkeys(p.strip() for p in value))
    if names:  # an empty list is "unset": the project follows the deployment
        for name, source in [(n, "deployment `required_predicates`") for n in
                             deployment_predicates()] + [
                (CI_FLOOR_PREDICATE, "the CI floor for items with a linked PR")]:
            if name not in names:
                raise Validation(
                    f"{DONE_PREDICATES_RULE} cannot drop {name!r}: it is required by {source}",
                    hint="a project may only add predicates to the floor")
    return names


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
        if rule.id == TEST_SCOPE_RULE and k == "engines":
            if (not isinstance(v, list) or not v
                    or not all(isinstance(e, str) and e.strip() for e in v)):
                raise Validation(f"{rule.id} param 'engines' must be a non-empty list of "
                                 "engine names")
            params = {**params, k: list(dict.fromkeys(e.strip().lower() for e in v))}
            continue
        want = type(rule.default_params[k])
        if not isinstance(v, want) or (want is int and isinstance(v, bool)):
            raise Validation(f"{rule.id} param {k!r} must be {want.__name__}")
        if want is list and not all(isinstance(x, str) for x in v):
            raise Validation(f"{rule.id} param {k!r} must be a list of strings")
        if rule.id == DONE_PREDICATES_RULE and k == "predicates":
            params = {**params, k: _check_done_predicates(v)}
        choices = PARAM_CHOICES.get(rule.id, {}).get(k)
        if choices is not None and v not in choices:
            raise Validation(f"{rule.id} param {k!r} must be one of {', '.join(choices)}",
                             hint=f"got {v!r}")
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
    if before["on"]["value"] and not after["on"]["value"]:
        _switched_off(db, rule_id, [project_id], user)
    return after


def set_org_toggle(db: Session, org_id: str, rule_id: str, changes: dict, *, actor: object,
                   reason: str | None, dry_run: bool = False) -> dict:
    """The org's value for a rule, and its lock. Applies to every project that inherits it —
    every project at all, when locked — recorded as ONE event naming the projects it changed.

    `dry_run` runs the same checks and the same resolution, then rolls the change back before
    anything is committed: no RuleToggle row, no event, no published-version stamp. The reply
    is what the write would return, so a preview and the write cannot disagree."""
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
    if dry_run:
        try:
            db.flush()  # resolve() below must see the change; the rollback takes it back
            changed = [pid for pid in project_ids
                       if _value(resolve(db, rule_id, pid)) != before[pid]]
            return {"rule_id": rule_id, "org_id": org_id, **_snapshot(row),
                    "projects_changed": changed}
        finally:
            db.rollback()
    db.commit()
    changed = [pid for pid in project_ids if _value(resolve(db, rule_id, pid)) != before[pid]]
    events_svc.record_user(
        db, user, action="set_rule_toggle", target_type="rule", target_id=rule_id,
        meta={"scope": "org", "org_id": org_id, "reason": why, "old": old,
              "new": _snapshot(row), "projects_changed": changed})
    _switched_off(db, rule_id, [pid for pid in changed if before[pid]["on"]
                                and not resolve(db, rule_id, pid)["on"]["value"]], user)
    from app.services import rules_org  # rules_org imports this module → keep lazy
    rules_org.note_published(db, org_id)  # GRPH-1297: linked boxes measure "overdue" from here
    return {"rule_id": rule_id, "org_id": org_id, **_snapshot(row), "projects_changed": changed}


def org_view(db: Session, org_id: str) -> dict:
    """The org's own state per library rule, and each of its projects' effective value with
    its source — one read, instead of one `effective` call per project. The org's `on` /
    `params` are its own row's, or the platform default when it holds none."""
    project_ids = list(db.scalars(select(Project.id).where(Project.org_id == org_id)
                                  .order_by(Project.id)))
    rows = {r.rule_id: r for r in db.scalars(select(RuleToggle).where(RuleToggle.org_id == org_id))}
    per_project = {pid: {t["rule_id"]: t for t in effective(db, pid)} for pid in project_ids}
    out = []
    for rule in LIBRARY.values():
        row = rows.get(rule.id)
        own_on = row is not None and row.on is not None
        own_params = row is not None and row.params is not None
        out.append({
            "rule_id": rule.id, "stage": rule.stage, "title": rule.title,
            "on": {"value": row.on if own_on else rule.default_on,
                   "source": "org" if own_on else "platform default"},
            "params": {"value": copy.deepcopy(row.params) if own_params
                       else copy.deepcopy(rule.default_params),
                       "source": "org" if own_params else "platform default"},
            "locked": bool(row is not None and row.locked),
            "projects": [{"project_id": pid, "on": per_project[pid][rule.id]["on"],
                          "params": per_project[pid][rule.id]["params"]}
                         for pid in project_ids]})
    return {"org_id": org_id, "library_loaded": library_loaded(), "rules": out}


def _switched_off(db: Session, rule_id: str, project_ids: list[str], user: User) -> None:
    """A person switching a rule off is the override of every refusal it is still holding in
    those projects (GRPH-1283 / D9 cost); recorded, never raised into the write."""
    if project_ids:
        from app.services import observations as observations_svc
        observations_svc.on_rule_switched_off(db, rule_id, project_ids, user.id)
