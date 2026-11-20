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
"""
from __future__ import annotations

import copy
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound, Validation
from app.models import CustomRule, User
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
    if project_id is None:
        return None
    return db.scalar(select(CustomRule).where(CustomRule.rule_id == rule_id,
                                              CustomRule.project_id == project_id))


def get(db: Session, rule_id: str, project_id: str | None) -> CustomRule:
    row = _row(db, rule_id, project_id)
    if row is None:
        raise NotFound(f"{rule_id!r} is not a custom rule of this project")
    return row


def resolve_row(row: CustomRule) -> dict:
    """`rule_toggles.resolve`'s shape for a custom rule. The layer is the rule's own, so `on`
    and `params` both carry `source: custom`; an UNMEASURED rule resolves off."""
    lib = library_rule(row)
    on = lib.default_on
    return {"rule_id": row.rule_id,
            "on": {"value": on, "source": SOURCE},
            "params": {"value": copy.deepcopy(row.params or {}), "source": SOURCE},
            "inherits": {"on": SOURCE, "params": SOURCE},
            "inherited": {"on": on, "params": copy.deepcopy(row.params or {})},
            "locked": False, "locked_by": None,
            "state": row.state, "unmeasured_reason": lib.unmeasured_reason or None}


def resolve(db: Session, rule_id: str, project_id: str | None) -> dict:
    return resolve_row(get(db, rule_id, project_id))


def rows_for_project(db: Session, project_id: str | None) -> list[CustomRule]:
    if project_id is None:
        return []
    return list(db.scalars(select(CustomRule).where(CustomRule.project_id == project_id)
                           .order_by(CustomRule.id)))


def effective(db: Session, project_id: str | None) -> list[dict]:
    return [dict(resolve_row(r), stage=r.stage.lower(), title=r.title)
            for r in rows_for_project(db, project_id)]


def _author(db: Session, row: CustomRule) -> str | None:
    u = db.get(User, row.author_id) if row.author_id else None
    return (u.handle or u.name) if u else None


def view(db: Session, row: CustomRule) -> dict:
    r = resolve_row(row)
    return {"rule_id": row.rule_id, "primitive": row.primitive, "stage": row.stage,
            "title": row.title, "why": row.why, "claims": row.claims, "params": row.params,
            "state": row.state, "author": _author(db, row), "reason": row.reason,
            "created_at": row.created_at, "on": r["on"]["value"], "source": SOURCE,
            "unmeasured_reason": r["unmeasured_reason"]}


def list_for_project(db: Session, project_id: str) -> list[dict]:
    return [view(db, r) for r in rows_for_project(db, project_id)]


def create(db: Session, actor: object, project_id: str, *, slug: str, primitive: str,
           title: str, why: str, reason: str, params: dict | None = None,
           claims: str | None = None) -> CustomRule:
    """Save a custom rule, `active` at once. A person's write, validated by `validate_draft`."""
    user = T._check_project_writer(db, actor, project_id)
    reason = T._reason(reason)
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
    if _row(db, rule_id, project_id) is not None:
        raise Validation(f"{rule_id} already exists in this project",
                         hint="pick another slug")
    row = CustomRule(rule_id=rule_id, project_id=project_id, primitive=primitive,
                     primitive_api=rule_pack.PRIMITIVE_API, stage=prim.stage,
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
