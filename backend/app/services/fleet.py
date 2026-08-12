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

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, AreaReservation, Item
from app.services import items as items_svc
from app.services import keys as keys_svc
from app.services.items import DEFAULT_LEASE_SECONDS

# The three roles, in the order a fleet fills them. `worker` is the default because a lone
# agent that registered without a hint should be able to do work rather than wait to be told.
ROLES = ("planner", "worker", "reviewer")
DEFAULT_ROLE = "worker"

# The fourth posture, and NOT a role: an agent doing everything, because nobody else is here.
#
# Graphban has two deployments and they are both first-class. The DEFAULT is a single dev with
# one agent — this session's shape — where the human is the reviewer and no server-side gate
# applies. The POWER-USER posture is a fleet, where roles are specialised and the server
# arbitrates between them. One substrate, two ways to hold it.
#
# Before this existed, registering made you strictly WORSE off: an all-in-one agent that called
# `register_agent` was labelled `worker` by default and the D2 ceiling then refused it
# `status: done` — so the correct move for a solo agent was to skip registration, which is
# also the move that hides it from the roster. The incentive pointed exactly the wrong way.
ALL_IN_ONE = "all-in-one"

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
                   branch: str = "", role_hint: str | None = None,
                   parent_agent_id: str | None = None) -> Agent:
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
    if role_hint in allowed:
        role = role_hint
    elif set(allowed) >= set(ROLES):
        # An unnarrowed credential and no preference stated: this is the single-dev posture,
        # so say so rather than guessing `worker`. Registering must never cost an agent
        # capability it already had — that is what made skipping registration rational.
        role = ALL_IN_ONE
    else:
        role = DEFAULT_ROLE if DEFAULT_ROLE in allowed else allowed[0]
    now = datetime.now(timezone.utc)
    agent = Agent(
        id=stored_id, number=number, project_id=project_id,
        api_key_id=getattr(api_key, "id", None),
        label=label or "", capabilities=capabilities or {},
        parent_agent_id=parent_agent_id or None,
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
    # A revoked credential means the agent cannot act, whatever its last heartbeat said. Read
    # as offline IMMEDIATELY rather than waiting out the TTL — and derived, not written, so
    # "End wave" never has to backdate `last_seen_at` into a time we know to be false.
    from app.models import ApiKey

    dead_keys = {k.id for k in db.scalars(
        select(ApiKey).where(ApiKey.revoked.is_(True))).all()}
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
            "state": ("offline" if a.api_key_id in dead_keys
                      else presence_state(a, lease_seconds=lease_seconds, now=now)),
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
    live = [a for a in agents if a["state"] != "offline"]
    # Counted BY ROLE, not just totalled. "4 agents online" is the same number whether it is a
    # balanced fleet or four workers with nobody to review them, and those need opposite
    # actions — the second is a review queue about to back up. `all-in-one` is reported beside
    # the roles rather than folded into one of them, because an unspecialised agent is the
    # DEFAULT posture and showing it as a worker would misdescribe the commonest deployment.
    by_role = {r: sum(1 for a in live if a["active_role"] == r) for r in ROLES}
    by_role[ALL_IN_ONE] = sum(1 for a in live if a["active_role"] == ALL_IN_ONE)
    return {
        "agents": agents,
        "online": len(live),
        "total": len(agents),
        "by_role": by_role,
        "posture": "fleet" if any(by_role[r] for r in ROLES) else "single-agent",
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
    "claim_cluster": ("worker",),
    "release_item": ("worker",),
    "heartbeat": ("worker",),
    # PRD authorship is the planner's.
    # Review belongs to the reviewer, and `claim_next` to the worker — the two halves of the
    # ban. A reviewer that could claim fresh work would drift into being a worker holding
    # review authority, which is self-review with extra steps.
    "claim_review": ("reviewer",),
    "sign_off": ("reviewer",),
    "bounce": ("reviewer",),
    # Allocation is the planner's whole job. `propose_allocation` is a read and ungated so a
    # worker can see the shape of the fleet; committing it is not.
    "assign_role": ("planner",),
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
            # An all-in-one agent is unrestricted, exactly as it was before it registered.
            # Its ceiling is still the credential's, so a narrowed key cannot reach this.
            if agent.active_role == ALL_IN_ONE:
                return "*", agent.id
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


# ---- D3: review, sign-off, and the self-review ban -----------------------------------------

class SelfReview(Exception):
    """An agent tried to pass its own work. Raised rather than returned, because every call
    site must handle it — a caller that ignores a `False` return would sign the item off."""


def independent(reviewer: Agent, author: Agent | None) -> bool:
    """Whether these two are separate enough that one reviewing the other means anything.

    **The self-review ban was keyed on agent id alone, and a call tree walks straight through
    it** (GRPH-361). `register_agent` mints a row per call — correctly, since "two terminals on
    one key are two agents" is the bug D1 exists to fix — so a verifier subagent that registers
    becomes a sibling with a distinct id and can sign off its parent's work. Reproduced: SA-A1
    built it, SA-A2 signed it.

    The tempting fix was to collapse identity on `(api_key_id, host)` so parent and child
    resolve to one agent. **That undoes D1**: two legitimate terminals would collapse too, and
    the server would again be unable to arbitrate between them. Identity is not the lever.

    So independence is a separate question from identity, asked only where it matters:

    - **A declared parent, either direction.** Cheap and honest, and it covers a subagent that
      reports a different host.
    - **Same credential AND same host.** The undeclared case, which is the common one — a
      subagent inherits its parent's key and runs in its process. It also catches something
      the original ban missed entirely: two windows of one model on one machine sharing one
      key are two agents by D1's definition and are not two opinions.

    Same key on DIFFERENT hosts stays independent — those are genuinely separate machines, and
    refusing there would block a legitimate fleet for no gain. The Fleet view already mints a
    credential per role, so the intended path is unaffected; a hand-rolled one-key fleet is
    told what to change rather than silently accepted.
    """
    if author is None:
        return True                      # human-authored, or an author nothing recorded
    if reviewer.id == author.id:
        return False                     # the original ban
    if reviewer.parent_agent_id == author.id or author.parent_agent_id == reviewer.id:
        return False
    if reviewer.parent_agent_id and reviewer.parent_agent_id == author.parent_agent_id:
        return False                     # siblings under one parent are one call tree
    same_key = bool(reviewer.api_key_id) and reviewer.api_key_id == author.api_key_id
    host_a = (reviewer.capabilities or {}).get("host")
    host_b = (author.capabilities or {}).get("host")
    same_host = host_a is not None and host_a == host_b
    return not (same_key and same_host)


NOT_INDEPENDENT = ("the only work in review was built by an agent sharing your credential and "
                   "host — mint a per-role credential in the Fleet view so review means "
                   "something")


def review_block_reason(db: Session, *, agent_id: str, project_id: str | None = None) -> str:
    """Why `claim_review` found nothing, when the answer is more useful than "nothing".

    Distinguishing "no work is waiting" from "work is waiting but you are not independent of
    it" is the difference between a reviewer waiting patiently and an operator learning to
    mint a second credential.
    """
    me = db.get(Agent, agent_id)
    stmt = select(Item).where(Item.status == "review")
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    for it in db.scalars(stmt).all():
        if it.claimed_by == agent_id:
            continue
        author = db.get(Agent, it.claimed_by) if it.claimed_by else None
        if me is not None and not independent(me, author):
            return NOT_INDEPENDENT
    return "no item awaiting a second pair of eyes"


def claim_review(db: Session, *, agent_id: str, project_id: str | None = None,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Item | None:
    """Lease an item awaiting review that this agent did NOT build.

    **`WHERE claimed_by != caller` is the entire invariant.** With one agent in the fleet this
    correctly returns nothing: self-review stops being a procedural discipline somebody
    remembers and becomes a clause the database enforces.

    Prefers a reviewer of a DIFFERENT VENDOR where the fleet has one. A Claude reviewer
    approving Claude work is a different agent but not a different error distribution — same
    training, same blind spots, same things it does not think to check. That upgrades the
    invariant from preventing self-review to preventing monoculture review, and it is the
    concrete payoff for running four heterogeneous windows rather than four identical ones.
    Preference, not requirement: a same-vendor review is far better than none.
    """
    me = db.get(Agent, agent_id)
    stmt = select(Item).where(Item.status == "review")
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    candidates = [
        it for it in db.scalars(stmt.order_by(Item.sort_order, Item.number)).all()
        # The ban, keyed on AUTHORSHIP rather than on role. The obvious attack on a dynamic
        # role system is to promote a worker to reviewer while it holds its own item; it does
        # not work, because an agent's id does not change when its role does.
        if it.claimed_by != agent_id
        # Already being reviewed by somebody else.
        and not (it.reviewed_by and it.reviewed_by != agent_id)
        # And separate enough for the review to mean anything (GRPH-361).
        and (me is None or independent(me, db.get(Agent, it.claimed_by) if it.claimed_by else None))
    ]
    if not candidates:
        return None

    my_vendor = ((me.capabilities or {}).get("vendor") if me else None)
    if my_vendor:
        authors = {a.id: (a.capabilities or {}).get("vendor")
                   for a in db.scalars(select(Agent)).all()}
        cross = [it for it in candidates if authors.get(it.claimed_by) != my_vendor]
        candidates = cross or candidates

    item = candidates[0]
    item.reviewed_by = agent_id
    db.commit()
    db.refresh(item)
    return item


# Below this effort, agent-distinct review is sufficient on its own (PRD-17 D9). An
# adversarial pass on a one-line fix is pure tax, and a gate that fires on trivia is a gate
# people route around — which is the AL-96 trust failure that kept GRPH-321 parked for months.
# The threshold is what answers that objection directly: the cheapest way to satisfy this gate
# is never to avoid it.
ADVERSARIAL_EFFORT_THRESHOLD = 3


class MissingAdversarialEvidence(Exception):
    """Above-threshold work signed off with nothing that tried to break it."""


def needs_adversarial_evidence(item: Item) -> bool:
    return (item.effort or 0) >= ADVERSARIAL_EFFORT_THRESHOLD


def sign_off(db: Session, *, item_id: str, agent_id: str, evidence: list | None = None) -> Item:
    """Take a reviewed item to `done`.

    The second of two independent gates on the invariant. `claim_review` already filters by
    authorship, so this assertion is redundant on the happy path — deliberately. A single gate
    keyed on a QUERY is one refactor away from being keyed on the caller's current role
    instead of on authorship, and the failure would be silent: work signing itself off while
    every test about roles still passed.
    """
    item = db.get(Item, item_id)
    if item is None:
        raise ValueError(f"item not found: {item_id}")
    if item.claimed_by and item.claimed_by == agent_id:
        raise SelfReview(
            f"{agent_id} built {item.key} and cannot sign it off; "
            "another agent has to take it"
        )
    # The SECOND gate checks independence too, not just identity. `claim_review` already
    # filters on it, so this is redundant on the happy path — same reasoning as the identity
    # check above: a single gate keyed on a query is one refactor away from being keyed on
    # something weaker, and the failure would be silent.
    me, author = db.get(Agent, agent_id), (db.get(Agent, item.claimed_by)
                                           if item.claimed_by else None)
    if me is not None and not independent(me, author):
        raise SelfReview(
            f"{agent_id} is not independent of {item.claimed_by} — same call tree or same "
            f"credential and host — so signing off {item.key} would be self-review with "
            "extra steps"
        )
    # The adversarial gate (PRD-17 D9). Reviewer and adversary are different jobs and must not
    # become one habit: a reviewer CONVERGES — the queue is three deep and an agent that blocks
    # everything is a bad reviewer — while an adversary DIVERGES, where finding nothing is
    # failure. Merge them and the convergent incentive wins under queue pressure.
    #
    # So this is a PRECONDITION, not a practice. The reviewer satisfies it however it likes —
    # subagents with opposing lenses, or its own passes — and Graphban checks only that a
    # receipt exists. Convert a hoped-for behaviour into something the server checks.
    #
    # Counted across the item's WHOLE evidence set: the author's own sabotage receipts are
    # adversarial evidence, and making the reviewer re-run what is already recorded would be
    # tax rather than rigour.
    fresh = items_svc.normalize_evidence(evidence or [])
    merged = list(item.evidence or []) + fresh
    if needs_adversarial_evidence(item) and not items_svc.has_effective_sabotage(merged):
        vacuous = items_svc.vacuous_sabotages(merged)
        raise MissingAdversarialEvidence(
            f"{item.key} is effort {item.effort} and needs adversarial evidence: a `sabotage` "
            "receipt naming the claim, the mutation, and how many tests_failed"
            + (f" — {len(vacuous)} recorded sabotage(s) broke NOTHING, which means the test "
               "cannot fail rather than that the claim is guarded" if vacuous else "")
        )

    release_reservations(db, item_id=item.id)
    item.reviewed_by = agent_id
    item.status = "done"
    if fresh:
        # Normalised on the way in, so a sabotage receipt is validated here exactly as it is
        # on `update_item` — one definition of what a receipt is.
        item.evidence = merged
    item.claimed_by = None
    item.claimed_at = None
    db.commit()
    db.refresh(item)
    return item


def bounce(db: Session, *, item_id: str, agent_id: str, reason: str,
           lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Item:
    """Send an item back to `next`, pinned to its AUTHOR for one lease period (D-f).

    The author still has the worktree, the branch and the context; handing half-finished work
    to a cold agent throws away exactly what cluster assignment exists to preserve.

    **The pin lapses.** A hard author-only pin is the tempting version and it is wrong: it
    strands the item when the author never comes back, which is the common case rather than
    the exotic one — they were re-tasked, or they died.
    """
    item = db.get(Item, item_id)
    if item is None:
        raise ValueError(f"item not found: {item_id}")
    if not (reason or "").strip():
        # A bounce without a reason is a rejection the author cannot act on, and it costs
        # them a full cycle to discover that.
        raise ValueError("bounce requires a reason")
    release_reservations(db, item_id=item.id)
    author = item.claimed_by
    item.status = "next"
    item.claimed_by = None
    item.claimed_at = None
    item.assignee = ""
    item.reviewed_by = None
    item.bounce_pinned_to = author
    item.bounce_pinned_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
                                if author else None)
    item.blocker = ""
    db.commit()
    db.refresh(item)
    return item


def bounce_pin_holder(item: Item, *, now: datetime | None = None) -> str | None:
    """Who this item is currently reserved for, or None once the pin has lapsed."""
    if not item.bounce_pinned_to or not item.bounce_pinned_until:
        return None
    until = item.bounce_pinned_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return item.bounce_pinned_to if until > (now or datetime.now(timezone.utc)) else None


# ---- D4: the divvy, and reservations over a moving partition -------------------------------

def active_reservations(db: Session, project_id: str | None = None, *,
                        now: datetime | None = None) -> list[AreaReservation]:
    """Reservations that have not expired.

    Filtered lazily at READ time rather than swept by a job, the same way `_is_claimable`
    already handles stale leases. A sweeper would add a failure mode lazy evaluation cannot
    have: a stopped sweeper silently freezing the divvy, with every cluster looking permanently
    taken and no error anywhere to explain it.
    """
    now = now or datetime.now(timezone.utc)
    rows = db.scalars(select(AreaReservation)).all()
    out = []
    for r in rows:
        expires = r.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires is None or expires > now:
            out.append(r)
    if project_id:
        item_ids = {i.id for i in db.scalars(
            select(Item).where(Item.project_id == project_id)).all()}
        out = [r for r in out if r.item_id in item_ids]
    return out


def _normalise_area(area: str) -> str:
    return (area or "").strip().rstrip("/").lower()


def areas_collide(a: list[str], b: list[str]) -> list[str]:
    """The overlapping areas between two sets — the UNION of two rules, deliberately.

    The reservation check and the partition must never disagree about what "collides" means,
    and measured against each other they did, in **both** directions:

        backend/app/services/fleet.py  vs  backend/app/services
            partition: no    (`_match` compares parent directories, and these differ)
            prefix:    yes

        area/0                         vs  area/1
            partition: yes   (siblings sharing a parent)
            prefix:    no

    So each caught a case the other missed. Taking the union makes the reservation at least
    as strict as the partition it guards — the asymmetry that matters, because a reservation
    LAXER than the partition hands out work the partition had already judged colliding, which
    is precisely the failure the reservation exists to prevent. Being stricter only costs a
    little parallelism, and it costs it visibly, as a queued cluster with a stated reason.
    """
    from app.services.clustering import _match

    out = []
    for x in a:
        nx = _normalise_area(x)
        if not nx:
            continue
        for y in b:
            ny = _normalise_area(y)
            if not ny:
                continue
            prefix = nx == ny or nx.startswith(ny + "/") or ny.startswith(nx + "/")
            if prefix or _match(x, y):
                out.append(x)
                break
    return out


def claim_cluster(db: Session, *, agent_id: str, project_id: str | None = None,
                  max_items: int = 3,
                  lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    """Lease a whole non-colliding cluster and reserve its touch-areas.

    **Checked against IN-FLIGHT reservations, not only the static partition.**
    `collision_clusters` partitions a snapshot; as work lands, actual touchpoints replace
    predicted ones and the partition moves under the fleet's feet. Handing clusters out from a
    stale snapshot re-introduces exactly the collisions the divvy exists to prevent.

    Reservations are written in the SAME transaction as the claims that justify them, so there
    is no window in which items are claimed but their areas unreserved — a window a second
    agent would claim straight through.
    """
    from app.services import collision as collision_svc

    now = datetime.now(timezone.utc)
    taken = active_reservations(db, project_id, now=now)
    # Somebody else's areas. An agent's own reservations do not block it: a worker asking for
    # more work should not be refused because of the cluster it is already holding.
    blocked = [r.area for r in taken if r.agent_id != agent_id]

    for cluster in collision_svc.clusters_for_project(db, project_id):
        overlap = areas_collide(cluster.get("areas") or [], blocked)
        if overlap:
            continue
        ids = (cluster.get("items") or [])[:max_items]
        claimed = [it for it in (items_svc.claim_item(db, i, agent_id, lease_seconds=lease_seconds)
                                 for i in ids) if it is not None]
        if not claimed:
            # Lost the race for every item in this cluster — the optimistic guard in
            # `_try_claim` resolved it against us. Try the next cluster rather than failing:
            # a loser that gives up entirely turns a race into an idle agent.
            continue
        expires = now + timedelta(seconds=lease_seconds)
        for area in (cluster.get("areas") or []):
            db.add(AreaReservation(agent_id=agent_id, item_id=claimed[0].id,
                                   area=area, expires_at=expires))
        db.commit()
        return {"claimed": True,
                "items": [{"id": it.key, "stored_id": it.id, "title": it.title} for it in claimed],
                "areas": cluster.get("areas") or [],
                "predicted": bool(cluster.get("predicted")),
                "reason": ""}

    return {"claimed": False, "items": [], "areas": [], "predicted": False,
            "reason": "all ready clusters collide with in-flight work"}


def release_reservations(db: Session, *, item_id: str | None = None,
                         agent_id: str | None = None) -> int:
    """Drop reservations when work ends. Called on sign_off / release / bounce.

    Explicit release matters even though rows expire lazily: an area held for the rest of a
    lease that nobody is editing any more is a cluster the divvy will not hand out, so the
    fleet idles for up to ten minutes on work that finished.
    """
    stmt = select(AreaReservation)
    if item_id:
        stmt = stmt.where(AreaReservation.item_id == item_id)
    if agent_id:
        stmt = stmt.where(AreaReservation.agent_id == agent_id)
    rows = db.scalars(stmt).all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


# ---- D5: the Fleet view's server half ------------------------------------------------------

# Fleet keys expire in a day by default. Ephemeral BECAUSE they are handed out by a UI and
# pasted into terminals: a credential that outlives the wave it was minted for is one nobody
# remembers issuing, and "End wave" is a hard stop rather than the only cleanup.
FLEET_KEY_DAYS = 1


def mint_fleet_key(db: Session, *, user_id: str, project_id: str, role: str,
                   wave: str, label: str = "") -> tuple:
    """A credential narrowed to ONE role and tagged to this wave.

    `roles=[role]` is the ceiling from D2, so an agent on this key cannot register into a
    different role however its client is configured. The wave tag is what lets "End wave"
    revoke exactly the keys this view issued and never one a human minted by hand.
    """
    from app.security.apikey import generate_api_key

    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r}")
    row, plaintext = generate_api_key(
        db, user_id, label or f"fleet {role}", ["read", "write"], project_id, FLEET_KEY_DAYS)
    row.roles = [role]
    row.fleet_wave = wave
    db.commit()
    db.refresh(row)
    return row, plaintext


def end_wave(db: Session, *, project_id: str | None, wave: str | None = None) -> dict:
    """Revoke a wave's keys, release every lease and reservation it holds. A hard stop.

    **All of it, at once.** A half-ended wave — keys revoked but leases still held — is the
    genuinely confusing state: work that no living agent can finish, held by credentials that
    no longer authenticate, and nothing in the roster explaining why the queue is stuck.

    Only keys carrying a `fleet_wave` tag are touched. A hand-minted key is somebody's
    long-lived credential and revoking it would be a surprise this button never promised.
    """
    from app.models import ApiKey

    stmt = select(ApiKey).where(ApiKey.fleet_wave.isnot(None), ApiKey.revoked.is_(False))
    if wave:
        stmt = stmt.where(ApiKey.fleet_wave == wave)
    if project_id:
        stmt = stmt.where(ApiKey.project_id == project_id)
    keys = list(db.scalars(stmt).all())

    agents = []
    for k in keys:
        agents.extend(db.scalars(select(Agent).where(Agent.api_key_id == k.id)).all())

    released, reservations = [], 0
    for a in agents:
        for it in db.scalars(select(Item).where(Item.claimed_by == a.id)).all():
            it.claimed_by = None
            it.claimed_at = None
            it.assignee = ""
            if it.status == "in_progress":
                it.status = "next"
            released.append(it.id)
        reservations += len(db.scalars(
            select(AreaReservation).where(AreaReservation.agent_id == a.id)).all())
        for row in db.scalars(
                select(AreaReservation).where(AreaReservation.agent_id == a.id)).all():
            db.delete(row)
        # An un-acked directive is simply dropped. Nothing ever assumed it delivered — the
        # `assigned > acked` comparison IS the outbox — so there is nothing to reconcile.
        a.role_acked_at = a.role_assigned_at
    for k in keys:
        k.revoked = True
    db.commit()
    return {"keys_revoked": len(keys), "agents": len(agents),
            "leases_released": len(released), "reservations_released": reservations}


def end_wave_preview(db: Session, *, project_id: str | None, wave: str | None = None) -> dict:
    """What `end_wave` would destroy, so the confirm can name it before acting.

    A confirm that says "are you sure?" teaches people to click through it; one that says
    "revoke 4 keys, release 3 leases?" is a decision.
    """
    from app.models import ApiKey

    stmt = select(ApiKey).where(ApiKey.fleet_wave.isnot(None), ApiKey.revoked.is_(False))
    if wave:
        stmt = stmt.where(ApiKey.fleet_wave == wave)
    if project_id:
        stmt = stmt.where(ApiKey.project_id == project_id)
    keys = list(db.scalars(stmt).all())
    agent_ids = [a.id for k in keys
                 for a in db.scalars(select(Agent).where(Agent.api_key_id == k.id)).all()]
    leases = len(db.scalars(select(Item).where(Item.claimed_by.in_(agent_ids))).all()) \
        if agent_ids else 0
    reservations = len(db.scalars(
        select(AreaReservation).where(AreaReservation.agent_id.in_(agent_ids))).all()) \
        if agent_ids else 0
    return {"keys": len(keys), "agents": len(agent_ids), "leases": leases,
            "reservations": reservations}


def review_queue(db: Session, project_id: str | None = None) -> list[dict]:
    """Items awaiting review, each carrying WHO BUILT IT.

    The ban is rendered as a negative on the item — "AGT-4 built it" — rather than as a list
    of who is eligible. The refusal belongs to the item, and stating it that way is what makes
    the invariant legible at a glance instead of something a reader has to reconstruct.
    """
    stmt = select(Item).where(Item.status == "review")
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    rows = list(db.scalars(stmt.order_by(Item.sort_order, Item.number)).all())
    labels = {a.id: (a.label or a.id) for a in db.scalars(select(Agent)).all()}
    return [{
        "id": it.id, "key": it.key, "title": it.title, "branch": it.branch,
        "built_by": it.claimed_by,
        "built_by_label": labels.get(it.claimed_by) if it.claimed_by else None,
        "reviewed_by": it.reviewed_by,
    } for it in rows]


def cluster_board(db: Session, project_id: str | None = None) -> list[dict]:
    """Clusters with who holds them and — for a held-back one — WHY.

    "Collides with `backend/app/models/`, queued until AGT-2 releases" is the difference
    between a human trusting the divvy and overriding it. Without the reason a queued cluster
    looks like the fleet being stuck.
    """
    from app.services import collision as collision_svc

    taken = active_reservations(db, project_id)
    holders: dict[str, str] = {}
    for r in taken:
        holders.setdefault(_normalise_area(r.area), r.agent_id)

    out = []
    for c in collision_svc.clusters_for_project(db, project_id):
        areas = c.get("areas") or []
        blocking = [(a, holders[k]) for a in areas
                    if (k := _normalise_area(a)) in holders]
        rows = [db.get(Item, i) for i in (c.get("items") or [])]
        out.append({
            "items": [r.key for r in rows if r is not None],
            "areas": areas,
            "predicted": bool(c.get("predicted")),
            "held_by": blocking[0][1] if blocking else None,
            "blocked_on": blocking[0][0] if blocking else None,
        })
    return out


# ---- D6: allocation, and the directive downlink ---------------------------------------------

def propose_allocation(db: Session, project_id: str | None = None, *,
                       lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    """What the fleet SHOULD look like, given who is here and what is ready.

    **The server proposes; the planner commits.** Nothing here writes a role — whether the
    planner is a human clicking Apply or an orchestrator agent calling `assign_role`, both go
    through the same commit path. A proposal that assigned itself would make the Fleet view's
    diff a formality and take the decision away from the only actor positioned to weigh it.

    The shape of the answer, and the reasoning that fixes it:

    - **One worker per free cluster, never more.** A fourth worker with no non-colliding
      cluster is not a worker — it is an agent that will be refused by the divvy every time it
      asks. Proposing it as a REVIEWER puts it where the fleet is actually short, and the
      review queue is the thing that backs up when workers outnumber the work.
    - **At least one reviewer as soon as there are two agents.** With one agent there is
      nobody to review anything, so a reviewer proposal would idle the only worker.
    - Offline and quarantined agents are not allocated. They cannot act, and counting them
      produces a plan whose arithmetic is right and whose fleet does not exist.
    """
    from app.services import collision as collision_svc

    roster = [a for a in list_agents(db, project_id, lease_seconds=lease_seconds)
              if a["state"] not in ("offline", "quarantined")]
    reserved = {_normalise_area(r.area) for r in active_reservations(db, project_id)}
    free_clusters = [
        c for c in collision_svc.clusters_for_project(db, project_id)
        if not any(_normalise_area(a) in reserved for a in (c.get("areas") or []))
    ]

    n = len(roster)
    if n == 0:
        return {"workers": 0, "reviewers": 0, "mapping": [], "rationale":
                "no agents online — nothing to allocate"}
    if n == 1:
        # One agent reviews nothing, so make it a worker and say why rather than proposing a
        # reviewer that would idle the only pair of hands in the room.
        return {"workers": 1, "reviewers": 0,
                "mapping": [{"agent": roster[0]["id"], "role": "worker",
                             "cluster": (free_clusters[0]["items"] if free_clusters else [])}],
                "rationale": "one agent: nobody to review for, so it works"}

    want_workers = min(len(free_clusters), n - 1) if free_clusters else 0
    mapping = []
    for i, agent in enumerate(roster):
        if i < want_workers:
            mapping.append({"agent": agent["id"], "role": "worker",
                            "cluster": free_clusters[i]["items"]})
        else:
            mapping.append({"agent": agent["id"], "role": "reviewer", "cluster": []})
    reviewers = n - want_workers
    return {
        "workers": want_workers,
        "reviewers": reviewers,
        "mapping": mapping,
        "rationale": (
            f"{len(free_clusters)} free cluster(s) for {n} agent(s): "
            f"{want_workers} worker(s), {reviewers} reviewer(s). "
            "Agents beyond the free clusters review rather than queue for work that collides."
        ),
    }


def assign_role(db: Session, *, agent_id: str, role: str, reason: str = "") -> Agent:
    """Commit a role change. It takes effect on the agent's NEXT POLL, not on a reconnect.

    `role_assigned_at > role_acked_at` is the whole mechanism — no queue table, because the
    comparison IS the outbox and a table would be a second place for the same fact to live.

    **At most one outstanding directive per agent, ever.** Re-assigning before the first was
    collected simply overwrites it: the agent needs to know what its role is NOW, and
    delivering a superseded instruction first would have it adopt a role the planner has
    already changed its mind about.
    """
    from app.models import ApiKey
    from app.security import authz

    agent = db.get(Agent, agent_id)
    if agent is None:
        raise ValueError(f"unknown agent: {agent_id}")
    if role not in ROLES + (ALL_IN_ONE,):
        raise ValueError(f"unknown role: {role!r}")
    key = db.get(ApiKey, agent.api_key_id) if agent.api_key_id else None
    if key is not None and role != ALL_IN_ONE and role not in eligible_roles(key):
        # The credential is the ceiling and a directive cannot climb past it. Otherwise the
        # planner could issue a role the agent will be refused for on every call — a fleet
        # arguing with itself while both halves believe they are right.
        raise authz.Forbidden(
            f"{agent_id} authenticates with a key eligible for "
            f"{', '.join(eligible_roles(key))}; {role!r} is not among them",
            hint="mint a credential for that role in the Fleet view")
    agent.active_role = role
    agent.role_assigned_at = datetime.now(timezone.utc)
    caps = dict(agent.capabilities or {})
    caps["directive_reason"] = reason
    agent.capabilities = caps
    db.commit()
    db.refresh(agent)
    return agent


def pending_directive(agent: Agent) -> dict | None:
    """The directive an agent has not yet collected, or None.

    Derived from the two timestamps rather than stored as a flag, so there is no state to
    leave set after delivery — and no way for the roster and the outbox to disagree.
    """
    if agent is None or agent.role_assigned_at is None:
        return None
    assigned, acked = agent.role_assigned_at, agent.role_acked_at
    if assigned.tzinfo is None:
        assigned = assigned.replace(tzinfo=timezone.utc)
    if acked is not None and acked.tzinfo is None:
        acked = acked.replace(tzinfo=timezone.utc)
    if acked is not None and acked >= assigned:
        return None
    reason = (agent.capabilities or {}).get("directive_reason") or ""
    return {
        "type": "role_change",
        "role": agent.active_role,
        "reason": reason,
        # The machine-readable next step, so an agent adopts the role without parsing prose.
        "next": _directive_next(agent.active_role),
    }


def _directive_next(role: str) -> str:
    if role == "reviewer":
        return "call claim_review — your worker tools now return unauthorized"
    if role == "planner":
        return "call propose_allocation — you no longer claim work yourself"
    return "call claim_cluster — you are working again"


def collect_directive(db: Session, agent_id: str | None) -> dict | None:
    """Take the outstanding directive and ACK it in the same breath.

    Acked on delivery rather than on the agent's next call, because a second round trip to
    confirm is a round trip that can be lost — and a directive redelivered forever is worse
    than one delivered once, since the agent would keep re-adopting a role it already holds.
    """
    if not agent_id:
        return None
    agent = db.get(Agent, agent_id)
    directive = pending_directive(agent)
    if directive is None:
        return None
    agent.role_acked_at = datetime.now(timezone.utc)
    db.commit()
    return directive


# ---- D7: the long-poll ------------------------------------------------------------------------

# The ceiling on a park. Bounded because an unbounded block is a connection an operator cannot
# reason about and a client cannot distinguish from a hang. 60s is also roughly where an edge
# proxy starts closing idle requests, so a longer park would be severed rather than answered.
MAX_WAIT_SECONDS = 60
POLL_INTERVAL_SECONDS = 1.0


def park(db: Session, attempt, *, agent_id: str | None = None,
         wait_seconds: int | None = None, sleep=None):
    """Retry `attempt(db)` until it yields something, a directive arrives, or time runs out.

    Returns whatever `attempt` returned, or None on timeout. The whole value is in what it
    replaces: a worker spinning `claim_next` every five seconds costs twelve tool calls a
    minute and twelve manifests' worth of attention to notice nothing changed.

    **No transaction is held while parked.** `db.rollback()` before each sleep returns the
    connection to the pool, so a fleet of parked agents does not consume the pool by sitting
    still — which is the failure that would make this feature worse than the spinning it
    replaces, and it would only show up under load.

    **An outstanding directive wakes the park early.** A re-tasked agent that stayed parked
    for its full minute would keep working the old role for that minute, and the whole promise
    of D6 is that reassignment lands on the next poll. The directive is DETECTED here and
    collected by the response envelope, so it is still acked exactly once.
    """
    sleep = sleep or time.sleep
    wait = max(0, min(int(wait_seconds or 0), MAX_WAIT_SECONDS))
    deadline = time.monotonic() + wait
    slept = 0.0
    while True:
        result = attempt(db)
        if result is not None:
            return result
        if agent_id and pending_directive(db.get(Agent, agent_id)) is not None:
            return None
        # Bounded by BOTH the wall clock and the time actually slept. Wall alone is correct in
        # production and a trap under test: an injected no-op `sleep` never advances it, so the
        # loop spins hot for the full minute — which is how this arrived, as an 84-second test
        # run. Counting the sleeps means the bound holds whether or not they really happen.
        remaining = min(deadline - time.monotonic(), wait - slept)
        if remaining <= 0:
            return None
        # Release the connection BEFORE sleeping, not after waking.
        db.rollback()
        interval = min(POLL_INTERVAL_SECONDS, remaining)
        slept += interval
        sleep(interval)
