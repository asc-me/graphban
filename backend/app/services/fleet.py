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

from app.models import Agent, AreaReservation, Item
from app.services import keys as keys_svc
from app.services.items import DEFAULT_LEASE_SECONDS

# The three roles, in the order a fleet fills them. `worker` is the default because a lone
# agent that registered without a hint should be able to do work rather than wait to be told.
ROLES = ("planner", "worker", "reviewer")
DEFAULT_ROLE = "worker"

# States an agent can be in. `offline` is DERIVED (see `presence_state`) and never written as
# a transition — an agent that dies does not get to tell us. `quarantined` is the opposite: it
# IS stored, because it describes an agent that is demonstrably alive and still being refused.
STATES = ("idle", "working", "reviewing", "offline", "quarantined")

# Neither may be set by the agent itself. `offline` is a contradiction from something that is
# calling us; `quarantined` is a verdict about the caller, and a caller does not get to
# withdraw it by asserting a different state.
_NOT_SELF_ASSERTABLE = ("offline", "quarantined")


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
    # Checked BEFORE the clock. A quarantined agent may still be heartbeating — that is what
    # got it quarantined — so deriving from `last_seen_at` would report it healthy.
    if agent.state == "quarantined":
        return "quarantined"
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
    if state in STATES and state not in _NOT_SELF_ASSERTABLE and agent.state != "quarantined":
        # `offline` is derived, never asserted — an agent claiming to be offline while it is
        # calling us is a contradiction. And a QUARANTINED agent cannot heartbeat its way back
        # to `working`: the recovery path is to register again, which is a new row, so the
        # verdict stays attached to the process that earned it.
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


# ---- D2: the call gate ---------------------------------------------------------------------
#
# Which role a tool requires. Absent from this map = no role requirement, which is the correct
# default for reads and for the tools every role shares (`get_context`, `search_items`, …).
#
# **Enforced at CALL time, not by trimming the manifest** (PRD-17 D-b). `tools/list` is fetched
# once at client connect, BEFORE `register_agent` has run, and this endpoint returns single
# JSON with no SSE — so there is no channel to push `notifications/tools/list_changed` when a
# role is later assigned. A manifest can only fail to mention a tool; the gate refuses it.
TOOL_ROLES: dict[str, tuple[str, ...]] = {
    # The orchestrator plans; it does not quietly do the work.
    "claim_next": ("worker",),
    "next_cluster": ("worker",),
    "release_item": ("worker",),
    "heartbeat": ("worker",),
    # PRD authorship is the planner's.
    "create_prd": ("planner",),
    "update_prd": ("planner",),
    "decompose_prd": ("planner",),
    "grill_prd": ("planner",),
}

# `update_item` is special: the tool is a worker's, but ONE argument on it is not. A worker
# moves work as far as `review` and no further — `done` is the reviewer's word, and letting a
# worker write it would make the self-review ban decorative while leaving every test green.
WORKER_STATUS_CEILING = "review"
_BEYOND_WORKER = ("done",)


def role_for_call(db: Session, *, api_key, agent_id: str | None) -> tuple[str, str | None]:
    """The role this call carries, and the agent it belongs to (or None).

    Falls back to the KEY's ceiling when no registered agent is named. That keeps every
    existing single-agent setup working — a key eligible for all three roles is refused
    nothing, which is exactly the pre-PRD-17 behaviour — while still binding a RESTRICTED key,
    so `roles: ["worker"]` cannot be escaped by simply never calling `register_agent`.
    """
    if agent_id:
        agent = db.get(Agent, agent_id)
        if agent is not None:
            return agent.active_role, agent.id
    allowed = eligible_roles(api_key)
    # A key that permits everything carries no restriction; one pinned to a single role
    # carries that role.
    return (allowed[0] if len(allowed) == 1 else "*"), None


def check_tool_role(db: Session, *, tool: str, api_key, agent_id: str | None,
                    args: dict | None = None) -> None:
    """Raise `authz.Forbidden` when this caller's role may not make this call.

    Deliberately raises the EXISTING error rather than a new class: the dispatcher already
    maps `Forbidden` to a JSON-RPC tool error with the stable `unauthorized` code, so an agent
    that already handles refusals needs no new branch. The `hint` is the machine-readable
    next step (AL-47), so a refused agent can act without parsing prose.
    """
    from app.security import authz

    role, resolved = role_for_call(db, api_key=api_key, agent_id=agent_id)
    if role == "*":
        return
    who = resolved or f"key {getattr(api_key, 'name', '') or getattr(api_key, 'id', '?')}"

    required = TOOL_ROLES.get(tool)
    if required and role not in required:
        raise authz.Forbidden(
            f"{tool} requires role {' or '.join(repr(r) for r in required)}; "
            f"{who} is registered as {role!r}",
            hint=_hint_for(tool, role),
        )

    # The argument-level ceiling. A worker may call `update_item`; it may not write `done`.
    if tool == "update_item" and role == "worker":
        status = (args or {}).get("status")
        if status in _BEYOND_WORKER:
            raise authz.Forbidden(
                f"update_item(status={status!r}) requires role 'reviewer'; "
                f"{who} is registered as 'worker'",
                hint="move it to 'review'; a reviewer takes it from there",
            )


def _hint_for(tool: str, role: str) -> str:
    if role == "worker":
        return "your work moves to review; a reviewer takes it from there"
    if role == "planner":
        return "planners allocate rather than claim; use propose_allocation"
    if role == "reviewer":
        return "reviewers take work through claim_review, not claim_next"
    return "call fleet_status to see the roles this project has available"


# How many refusals in a row before an agent is quarantined. A drifting agent that holds a
# cluster while producing nothing is strictly worse than no agent — it blocks the divvy. Not a
# setting: pick one number and let somebody hit it.
QUARANTINE_AFTER_REFUSALS = 3


def record_refusal(db: Session, *, agent_id: str | None) -> int:
    """Count a refusal against an agent and return the running total.

    Counted on the AGENT, not the key: a key may carry several terminals, and quarantining
    all of them because one drifted would take down the healthy ones with it.
    """
    if not agent_id:
        return 0
    agent = db.get(Agent, agent_id)
    if agent is None:
        return 0
    caps = dict(agent.capabilities or {})
    count = int(caps.get("refusals", 0)) + 1
    caps["refusals"] = count
    agent.capabilities = caps
    db.commit()
    return count


def clear_refusals(db: Session, agent_id: str | None) -> None:
    """A successful call means the agent is complying again. Consecutive is the property that
    matters — three refusals spread across a productive hour is a client with one stale code
    path, not an agent that has stopped listening."""
    if not agent_id:
        return
    agent = db.get(Agent, agent_id)
    if agent is None or not (agent.capabilities or {}).get("refusals"):
        return
    caps = dict(agent.capabilities or {})
    caps.pop("refusals", None)
    agent.capabilities = caps
    db.commit()


def quarantine(db: Session, agent_id: str) -> dict:
    """Stop an agent that has stopped listening: release its work and take it off the fleet.

    Called after `QUARANTINE_AFTER_REFUSALS` consecutive refusals — an agent that keeps
    calling its old role's tools after a directive is a drifting agent, and one holding a
    cluster while producing nothing is strictly worse than no agent at all, because it blocks
    the divvy for everyone else.

    **Only ever reached by an agent that is demonstrably alive.** Refusal and network silence
    are indistinguishable to a server, so silence is left entirely to the presence clock; this
    path requires the agent to be actively calling tools and being told no.

    Recorded as `quarantined` rather than backdating `last_seen_at` into the past. Backdating
    would make the roster claim we had not seen an agent that had just called us — falsifying
    the one field the server actually knows to be true.
    """
    agent = db.get(Agent, agent_id)
    if agent is None:
        return {"quarantined": False}

    released = []
    for it in db.scalars(select(Item).where(Item.claimed_by == agent.id)).all():
        it.claimed_by = None
        it.claimed_at = None
        it.assignee = ""
        if it.status == "in_progress":
            it.status = "next"
        released.append(it.id)
    reservations = db.scalars(
        select(AreaReservation).where(AreaReservation.agent_id == agent.id)).all()
    for row in reservations:
        db.delete(row)

    agent.state = "quarantined"
    # A branch left behind is state only a human can resolve — the fleet can release the ITEM
    # but it cannot merge or discard someone's edits.
    if agent.branch and released:
        agent.branch_orphaned = True
    db.commit()
    return {"quarantined": True, "released_items": released,
            "released_reservations": len(reservations),
            "branch_orphaned": agent.branch_orphaned}
