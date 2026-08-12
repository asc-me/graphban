"""Agents as first-class, and the one clock that governs them (PRD-17 / GRPH-331).

Nothing counted agents before this. `agent_id` was a self-declared string defaulting to the
API key's name, so three terminals sharing a key were **one agent** to the server: nothing
could assign roles between them, nothing could stop one signing off its own work, and the
roster's basic question — who is out there — had no answer at all.

This module owns the derived vocabulary that the registry, the divvy and the Fleet view all
read. It deliberately holds no endpoints: GRPH-331 is the data model, and D1/D2/D3 build on
top of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, Item
from app.services import keys as keys_svc
from app.services.items import DEFAULT_LEASE_SECONDS

# The three roles, in the order a fleet fills them. `worker` is the default because a lone
# agent that registered without a hint should be able to do work rather than wait to be told.
ROLES = ("planner", "worker", "reviewer")
DEFAULT_ROLE = "worker"

# States an agent can be in. `offline` is DERIVED (see `presence_state`) and never written as
# a transition — an agent that dies does not get to tell us.
STATES = ("idle", "working", "reviewing", "offline")


def presence_ttl_seconds(lease_seconds: int = DEFAULT_LEASE_SECONDS) -> int:
    """How long since `last_seen_at` before an agent counts as offline.

    **Derived from the lease clock rather than a constant of its own**, and that is the whole
    design: one number governs item leases, reservation horizons, the bounce pin and presence
    together. A project that raises `lease_seconds` for long builds automatically gets a
    longer presence window — with an independent constant it would instead start declaring
    healthy workers dead mid-edit, on exactly the projects whose work takes longest.
    """
    return max(1, lease_seconds // 4)


def heartbeat_interval_seconds(lease_seconds: int = DEFAULT_LEASE_SECONDS) -> int:
    """How often an agent should call `heartbeat`: a third of the TTL.

    The 3× gap absorbs latency — an agent must miss three consecutive heartbeats before it is
    declared offline, so one slow network round trip never releases a working agent's items.
    Deliberately not per-agent configurable: a fleet whose members disagree about what "alive"
    means makes the roster's one job unanswerable.
    """
    return max(1, presence_ttl_seconds(lease_seconds) // 3)


def presence_state(agent: Agent, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                   now: datetime | None = None) -> str:
    """`idle|working|reviewing` as stored, or `offline` when presence has lapsed.

    Computed on read rather than swept, the same shape as `memory.age_state`. There is no
    sweep to forget to run, and no window in which the roster shows green for a process that
    stopped an hour ago.
    """
    now = now or datetime.now(timezone.utc)
    seen = agent.last_seen_at
    if seen is None:
        return "offline"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    if now - seen > timedelta(seconds=presence_ttl_seconds(lease_seconds)):
        return "offline"
    return agent.state if agent.state in STATES else "idle"


def eligible_roles(api_key) -> tuple[str, ...]:
    """The roles a credential permits — the CEILING, not the assignment.

    An empty or missing list means all three. That is the migration position for keys minted
    before PRD-17 (nothing in flight breaks), and it is safe only because this is a ceiling:
    a key still cannot grant a role that does not exist, and `assign_role` is what actually
    moves an agent. Read it as "unspecified", not as "none" — a key resolving to no roles at
    all would silently make every agent on it unable to work, which is an absence behaving
    like a decision.
    """
    roles = getattr(api_key, "roles", None) or []
    allowed = tuple(r for r in roles if r in ROLES)
    return allowed or ROLES


# ---- D1: the registry ---------------------------------------------------------------------

def register_agent(db: Session, *, project_id: str, api_key, label: str = "",
                   capabilities: dict | None = None, worktree: str = "",
                   branch: str = "", role_hint: str | None = None) -> Agent:
    """Register a connected process as an agent, and tell it what role it holds.

    **Always creates a row. Never reuses one by label.** Two identical Claude Code windows on
    one machine is a legitimate fleet shape, and matching an existing agent by label would
    merge two live terminals into one — the precise condition that made the old self-declared
    `agent_id` useless. The per-project sequence gives simultaneous registrations distinct ids
    by construction, so two windows racing on one key cannot collide.

    `role_hint` is a request, not an instruction: it is honoured when the credential permits
    that role and silently clamped to the default otherwise. Silently, because the hint comes
    from a client config file — refusing the registration would strand an agent over a
    preference, and the authoritative answer is returned in `active_role` either way.
    """
    stored_id, number = keys_svc.mint(db, project_id, "agent")
    allowed = eligible_roles(api_key)
    role = role_hint if role_hint in allowed else (DEFAULT_ROLE if DEFAULT_ROLE in allowed
                                                   else allowed[0])
    now = datetime.now(timezone.utc)
    agent = Agent(
        id=stored_id, number=number, project_id=project_id,
        api_key_id=getattr(api_key, "id", None),
        label=label or "", capabilities=capabilities or {},
        worktree=worktree or "", branch=branch or "",
        active_role=role, role_assigned_at=now, role_acked_at=now,
        state="idle", registered_at=now, last_seen_at=now,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def touch(db: Session, agent_id: str, *, state: str | None = None) -> Agent | None:
    """Extend an agent's presence. Returns None when no such agent — never creates one.

    Called by `heartbeat` alongside the item lease, so one call keeps both alive. Creating on
    miss would resurrect an id the roster had already aged out and quietly give it a second
    identity.
    """
    agent = db.get(Agent, agent_id)
    if agent is None:
        return None
    agent.last_seen_at = datetime.now(timezone.utc)
    if state in STATES and state != "offline":
        # `offline` is derived, never asserted — an agent claiming to be offline while it is
        # calling us is a contradiction, and storing it would survive the next heartbeat.
        agent.state = state
    db.commit()
    db.refresh(agent)
    return agent


def list_agents(db: Session, project_id: str | None = None, *,
                lease_seconds: int = DEFAULT_LEASE_SECONDS,
                now: datetime | None = None) -> list[dict]:
    """The roster: every agent with its DERIVED presence and what it is holding.

    Offline agents are listed rather than hidden. An agent that died holding a branch is
    exactly what a human needs to see, and dropping it from the roster would answer "who is
    out there" with a tidier lie.
    """
    stmt = select(Agent)
    if project_id:
        stmt = stmt.where(Agent.project_id == project_id)
    agents = list(db.scalars(stmt.order_by(Agent.project_id, Agent.number)).all())
    held: dict[str, list[Item]] = {}
    if agents:
        ids = [a.id for a in agents]
        for it in db.scalars(select(Item).where(Item.claimed_by.in_(ids))).all():
            held.setdefault(it.claimed_by, []).append(it)
    out = []
    for a in agents:
        out.append({
            "id": a.id,      # frozen, internal — what `claimed_by` and `reviewed_by` store
            "key": a.key,    # rendered from the project's CURRENT tag (PRD-13)
            "label": a.label,
            "active_role": a.active_role,
            "state": presence_state(a, lease_seconds=lease_seconds, now=now),
            "capabilities": a.capabilities or {},
            "worktree": a.worktree,
            "branch": a.branch,
            "branch_orphaned": a.branch_orphaned,
            # ISO string, not a datetime: this dict crosses the MCP boundary as JSON, and a
            # raw datetime raises there rather than at the call site that built it.
            "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
            # What it is holding right now. The roster's second question after "who is out
            # there" is "what is stuck with them".
            # `id` is the RENDERED key, matching `_item_dict` and every other item the MCP
            # surface emits (PRD-13). The stored id is frozen and internal; emitting it here
            # would hand an agent a string it cannot quote back, and would leak a retired tag
            # into agent memory after a rename. `stored_id` is kept for the web UI, which
            # addresses rows directly.
            "holdings": [{"id": i.key, "stored_id": i.id, "title": i.title, "status": i.status}
                         for i in held.get(a.id, [])],
        })
    return out


def fleet_status(db: Session, project_id: str | None = None, *,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    """The roster plus the clock every member must obey.

    The intervals travel WITH the roster on purpose: an agent that does not know the
    heartbeat cadence cannot stay alive, and making it read a constant out of documentation
    is how a fleet ends up with members that disagree about what alive means.
    """
    agents = list_agents(db, project_id, lease_seconds=lease_seconds)
    return {
        "agents": agents,
        "online": sum(1 for a in agents if a["state"] != "offline"),
        "total": len(agents),
        "roles": list(ROLES),
        "presence_ttl_seconds": presence_ttl_seconds(lease_seconds),
        "heartbeat_interval_seconds": heartbeat_interval_seconds(lease_seconds),
    }
