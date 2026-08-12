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

from app.models import Agent
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
