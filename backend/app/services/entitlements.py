"""The org entitlement registry: what AI capacity an org holds, and who can run it (PRD-62 S2).

One service for REST (`routers/orgs.py`), MCP (`list_entitlements` / `set_entitlement`) and
runner declaration (`register_agent`'s `entitlements`). Headroom state (D4) is S3's and is not
derived here; a row in this registry says the entitlement EXISTS, never that it has capacity.

**Credentials never move; work does** (D1). Nothing here accepts a secret:

- `subscription` and `local` rows have no secret field (D2). The API refuses `secret_ref` on
  them, and the `entitlements` CHECK constraint makes the column NULL for them regardless.
- `api_key` and `cloud` rows carry `secret_ref`, a HANDLE the runner resolves — an env var or
  a secrets-manager name — never the key (D3). A value shaped like a raw key is refused.
- Any field this registry does not know is refused rather than ignored, and one named like a
  credential (`password`, `api_key`, `oauth_token`, ...) is refused naming D2. Ignoring it would
  answer a request to store a secret with a success.

Plan-gated to `team` / `enterprise` (D8) by `quotas.require_capacity_routing`.
"""
from __future__ import annotations

import re
from secrets import token_urlsafe

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound, Validation
from app.models import Agent, Entitlement, OrgMembership, Project, utcnow
from app.services import quotas

KINDS: tuple[str, ...] = ("subscription", "api_key", "cloud", "local")
#: The only kinds that may carry a `secret_ref` (D3). Mirrored as a literal in the
#: `entitlements` CHECK constraint, which cannot import this; a test asserts they agree.
SECRET_KINDS: tuple[str, ...] = ("api_key", "cloud")
#: The writable fields. Anything else in a write is refused, never dropped.
FIELDS: tuple[str, ...] = (
    "vendor", "kind", "owner_user_id", "runner", "plan_label", "reset_cadence",
    "secret_ref", "token_budget",
)

#: Said wherever a runner's declaration is shown, so no reader takes it for a check (S2).
DECLARATION_TRUST = (
    "Self-declared by the runner when it registered; not verified. Graphban cannot see a "
    "vendor sign-in, so a runner can claim an entitlement it does not hold — that costs a "
    "failed spawn, not a credential."
)

_SECRETISH = re.compile(
    r"secret|password|passwd|token|api_?key|credential|oauth|session|cookie|login|bearer",
    re.IGNORECASE,
)
_HANDLE = re.compile(r"^[A-Za-z0-9_.:/@-]{1,200}$")
#: Prefixes of literal vendor keys. A `secret_ref` starting with one of these is the secret
#: pasted where its NAME belongs, which is exactly what D3 keeps off the server.
_RAW_KEY_PREFIXES = ("sk-", "sk_", "pk-", "rk_", "xox", "ghp_", "gho_", "github_pat_",
                     "AKIA", "ASIA", "AIza", "ya29.")


def org_for_project(db: Session, project_id: str | None) -> str:
    """The plan-gated org a project-scoped (MCP) call reaches the registry through."""
    org_id = quotas.org_id_for_project(db, project_id)
    quotas.require_capacity_routing(db, org_id)
    return org_id  # type: ignore[return-value]  # the gate refuses None


def _refuse_unknown(fields: dict) -> None:
    unknown = sorted(k for k in fields if k not in FIELDS)
    if not unknown:
        return
    secretish = [k for k in unknown if _SECRETISH.search(k)]
    if secretish:
        raise Validation(
            f"refused {', '.join(secretish)}: the entitlement registry never stores a "
            "credential. A subscription has no secret field; an api_key or cloud entitlement "
            "takes `secret_ref`, the NAME of the secret on its runner",
            hint="keep the secret on the runner (env, keychain, secrets manager) and pass "
                 "its name as secret_ref",
        )
    raise Validation(f"unknown entitlement field(s): {', '.join(unknown)}",
                     hint=f"writable fields: {', '.join(FIELDS)}")


def _str(fields: dict, name: str) -> str:
    value = fields[name]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Validation(f"{name} must be a string")
    return value.strip()


def _secret_ref(value) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise Validation("secret_ref must be a string naming the secret on the runner")
    value = value.strip()
    if value.startswith(_RAW_KEY_PREFIXES) or not _HANDLE.match(value):
        raise Validation(
            "secret_ref must NAME the secret (e.g. ANTHROPIC_API_KEY or a secrets-manager "
            "path), not hold it; this value looks like the key itself",
            hint="the key stays on the runner (PRD-62 D3)",
        )
    return value


def _apply(db: Session, row: Entitlement, fields: dict) -> None:
    """Write `fields` onto `row`, then validate the RESULT — a kind change is checked against
    the secret_ref it would be left holding, not only against what this call sent."""
    if "vendor" in fields:
        row.vendor = _str(fields, "vendor").lower()
    if "kind" in fields:
        row.kind = _str(fields, "kind")
    for name in ("runner", "plan_label", "reset_cadence"):
        if name in fields:
            setattr(row, name, _str(fields, name))
    if "owner_user_id" in fields:
        row.owner_user_id = _str(fields, "owner_user_id") or None
    if "secret_ref" in fields:
        row.secret_ref = _secret_ref(fields["secret_ref"])
    if "token_budget" in fields:
        budget = fields["token_budget"]
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)
                                   or budget < 0):
            raise Validation("token_budget must be a non-negative integer, or null for none "
                             "stated")
        row.token_budget = budget

    if not row.vendor:
        raise Validation("vendor is required, e.g. anthropic")
    if row.kind not in KINDS:
        raise Validation(f"kind must be one of: {', '.join(KINDS)}")
    if row.secret_ref is not None and row.kind not in SECRET_KINDS:
        raise Validation(
            f"a {row.kind} entitlement has no secret field (PRD-62 D2); only "
            f"{' and '.join(SECRET_KINDS)} take a secret_ref",
            hint="send secret_ref: null with the kind change to clear it",
        )
    if row.kind == "subscription" and not row.owner_user_id:
        raise Validation("a subscription needs owner_user_id: it runs only on the runner of "
                         "the person signed in to it (PRD-62 D1)")
    if row.owner_user_id is not None and db.scalar(
        select(OrgMembership.id).where(OrgMembership.org_id == row.org_id,
                                       OrgMembership.user_id == row.owner_user_id)
    ) is None:
        raise Validation(f"owner_user_id {row.owner_user_id!r} holds no seat in this org")


def set_entitlement(db: Session, org_id: str, fields: dict, *, entitlement_id: str | None = None,
                    actor: str | None = None) -> dict:
    """Create an entitlement, or patch one by id. Omitted fields are unchanged; null clears.
    Commits and returns the row as `to_dict` renders it."""
    quotas.require_capacity_routing(db, org_id)
    _refuse_unknown(fields)
    if entitlement_id:
        row = db.get(Entitlement, entitlement_id)
        if row is None or row.org_id != org_id:
            raise NotFound(f"entitlement {entitlement_id!r} not found in this org")
    else:
        row = Entitlement(id=f"ent_{token_urlsafe(8)}", org_id=org_id, vendor="", kind="",
                          runner="", plan_label="", reset_cadence="", created_by=actor)
    _apply(db, row, fields)
    row.updated_at = utcnow()
    if not entitlement_id:
        db.add(row)
    db.commit()
    db.refresh(row)
    return to_dict(db, row)


def list_entitlements(db: Session, org_id: str) -> list[dict]:
    quotas.require_capacity_routing(db, org_id)
    rows = db.scalars(
        select(Entitlement).where(Entitlement.org_id == org_id)
        .order_by(Entitlement.vendor, Entitlement.kind, Entitlement.created_at)
    ).all()
    declared = _declarations(db, org_id)
    return [to_dict(db, r, declared=declared) for r in rows]


def _declarations(db: Session, org_id: str) -> dict[str, list[dict]]:
    """entitlement id -> the agents in this org that declared it, newest first."""
    from app.services import fleet as fleet_svc  # fleet imports widely; keep this lazy

    agents = db.scalars(
        select(Agent).join(Project, Project.id == Agent.project_id)
        .where(Project.org_id == org_id, Agent.dismissed_at.is_(None))
        .order_by(Agent.registered_at.desc())
    ).all()
    out: dict[str, list[dict]] = {}
    for a in agents:
        for ent_id in a.entitlement_ids or []:
            out.setdefault(ent_id, []).append({
                "agent_id": a.id,
                "key": a.key,
                "host": (a.capabilities or {}).get("host") or "",
                "state": fleet_svc.presence_state(a),
                "verified": False,
            })
    return out


def to_dict(db: Session, row: Entitlement, *, declared: dict[str, list[dict]] | None = None) -> dict:
    if declared is None:
        declared = _declarations(db, row.org_id)
    return {
        "id": row.id,
        "org_id": row.org_id,
        "vendor": row.vendor,
        "kind": row.kind,
        "owner_user_id": row.owner_user_id,
        "runner": row.runner,
        "plan_label": row.plan_label,
        "reset_cadence": row.reset_cadence,
        # Present on every row, null on a subscription: the shape says there is no secret
        # field rather than leaving a reader to wonder whether it was withheld.
        "secret_ref": row.secret_ref,
        "token_budget": row.token_budget,
        "declared_by": declared.get(row.id, []),
        "declaration": DECLARATION_TRUST,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def validate_declaration(db: Session, project_id: str | None, ids) -> list[str]:
    """The entitlement ids a registering runner may declare: every one must exist in the
    project's org, and the org must be on a capacity-routing plan. Checked BEFORE the agent
    row is written, so a refused declaration mints no agent. Existence is all that is checked —
    whether the runner really holds it is unverifiable and said so (`DECLARATION_TRUST`)."""
    if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
        raise Validation("entitlements must be a list of entitlement ids")
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    org_id = org_for_project(db, project_id)
    known = set(db.scalars(
        select(Entitlement.id).where(Entitlement.org_id == org_id, Entitlement.id.in_(ids))
    ).all())
    missing = [i for i in ids if i not in known]
    if missing:
        raise Validation(f"no such entitlement in this org: {', '.join(missing)}",
                         hint="list_entitlements names what the org has registered")
    return ids
