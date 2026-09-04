"""The audit ledger (AL-43): one owner for recording and reading mutation events.

Written at the boundaries — the MCP dispatcher for agent (API-key) actions and
REST routers for user actions — so every accepted mutation captures who did it.
Recording never raises into the caller: an audit failure must not fail the
operation it audits (best-effort, logged).
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApiKey, Event, User

logger = logging.getLogger("graphban.events")


def record(
    db: Session,
    *,
    actor_type: str,
    actor_id: str = "",
    actor_label: str = "",
    surface: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    project_id: str | None = None,
    meta: dict | None = None,
) -> None:
    try:
        db.add(Event(
            actor_type=actor_type, actor_id=actor_id, actor_label=actor_label,
            surface=surface, action=action, target_type=target_type,
            target_id=target_id, project_id=project_id, meta=meta,
        ))
        db.commit()
    except Exception:  # noqa: BLE001 — auditing must never break the audited op
        logger.exception("failed to record event %r", action)
        db.rollback()


def record_key(db: Session, key: ApiKey, *, action: str, target_type: str = "",
               target_id: str = "", project_id: str | None = None, meta: dict | None = None) -> None:
    """Record an agent action, attributed to the API key that performed it — and to the
    HUMAN principal that owns the key, so the audit shows who was behind the agent (AL-197)."""
    owner = db.get(User, key.user_id) if key.user_id else None
    m = dict(meta or {})
    if owner is not None:
        m["principal"] = {"id": owner.id, "label": owner.handle or owner.name}
    record(
        db, actor_type="apikey", actor_id=key.id, actor_label=key.name or key.id,
        surface="mcp", action=action, target_type=target_type, target_id=target_id,
        project_id=project_id, meta=m or None,
    )


def record_user(db: Session, user: User, *, action: str, target_type: str = "",
                target_id: str = "", project_id: str | None = None, meta: dict | None = None) -> None:
    """Record a user action from a REST route, attributed to the logged-in user."""
    record(
        db, actor_type="user", actor_id=user.id, actor_label=user.handle or user.name,
        surface="rest", action=action, target_type=target_type, target_id=target_id,
        project_id=project_id, meta=meta,
    )


def list_events(db: Session, *, project_ids: list[str], limit: int = 50, offset: int = 0,
                action: str | None = None) -> dict:
    """Most-recent-first events across the projects the caller may read. Includes
    project-less events (e.g. global memory) only implicitly via NULL — callers
    pass their readable project set."""
    stmt = select(Event).where(Event.project_id.in_(project_ids))
    if action:
        stmt = stmt.where(Event.action == action)
    stmt = stmt.order_by(Event.id.desc())
    total = len(db.scalars(select(Event.id).where(Event.project_id.in_(project_ids))).all())
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return {
        "results": [_event_dict(e) for e in rows],
        "total": total, "limit": limit, "offset": offset,
        "has_more": offset + limit < total,
    }


def _principal_and_agent(e: Event) -> tuple[str, str]:
    """Normalize an event to (human principal, agent): the person on whose behalf it ran,
    and the agent that performed it — empty when none. AL-197.

    - API-key action: the key IS the agent; the human is its owner (meta.principal).
    - assistant action: the human is the actor; the agent is meta.origin (assistant:<provider>).
    - plain user action: the human is the actor; no agent.
    """
    meta = e.meta or {}
    if e.actor_type == "apikey":
        principal = (meta.get("principal") or {}).get("label") or ""
        # The AGENT behind the key when the dispatcher could name it (PRD-34 D3); several
        # agents share one credential by design, and the key label alone cannot say which.
        agent = str(meta.get("agent_id") or "") or (e.actor_label or e.actor_id)
        return principal, agent
    origin = str(meta.get("origin") or "")
    if origin.startswith("assistant:"):
        return (e.actor_label or e.actor_id), origin
    return (e.actor_label or e.actor_id), ""


def _event_dict(e: Event) -> dict:
    principal, agent = _principal_and_agent(e)
    return {
        "id": e.id,
        "ts": e.ts.isoformat() if e.ts else None,
        "actor_type": e.actor_type,
        "actor_id": e.actor_id,
        "actor_label": e.actor_label,
        "principal": principal,  # the human behind the action (AL-197)
        "agent": agent,          # the agent that performed it, if any
        "surface": e.surface,
        "action": e.action,
        "target_type": e.target_type,
        "target_id": e.target_id,
        "project_id": e.project_id,
        "meta": e.meta,
    }


# The complete set of actions that can be taken FROM the operator plane. It's an
# allowlist, not a "not project-scoped" filter: a project-less tenant event (a global
# memory write, say) is still tenant activity and must not surface cross-tenant.
PLATFORM_ACTIONS = (
    "create_platform_invite",
    "revoke_platform_invite",
    "decide_org_request",
    "set_org_plan",
)


def platform_ledger(db: Session, *, limit: int = 12) -> list[Event]:
    """Most-recent-first operator-plane actions, across every tenant.

    This is the operator's own ledger, not a platform activity feed. Nothing a tenant
    does appears here, so an empty result means "no operator has done anything", never
    "the platform is quiet" — the caller renders those as different sentences.
    """
    return list(
        db.scalars(
            select(Event)
            .where(Event.action.in_(PLATFORM_ACTIONS))
            .order_by(Event.id.desc())
            .limit(limit)
        )
    )
