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

from app.models import Agent, AreaReservation, Enrolment, Item, Project
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

# ---- who is calling -------------------------------------------------------------------------
#
# Every fleet tool that stamps a name onto a row resolves it the same way: the `agent_id` the
# caller sent, or — in the single-agent posture, where nobody has registered — the CREDENTIAL.
# That fallback is deliberate and predates PRD-17: a solo agent must be able to work without
# registering. What it lacked was a mark. `items.reviewed_by` could hold an agent id or an API
# key's name with nothing to tell them apart, and on 2026-08-21 four items were signed off by
# `wave-refetch-2` — the label on a key minted for an unrelated probe — which reads in the
# ledger and the UI exactly like an agent that reviewed them. Nobody could have said otherwise
# from the row (GRPH-437).
#
# `key:` is that mark. It is applied in ONE place because consistency is load-bearing rather
# than cosmetic: the self-review ban is `item.built_by == agent_id`, and both sides get their
# value from here. Prefixing where work is claimed but not where it is signed off would make
# the two sides of that comparison stop matching, and the ban would pass silently — which is a
# worse bug than the one being fixed.
CREDENTIAL_PREFIX = "key:"


def caller_identity(agent_id: str | None, api_key) -> str:
    """The name to stamp on a row for this call.

    A registered agent's id is used as-is. Everything else is the credential itself, marked so
    a reader can tell which of the two they are looking at.
    """
    if agent_id:
        return agent_id
    return f"{CREDENTIAL_PREFIX}{getattr(api_key, 'name', None) or getattr(api_key, 'id', '')}"


class NotYourAgent(Exception):
    """The caller named an agent that is not on its credential."""


def minter_for(db: Session, agent_id: str, api_key) -> str:
    """Resolve a self-declared `agent_id` to a minter, refusing one that is not yours.

    `caller_identity` takes the id as given, which is right for stamping provenance on a
    row: a wrong name there is a bad record, recoverable. It is NOT right for scoping a
    destructive call. §6 says `retire_wave` "cannot reach another planner's seats", and
    a self-declared id makes that a promise rather than a property.

    So this checks the named agent is on the calling credential. **Say exactly what that
    buys and no more**: agents provisioned by one planner share its credential by
    design (PRD-19 — one credential, many seats, which is the whole point), so this
    stops a DIFFERENT credential's planner and does not separate siblings on the same
    one. Between those, the role gate is what remains. That is a coordination scope, not
    an authorization boundary, and PRD-22 D-k is explicit there is no security boundary
    here to begin with.
    """
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise NotYourAgent(f"no registered agent {agent_id!r}")
    key_id = getattr(api_key, "id", None)
    if key_id and agent.api_key_id and agent.api_key_id != key_id:
        raise NotYourAgent(
            f"agent {agent_id!r} was registered on a different credential; you can only "
            "retire seats you minted"
        )
    return agent.id


def is_credential(identity: str | None) -> bool:
    """Was this row stamped by a credential rather than by a registered agent?

    The question the ledger could not answer before: a `reviewed_by` that nobody can attribute
    to an agent is a weaker record than one that can, and the difference should be visible
    rather than inferred from whether the string happens to look like an agent id.
    """
    return bool(identity) and identity.startswith(CREDENTIAL_PREFIX)

# Recorded on a credential when all-in-one was CHOSEN, as opposed to a key that simply never
# set roles. Both resolve to all three, so without this the two are indistinguishable and a
# client-supplied `role_hint` can silently narrow a posture a human picked in the UI.
POSTURE_SINGLE = "single"

# How long a seat is redeemable. Thirty minutes is long enough to paste a prompt into four
# terminals and short enough that a code in a transcript is worth little by the time anyone
# reads it. Deliberately NOT bound to a credential or an IP as well: single-use already binds
# a seat to exactly one redemption, so binding would only break a seat whose agent moved
# machines — two mechanisms enforcing one fact.
ENROLMENT_TTL_MINUTES = 30

# Unambiguous under a human reading a code off a screen: no O/0, no I/1/L.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# States an agent can be in. `offline` is DERIVED (see `presence_state`) and never written as
# a transition — an agent that dies does not get to tell us. `quarantined` is the opposite: it
# IS stored, because it describes an agent that is demonstrably alive and still being refused.
STATES = ("idle", "working", "reviewing", "offline", "quarantined")

# Neither may be set by the agent itself. `offline` is a contradiction from something that is
# calling us; `quarantined` is a verdict about the caller, and a caller does not get to
# withdraw it by asserting a different state.
_NOT_SELF_ASSERTABLE = ("offline", "quarantined")


def _aware(dt: datetime | None) -> datetime | None:
    """UTC-aware, whatever the dialect handed back. SQLite returns naive datetimes and
    Postgres returns aware ones, so comparing a stored timestamp to `now()` without this
    raises on one engine and silently passes on the other."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
    seen = _aware(agent.last_seen_at)
    if seen is None:
        return "offline"
    if now - seen > timedelta(seconds=presence_ttl_seconds(lease_seconds)):
        return "offline"
    return agent.state if agent.state in STATES else "idle"


def is_single_posture(api_key) -> bool:
    """Was all-in-one CHOSEN for this credential, as opposed to merely unspecified?

    Only a deliberate `all-in-one` mint sets this. A legacy key and a key a fleet shares both
    read NULL and keep honouring `role_hint` — which is what a fleet running off one credential
    depends on, and why this is a separate field rather than an inference from `roles`.
    """
    return getattr(api_key, "posture", None) == POSTURE_SINGLE


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
                   parent_agent_id: str | None = None,
                   enrolment_code: str | None = None) -> Agent:
    """Register a connected process as an agent, and tell it what role it holds.

    **Always creates a row. Never reuses one by label.** Two identical Claude Code windows on
    one machine is a legitimate fleet shape, and matching an existing agent by label would
    merge two live terminals into one — the precise condition that made the old self-declared
    `agent_id` useless. The per-project sequence gives simultaneous registrations distinct ids
    by construction, so two windows racing on one key cannot collide.

    `role_hint` is a request, not an instruction: it is honoured when the credential permits
    that role, IGNORED outright on a credential minted as all-in-one, and silently clamped to
    the default otherwise. Silently, because the hint comes from a client config file — refusing the registration would strand an agent over a
    preference, and the authoritative answer is returned in `active_role` either way.
    """
    # Redeemed FIRST, and it raises before anything is written: a seat the credential may not
    # honour must not burn, and a registration that is going to be refused must not mint an
    # agent id. `consume_enrolment` only reads.
    seat = None
    if enrolment_code:
        seat = consume_enrolment(db, code=enrolment_code, project_id=project_id,
                                 api_key=api_key)
    stored_id, number = keys_svc.mint(db, project_id, "agent")
    allowed = eligible_roles(api_key)
    if seat is not None:
        # The seat IS the grant. A hint alongside it is ignored rather than merged — two
        # sources for one fact is how the role ended up self-declared in the first place.
        role = seat.role
    elif is_single_posture(api_key) and set(allowed) >= set(ROLES):
        # All-in-one was chosen for this credential, so a hint cannot narrow it. The ceiling is
        # re-checked rather than trusted: posture may only decline to NARROW, never WIDEN, or a
        # `single` marker on a role-restricted key would resolve to all-in-one — which
        # `role_for_call` treats as unrestricted, turning a label into an escalation. The hint is a
        # string from a client config; this is a posture a human selected in the UI. Honouring
        # the hint here would cost the agent `done` and `sign_off` — which is exactly the
        # "registering must never cost capability" clause below, applied to the hinted branch
        # too rather than only when no hint is given.
        role = ALL_IN_ONE
    elif role_hint in allowed:
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
        enrolment_id=seat.id if seat is not None else None,
        active_role=role, role_assigned_at=now, role_acked_at=now,
        state="idle", registered_at=now, last_seen_at=now,
    )
    db.add(agent)
    if seat is not None:
        # Consumed in the SAME transaction as the agent row. Marking it first would spend a
        # seat on a registration that then failed; marking it after would let two agents race
        # onto one seat, and two agents sharing an enrolment cannot review each other.
        seat.consumed_at = now
        seat.consumed_by = agent.id
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

    # The whole key table, not just the revoked ones: the roster also reports WHICH credential
    # each agent authenticated with. A stale key in a client config is otherwise invisible from
    # the UI — the agent, its role and its state all look correct, and the one fact that
    # explains a surprising role is the one thing not shown (found on the D-h walk, where a
    # fresh all-in-one credential was minted and the client kept presenting the previous one).
    keys = {k.id: k for k in db.scalars(select(ApiKey)).all()}
    dead_keys = {kid for kid, k in keys.items() if k.revoked}
    held: dict[str, list[Item]] = {}
    if agents:
        ids = [a.id for a in agents]
        for it in db.scalars(select(Item).where(Item.claimed_by.in_(ids))).all():
            held.setdefault(it.claimed_by, []).append(it)
    out = []
    for a in agents:
        state = ("offline" if a.api_key_id in dead_keys
                 else presence_state(a, lease_seconds=lease_seconds, now=now))
        out.append({
            "id": a.id,      # frozen, internal — what `claimed_by` and `reviewed_by` store
            "key": a.key,    # rendered from the project's CURRENT tag (PRD-13)
            "label": a.label,
            "active_role": a.active_role,
            "state": state,
            "capabilities": a.capabilities or {},
            # The DISPLAY PREFIX only — `gb_sk_ab12`. Never the plaintext, which is not stored
            # and could not be emitted even deliberately, and never the row id, which says
            # nothing a human can match against a client config.
            "credential": getattr(keys.get(a.api_key_id), "prefix", None),
            # Why an all-in-one credential produces an all-in-one agent, shown next to it.
            # `single` means a role hint cannot narrow this key; NULL means it can (GRPH-362).
            "credential_posture": getattr(keys.get(a.api_key_id), "posture", None),
            # Un-enrolled means the single-agent posture — legitimate, but not part of a
            # fleet, which is why the view groups them apart rather than mixing them in.
            "enrolled": a.enrolment_id is not None,
            # The SEAT, not just whether there is one. PRD-22 §6's specific complaint is
            # that `enrolled` carries "the consequences of revocation, never the
            # transition" — a planner watching its fleet could see an agent vanish and
            # not know which seat went with it. It is also what a supervisor needs to
            # match a child it spawned to the roster row it became: PRD-22's acceptance
            # walk step 3 asks for exactly this and could not be run without it.
            #
            # Safe to emit. It is the seat's ROW id, never its code — the code is hashed
            # and `list_enrolments` deliberately returns no fragment of it.
            "enrolment_id": a.enrolment_id,
            "dismissed": a.dismissed_at is not None,
            "worktree": a.worktree,
            "branch": a.branch,
            "branch_orphaned": has_orphaned_branch(a, state),
            # ISO string, not a datetime: this dict crosses the MCP boundary as JSON, and a
            # raw datetime raises there rather than at the call site that built it.
            "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
            # What it is holding right now. The roster's second question after "who is out
            # there" is "what is stuck with them".
            # `phase` and `phase_basis` are DERIVED server-side rather than reported by the child,
            # because three of the four adapters are vendors we do not control — see
            # `holding_phase`. They cost no extra query: `held` already loaded the full rows.
            "holdings": [_holding_dict(i, state) for i in held.get(a.id, [])],
        })
    return out


def dismiss_agent(db: Session, *, agent_id: str, undo: bool = False) -> Agent:
    """Hide an agent from the roster, or put it back. NEVER deletes.

    A row still holding work refuses: that agent is unfinished business, and hiding it would
    lose the one thing the roster exists to surface — a lease no living agent can finish, or a
    branch only a human can resolve.
    """
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise ValueError(f"unknown agent: {agent_id}")
    if not undo:
        held = db.scalars(select(Item).where(Item.claimed_by == agent_id)).all()
        # The derived definition, so this guard covers the DEAD agent too (GRPH-396). While
        # the flag was a column written only by `quarantine()`, Dismiss went straight through
        # on a crashed agent that had left a branch — exactly the row an operator dismisses.
        if held or has_orphaned_branch(agent, presence_state(agent)):
            raise ValueError(
                f"{agent_id} still holds "
                + (f"{len(held)} item(s)" if held else "an unmerged branch")
                + " — release it first, or it disappears with the work still attached")
    agent.dismissed_at = None if undo else datetime.now(timezone.utc)
    db.commit()
    db.refresh(agent)
    return agent


def restore_on_work(db: Session, agent_id: str) -> None:
    """An agent that takes work is not gone, whatever the roster was told. Called from the one
    path that hands out a lease.

    The refusal in `dismiss_agent` guards work held at that INSTANT; this is the same invariant
    arriving late. On the real roster the two states overlap — of 24 rows, 8 were heartbeating
    within seconds — so an agent dismissed while idle can claim a minute later, and without
    this the roster would hide a live lease.

    PRESENCE IS DELIBERATELY NOT ENOUGH. A heartbeat is also how an abandoned process behaves,
    and restoring on one would make a chatty idle agent impossible to dismiss — which is most
    of what dismissal is for. Taking work is the act that has to be visible.
    """
    agent = db.get(Agent, agent_id)
    if agent is not None and agent.dismissed_at is not None:
        agent.dismissed_at = None
        db.commit()


def fleet_status(db: Session, project_id: str | None = None, *,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS,
                 minted_by: str | None = None) -> dict:
    """The roster plus the clock every member must obey.

    The intervals travel WITH the roster on purpose: an agent that does not know the
    heartbeat cadence cannot stay alive, and making it read a constant out of documentation
    is how a fleet ends up with members that disagree about what alive means.

    `minted_by` adds the SEATS that agent issued, and it lives here rather than on a
    tool of its own for a reason that is not only budget: the roster's second question
    has always been "what became of the one I sent", and a seat's state is half that
    answer. §6's complaint is that `enrolled` reports "the consequences of revocation,
    never the transition" — an agent vanishing from the roster and a seat being revoked
    are the same event seen from two places, and separating them across two calls is
    what made the transition invisible.

    Omitted when not asked for, so nothing changes for an agent that does not mint.
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
    seats = list_enrolments(db, project_id, minted_by=minted_by) if minted_by else None
    return {
        "agents": agents,
        **({"seats": seats} if seats is not None else {}),
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
    # Mint, list and retire are ONE capability with one scope (PRD-22 §6), so retiring
    # is gated exactly as minting is. The containment argument is the same and it is
    # structural: a planner cannot build, so it has no authored work that revoking its
    # own seats could launder.
    "retire_wave": ("planner",),
    # The orchestrator plans; it does not quietly do the work.
    "claim_next": ("worker",),
    "next_cluster": ("worker",),
    "claim_cluster": ("worker",),
    # A reviewer holds a REVIEW claim, which is a hold it must be able to hand back — the
    # release verb is one verb for whichever hold you have, not one per role (GRPH-429).
    "release_item": ("worker", "reviewer"),
    # `heartbeat` is NOT here, and was, which is the bug. It does two jobs: extend an item
    # LEASE and extend agent PRESENCE. Grouping it with the claiming tools gated both — so a
    # reviewer or planner was refused the only call that keeps it on the roster, registered
    # fine, and vanished 150s later while its terminal sat open and healthy. Found on the
    # PRD-17 walk: `role_refused ... heartbeat` for the reviewer AND the planner, minutes
    # apart. The lease half needs no role gate because it is already bounded by ownership —
    # `items.heartbeat` only extends a lease the caller holds.
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
    # PLANNER-ONLY, and that restriction IS the safety argument rather than a check bolted
    # beside it. A worker that could mint would build an item, mint itself a reviewer seat,
    # register as a fresh agent — new id, new enrolment, therefore independent — and sign off
    # its own work, invisibly to an authorship ban keyed on agent id. Planners are refused
    # `claim_next` two entries up, so a planner has NO AUTHORED WORK TO LAUNDER. If this ever
    # gains a second role, that argument evaporates silently; a test asserts it does not.
    "mint_enrolment": ("planner",),
    "create_prd": ("planner",),
    "update_prd": ("planner",),
    "decompose_prd": ("planner",),
    "grill_prd": ("planner",),
    # `answer_grill` joins its four siblings (GRPH-514). Authoring a PRD is planner work and
    # the other four verbs that do it — create, update, decompose, grill — have said so since
    # PRD-17. This one was ungated, which meant a worker mid-build could relay an answer into
    # an interrogation it is not part of.
    #
    # It does NOT change whether an AGENT may call it. AL-299 decided that deliberately —
    # `test_authority_gates.py` records the reason, "relays an author's answer into the grill;
    # recorded as agent-supplied" — and that stays true. Agent-callable and planner-only are
    # different axes, which is why `create_prd` has always been both.
    "answer_grill": ("planner",),
}

#: Prefix marking a tool that is open because NOBODY HAS ARGUED IT, as opposed to one that is
#: open for a stated reason. Both are entries in `OPEN_TOOLS`; only these are debt, and
#: `test_authority_gates.py` pins how many there may be so the list can shrink and not grow.
UNARGUED = "NOT ARGUED"

#: Why a tool carries NO role gate (GRPH-516).
#:
#: `TOOLS` already has a completeness guard: add a tool and you must classify it as a quality
#: gate or an authority one or the suite goes red. `TOOL_ROLES` had no equivalent, so the
#: default was silently "every role may call this", and forty tools reached that default
#: without anyone arguing for it. Some are certainly right; nobody could tell which from the
#: file. Every tool now appears in exactly one of these two maps, so a new one forces the
#: question rather than inheriting an answer.
#:
#: This records what is TRUE TODAY. It deliberately gates nothing new: `heartbeat` is the
#: warning — it was gated, that was the bug, and it broke presence for every reviewer and
#: planner, which registered fine and vanished 150s later. Four more gates today would be
#: four more chances to repeat that. The guard makes the next forty arrive already argued.
#:
#: The saving is not only about permissions. Role narrowing can only remove GATED tools, so
#: how complete `TOOL_ROLES` is bounds how much the session manifest can ever shrink — an
#: unargued list is also why the narrowing saves less than it looks like it should.
OPEN_TOOLS: dict[str, str] = {
    # ---- reads: the gate exists to stop ACTIONS, not sight -------------------------------
    # Every role has to be able to see the board it works on, and a role that could not read
    # would have to be told what it holds by another agent — which is a worse property than
    # anything reading could cost. None of these mutate.
    **{name: "a read; every role must be able to see the board it works on" for name in (
        "code_neighbors", "fleet_status", "generate_digest", "get_backlog", "get_code_map",
        "get_context", "get_item_details", "get_prd", "graph_query", "learning_loop",
        "list_projects", "prd_acceptance", "prd_coverage", "related_work", "search_code",
        "search_items", "search_memory", "suggest_next",
    )},
    "setup_project": "a read despite the name — returns a checklist, changes nothing",
    "collision_clusters": "the divvy a planner allocates FROM; seeing the partition is not "
                          "taking from it, and a worker that could not see it would have to "
                          "be told which files are safe",

    # ---- the two whose absence is load-bearing -------------------------------------------
    "register_agent": "MUST stay open: a caller cannot hold a role before it registers, so "
                      "gating this deadlocks every agent at its first call",
    "heartbeat": "MUST stay open (PRD-17 walk): it extends agent PRESENCE as well as an item "
                 "lease. Gating it with the claiming verbs took reviewers and planners off "
                 "the roster 150s after they registered. The lease half needs no gate — it "
                 "only ever extends a lease the caller already holds",

    # ---- work verbs: bounded by ownership rather than by role ----------------------------
    "create_item": "any role may record work that needs doing; creating is not claiming",
    "update_item": "the worker's verb, and its authority is bounded by WORKER_STATUS_CEILING "
                   "below rather than by this map — a role gate here would not add to it",
    "link_items": "a relation between two items is a statement about the work, not a claim "
                  "on it",
    "unlink_items": "the inverse of link_items and gated the same",

    # ---- shared context: memory and the code graph ---------------------------------------
    "add_memory": "memory is shared context; a role that could not contribute would make "
                  "every other role's context worse",
    "publish_memory": "promotes a shard already written; the authority question is the "
                      "human review gate on memory, not the caller's fleet role",
    "reject_memory": "the inverse of publish_memory and gated the same",
    "describe_code": "records what a symbol is. The code graph is shared context and a "
                     "worker is the role that has just read the code",
    "link_code": "attaches a path to an item — a statement about work already claimed",
    "unlink_code": "the inverse of link_code and gated the same",
    "report_graphban_issue": "feedback about Graphban itself, to its maintainers. It touches "
                             "no project data, so no project role bears on it",

    # ---- open, and NOBODY HAS ARGUED IT (GRPH-516) ---------------------------------------
    # Each of these is a candidate the ticket names and none is obvious. They are recorded as
    # debt rather than given an invented rationale, because a fabricated argument here reads
    # exactly like a considered one and would close the question wrongly.
    "close_prd": f"{UNARGUED}: terminal and irreversible. test_authority_gates argues it is a "
                 f"quality gate, which is the human-vs-agent axis — it says nothing about a "
                 f"worker closing a PRD mid-build",
    "request_rebaseline": f"{UNARGUED}: asks for new governing intent, beside update_prd "
                          f"which is planner-only",
    "propose_allocation": f"{UNARGUED}: the orchestrator planning a fleet allocation, beside "
                          f"assign_role and mint_enrolment which are planner-gated",
    "submit_verdict": f"{UNARGUED}: review-shaped, while sign_off and bounce are "
                      f"reviewer-only",
    "review_recommendation": f"{UNARGUED}: approves or rejects a proposed artifact — the "
                             f"human boundary, and which fleet role may stand at it is "
                             f"undecided",
    "create_project": f"{UNARGUED}: self-host only and already refused once an instance is "
                      f"linked, but no argument records which roles may create one",
    "extract_lessons": f"{UNARGUED}: distils an item into memory. Reads like worker work "
                       f"and is gated as nobody's",
}


# `update_item` is special: the tool is a worker's, but ONE argument on it is not. A worker
# moves work as far as `review` and no further — `done` is the reviewer's word, and letting a
# worker write it would make the self-review ban decorative while leaving every test green.
WORKER_STATUS_CEILING = "review"
_BEYOND_WORKER = ("done",)


def seat_of(db: Session, agent: Agent | None):
    """The enrolment an agent holds, or None."""
    if agent is None or not agent.enrolment_id:
        return None
    return db.get(Enrolment, agent.enrolment_id)


def session_expired(db: Session, agent: Agent | None) -> bool:
    """Did this agent's SEAT stop being valid under it?

    Ending a wave revokes seats rather than credentials (PRD-19 D-e), which is the whole
    reason a credential no longer has to be per-wave: the config keeps authenticating and only
    the grant goes away.
    """
    seat = seat_of(db, agent)
    return seat is not None and enrolment_state(seat) != "consumed"


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
            # THE SEAT IS THE GRANT, so losing it loses the role — not by rewriting
            # `active_role`, which stays as the record of what this agent held, but by
            # resolving to a role no tool requires. Ending a wave therefore stops an agent
            # working without touching the credential it authenticates with.
            if session_expired(db, agent):
                return "expired", agent.id
            # An all-in-one agent is unrestricted, exactly as it was before it registered.
            # Its ceiling is still the credential's, so a narrowed key cannot reach this.
            if agent.active_role == ALL_IN_ONE:
                return "*", agent.id
            return agent.active_role, agent.id
    allowed = eligible_roles(api_key)
    if len(allowed) == 1:
        return allowed[0], None
    # An unrestricted credential with NO live specialised agents is the single-agent posture:
    # unrestricted, exactly as before PRD-17. But if this credential is running a fleet, an
    # anonymous call cannot be assumed to be the unrestricted one — that assumption is how a
    # worker wrote `done` on the acceptance walk. `update_item` did not even ADVERTISE
    # `agent_id`, so the gate was unreachable through the published schema and every test that
    # "proved" it passed the parameter by hand.
    if _has_specialised_agents(db, api_key):
        return "unidentified", None
    return "*", None


def _has_specialised_agents(db: Session, api_key) -> bool:
    """Does this credential have a LIVE agent holding a narrowed role?

    Live matters: yesterday's dead fleet must not lock out today's single agent. Cheap — one
    indexed lookup on a column already loaded for the roster.
    """
    key_id = getattr(api_key, "id", None)
    if not key_id:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=presence_ttl_seconds())
    return db.scalar(
        select(Agent.id)
        .where(Agent.api_key_id == key_id, Agent.active_role.in_(ROLES),
               Agent.last_seen_at >= cutoff)
        .limit(1)) is not None


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
    if role == "unidentified" and (required or (tool == "update_item"
                                                and (args or {}).get("status") in _BEYOND_WORKER)
                                  or (tool == "release_item"
                                      and (args or {}).get("to_status", "next") in _BEYOND_WORKER)):
        raise authz.Forbidden(
            f"{tool} needs to know which agent is calling: this credential is running a fleet, "
            "so an unidentified caller cannot be assumed to be unrestricted",
            hint="pass agent_id — the value register_agent returned")
    if required and role == "expired":
        # An expired session grants NO role, so every role-gated tool refuses — but only
        # those. The shared reads stay open deliberately: `fleet_status` is how the agent
        # collects the `session_expired` directive telling it to re-enrol, and refusing that
        # too would make the remedy unreachable from inside the agent. Found by the test for
        # the directive, which could not receive one.
        raise authz.Forbidden(
            f"{who}'s enrolment is no longer valid — the wave ended, or the seat was revoked "
            f"— so {tool} has no role behind it",
            hint="register again with a fresh enrolment code from the Fleet view")
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

    # `release_item` is how a worker hands work back — the release verb is shared across
    # holds, not a second peer to `sign_off`. A worker may release to `next`/`backlog`;
    # but not to anything beyond the review ceiling, or it undoes that ceiling entirely.
    if tool == "release_item" and role == "worker":
        to_status = (args or {}).get("to_status", "next")
        if to_status in _BEYOND_WORKER:
            raise authz.Forbidden(
                f"release_item(to_status={to_status!r}) requires role 'reviewer'; "
                f"{who} is registered as 'worker'",
                hint="release_item does not write `done`; a reviewer takes it from there",
            )


def tools_off_limits(role: str) -> list[str]:
    """The gated tools this role will be refused, sorted.

    The manifest cannot say this. `tools/list` is fetched at connect, before any role exists,
    so a fleet agent holds the full list all session and finds the boundary by walking into
    it — three refusals in a row is also how `quarantine` decides an agent has stopped
    listening, so discovering the edge by trial is not free.

    This is NOT a security surface and must never be read as one: it names what WILL be
    refused, and the refusal itself is what enforces it. An agent that ignores this list is
    exactly as constrained as one that reads it.

    All-in-one is unrestricted, so the honest answer for it is an empty list rather than a
    reassuring sentence.
    """
    if role == ALL_IN_ONE or role not in ROLES:
        return []
    return sorted(name for name, allowed in TOOL_ROLES.items() if role not in allowed)


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
    db.commit()
    # Derived, not written (GRPH-396). A quarantined agent that declared a branch left it
    # behind — and so does a dead one, which is what writing it here could never say.
    return {"quarantined": True, "released_items": released,
            "released_reservations": len(reservations),
            "branch_orphaned": has_orphaned_branch(agent, "quarantined")}


# ---- D3: review, sign-off, and the self-review ban -----------------------------------------

class SelfReview(Exception):
    """An agent tried to pass its own work. Raised rather than returned, because every call
    site must handle it — a caller that ignores a `False` return would sign the item off."""


# What an agent may declare to distinguish itself from another on the SAME credential, in the
# order a human would read them. `instance` exists for clients that cannot hold more than one
# credential at a time — Cursor stores one MCP config and reuses it across every agent, so
# without a tag its whole fleet is one indistinguishable blob and no review is ever independent.
#
# Self-reported, like `host` and `worktree`, and deliberately so: the alternative for those
# clients is no fleet at all. It buys COORDINATION, not an adversarial boundary — an agent that
# wants to review its own work can simply claim a different instance. What it prevents is the
# accident, which is the failure that actually happens.
_DISCRIMINATORS = ("instance", "worktree", "host", "vendor")


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
    - **Different credentials.** The intended path: the Fleet view mints one per role, so two
      agents holding different keys are separate by construction.
    - **One credential, two SEATS.** Independent, and this is the path PRD-19 exists to make
      the normal one: the human issued two enrolments and each agent redeemed one, single-use,
      so the server decided it rather than an agent declaring it. Requires BOTH to be enrolled
      — an enrolled agent beside an un-enrolled one tells us nothing about the second.
    - **One credential, neither enrolled, and nothing declared that differs.** Not independent.
      An agent must SHOW a difference — `instance`, `worktree`, `host` or `vendor` — and
      absence is not a difference.

    **That last polarity used to be backwards and it mattered.** An unreported host counted as
    "different", so two agents declaring nothing could review each other while an agent that
    honestly reported a matching host was refused: the missing datum granted the permission,
    and laundering a self-review cost nothing but omitting a field.

    `instance` remains the fallback for an un-enrolled fleet sharing a credential. It is
    self-reported, so it buys coordination rather than an adversarial boundary — an agent
    determined to review its own work can claim a different instance. **An enrolment cannot be
    self-asserted**, which is why enrolling is the recommended path and this is the weaker one.
    """
    if author is None:
        return True                      # human-authored, or an author nothing recorded
    if reviewer.id == author.id:
        return False                     # the original ban
    if reviewer.parent_agent_id == author.id or author.parent_agent_id == reviewer.id:
        return False
    if reviewer.parent_agent_id and reviewer.parent_agent_id == author.parent_agent_id:
        return False                     # siblings under one parent are one call tree
    if not (bool(reviewer.api_key_id) and reviewer.api_key_id == author.api_key_id):
        return True                      # genuinely separate credentials
    # ONE credential — so the question is whether these are two SESSIONS.
    #
    # Different seats are different sessions, and that is decided by the SERVER: the human
    # issued two enrolments and each agent redeemed one, single-use. Everything below this line
    # is self-reported and can only approximate it. This is the whole point of PRD-19 — a
    # client that stores one credential for every agent (Cursor) can still run a fleet, because
    # independence stops depending on an agent remembering to declare a tag.
    #
    # BOTH must be enrolled. An enrolled agent and an un-enrolled one on one credential tell us
    # nothing: the un-enrolled one could be any process at all, including the same one twice.
    if reviewer.enrolment_id and author.enrolment_id:
        return reviewer.enrolment_id != author.enrolment_id
    # Otherwise: independence must be EARNED by declaring something that differs, and
    # **absence is restrictive** — the polarity matters and used to be backwards. Treating an
    # undeclared host as "different" meant an agent that honestly reported its host disabled
    # its own reviewing while one that omitted it was permitted: the missing datum granted the
    # permission, which is this repo's defect class pointed at its own gate.
    for field in _DISCRIMINATORS:
        a = (reviewer.capabilities or {}).get(field)
        b = (author.capabilities or {}).get(field)
        if a is not None and b is not None and a != b:
            return True
    return False


NOT_INDEPENDENT = ("the only work in review was built by an agent you are not distinguishable "
                   "from — same credential, same session. Redeem an enrolment seat at "
                   "register_agent (the Fleet view issues one per agent), or failing that "
                   "declare a distinct capabilities.instance, or use a per-role credential — "
                   "so review means something")


def _independent_of_author(db: Session, item: Item, agent_id: str, *,
                           api_key=None) -> bool:
    """Is this caller independent of whoever built the item? False when it built it itself.

    An UNREGISTERED caller used to be declared independent by fiat — `me is None or …` — and
    that was a bypass rather than a lenience (GRPH-437). The route to it needed no privilege:
    an agent builds an item, its heartbeat lapses (which is the ordinary end of every session),
    the credential stops counting as "running a fleet", and the same process signs its own work
    off unidentified. `built_by` held an agent id and `reviewed_by` the key's name, so the two
    were different strings and every invariant test read it as reviewed by somebody else.

    A bare credential cannot demonstrate independence from an agent that ran on THAT credential
    — it is at best the same operator and at worst the same process. It stays independent of
    everyone else, so a second person holding a different key can still review normally.

    `api_key` is optional only because internal callers construct their own sessions; when it
    is absent the credential half cannot be evaluated and this falls back to the old answer.
    Every path a client can reach passes it.
    """
    if item.built_by == agent_id:
        return False
    me = db.get(Agent, agent_id)
    author = db.get(Agent, item.built_by) if item.built_by else None
    if me is None:
        key_id = getattr(api_key, "id", None)
        if author is not None and key_id and author.api_key_id == key_id:
            return False
        return True
    return independent(me, author)


def could_review(db: Session, *, item: Item, exclude_agent_id: str,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS) -> str | None:
    """Some OTHER live agent that could legitimately review this item, or None.

    This is the condition that keeps danger mode honest. The project flag says the operator
    accepts self-review; this says the fleet has nobody else to do it. Both are required,
    because a bypass that can be taken while a reviewer is sitting idle is not an escape hatch
    — it is the review gate switched off for everyone.

    Eligibility is the union of the two gates a real reviewer passes: it must be able to CALL
    `claim_review` (a worker and a planner cannot), and it must be `independent` of the author.
    Anything offline, quarantined, dismissed or on an expired seat cannot act at all, so
    counting it would let a dead agent hold a gate open.
    """
    author = db.get(Agent, item.built_by) if item.built_by else None
    for row in list_agents(db, item.project_id, lease_seconds=lease_seconds):
        if row["id"] == exclude_agent_id or row["state"] in ("offline", "quarantined"):
            continue
        cand = db.get(Agent, row["id"])
        if cand is None or session_expired(db, cand):
            continue
        if cand.active_role not in ("reviewer", ALL_IN_ONE):
            continue
        if independent(cand, author):
            return cand.id
    return None


def self_review_allowed(db: Session, *, item: Item, agent_id: str) -> bool:
    """Danger mode, and the two conditions it takes (GRPH-380)."""
    project = db.get(Project, item.project_id) if item.project_id else None
    if project is None or not project.allow_self_review:
        return False
    return could_review(db, item=item, exclude_agent_id=agent_id) is None


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
    taken = []
    for it in db.scalars(stmt).all():
        if it.built_by == agent_id:
            continue
        holder = review_claim_holder(it)
        if holder is not None and holder != agent_id:
            taken.append(holder)
            continue
        author = db.get(Agent, it.built_by) if it.built_by else None
        if me is not None and not independent(me, author):
            return NOT_INDEPENDENT
    if taken:
        # Now that a review claim is a real lease (GRPH-395) it can be the reason for an empty
        # answer, and "nothing waiting" would be a lie with a queue full of work: the fleet is
        # busy, not idle, and this reviewer should wait rather than go looking for a problem.
        return (f"every item awaiting review is already being reviewed — by "
                f"{', '.join(sorted(set(taken)))}; their claims lapse if they go silent")
    return "no item awaiting a second pair of eyes"


def claim_review(db: Session, *, agent_id: str, project_id: str | None = None,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS,
                 skip: list[str] | None = None) -> Item | None:
    """Lease an item awaiting review that this agent did NOT build.

    **`WHERE built_by != caller` is the entire invariant** — authorship, not the lease. It was
    `claimed_by` and that is how End wave defeated it: releasing a lease erased the author, and
    `independent(reviewer, None)` reads "nothing to be independent of" (GRPH-377). With one agent in the fleet this
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
    declined = {s for s in (skip or [])}
    candidates = [
        it for it in db.scalars(stmt.order_by(Item.sort_order, Item.number)).all()
        # Declined this round — see `claim_next`. A reviewer that must refuse the top item
        # (its own work) otherwise gets it back on every call and never sees the rest.
        if it.id not in declined and it.key not in declined
        # The ban, keyed on AUTHORSHIP rather than on role. The obvious attack on a dynamic
        # role system is to promote a worker to reviewer while it holds its own item; it does
        # not work, because an agent's id does not change when its role does.
        if it.built_by != agent_id
        # Already being reviewed by somebody else — while their claim is still LIVE. A
        # reviewer that went silent releases it, the same way a worker's lease releases.
        and (review_claim_holder(it, lease_seconds=lease_seconds) or agent_id) == agent_id
        # And separate enough for the review to mean anything (GRPH-361).
        and (me is None or independent(me, db.get(Agent, it.built_by) if it.built_by else None))
    ]
    if not candidates:
        return None

    my_vendor = ((me.capabilities or {}).get("vendor") if me else None)
    if my_vendor:
        authors = {a.id: (a.capabilities or {}).get("vendor")
                   for a in db.scalars(select(Agent)).all()}
        cross = [it for it in candidates if authors.get(it.built_by) != my_vendor]
        candidates = cross or candidates

    item = candidates[0]
    # The HOLD, not the verdict. `reviewed_by` is written once, at sign-off, by whoever
    # actually decided — so an abandoned review can never look like a completed one.
    item.review_claimed_by = agent_id
    item.review_claimed_at = datetime.now(timezone.utc)
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


class NotInReview(Exception):
    """A review verdict on work that was never submitted for review (GRPH-383).

    Both verdicts checked WHO was calling and never WHETHER the work had been handed over.
    Found by using the fleet: an item `in_progress` and leased to one agent was taken straight
    to `done` by another. Every gate that existed passed — the two were genuinely independent —
    which is why it survived a suite that tests all of them.

    A verdict on unsubmitted work is not review. It ends somebody else's lease mid-change and
    records a decision about a diff nobody was shown.
    """


def _require_in_review(item: Item, verb: str) -> None:
    if item.status != "review":
        raise NotInReview(
            f"{item.key} is {item.status}, not awaiting review, so there is nothing to {verb} — "
            + ("it is still being worked on by "
               f"{item.claimed_by}; wait for it to reach review" if item.claimed_by
               else "the agent working it has to move it to review first"))


def needs_adversarial_evidence(item: Item) -> bool:
    return (item.effort or 0) >= ADVERSARIAL_EFFORT_THRESHOLD


def sign_off(db: Session, *, item_id: str, agent_id: str, evidence: list | None = None,
             api_key=None, commit: str | None = None) -> Item:
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
    # BEFORE the authorship gates, because "this was never submitted" is the more fundamental
    # refusal: reporting a self-review on an item still in progress would send the caller
    # looking for a second agent when the actual answer is that the work is not finished.
    _require_in_review(item, "sign off")
    # A live claim by SOMEBODY ELSE. Without this the lease is advisory — two reviewers can
    # both work an item and the second one's verdict simply lands, which is the duplicated
    # effort `claim_review` exists to prevent. An unclaimed item stays signable, because
    # nothing requires a reviewer to claim before deciding.
    holder = review_claim_holder(item)
    if holder is not None and holder != agent_id:
        raise NotInReview(
            f"{item.key} is being reviewed by {holder}; wait for their verdict or take other "
            "work with claim_review")
    # Danger mode is checked ONCE, here, and its answer is reused by the second gate below.
    # Asking twice would mean two chances to answer differently — and this is exactly the kind
    # of gate where a later refactor makes one of them read a weaker condition.
    # Asked ONCE and reused by both gates below. Two calls would be two chances to answer
    # differently, and this is exactly the kind of gate where a later refactor makes one of
    # them read the weaker condition.
    indep = _independent_of_author(db, item, agent_id, api_key=api_key)
    danger = (item.built_by == agent_id or not indep) and \
        self_review_allowed(db, item=item, agent_id=agent_id)
    if item.built_by and item.built_by == agent_id and not danger:
        raise SelfReview(
            f"{agent_id} built {item.key} and cannot sign it off; "
            "another agent has to take it"
            + ("" if could_review(db, item=item, exclude_agent_id=agent_id)
               else " — no other agent here can review it either, so this item needs a second "
                    "agent, or the project owner has to turn on self-review")
        )
    # The SECOND gate checks independence too, not just identity. `claim_review` already
    # filters on it, so this is redundant on the happy path — same reasoning as the identity
    # check above: a single gate keyed on a query is one refactor away from being keyed on
    # something weaker, and the failure would be silent.
    if not indep and not danger:
        if is_credential(agent_id):
            # The unidentified caller. Named separately because the fix is different: there is
            # no instance to distinguish and no seat to redeem — the caller has to say who it
            # is, or somebody on another credential has to take it.
            raise SelfReview(
                f"this call is not identified as an agent, and {item.built_by} — which built "
                f"{item.key} — runs on this same credential, so signing it off here would be "
                "self-review by an anonymous caller. Pass agent_id (the value register_agent "
                "returned), or let an agent on a different credential review it"
            )
        raise SelfReview(
            f"{agent_id} is not independent of {item.built_by} — same call tree, or one "
            f"credential and one session — so signing off {item.key} would be self-review "
            "with extra steps. Redeem your own enrolment seat at register_agent, or declare "
            "a distinct capabilities.instance, or use a per-role credential"
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
    # One appender for the whole codebase (GRPH-494). This path already appended; routing it
    # through the shared helper is what makes "the record only grows" a property of the field
    # rather than a habit each writer has to remember.
    merged = items_svc.append_evidence(item.evidence, evidence or [])
    if needs_adversarial_evidence(item) and not items_svc.has_effective_sabotage(merged):
        vacuous = items_svc.vacuous_sabotages(merged)
        raise MissingAdversarialEvidence(
            f"{item.key} is effort {item.effort} and needs adversarial evidence: a `sabotage` "
            "receipt naming the claim, the mutation, and how many tests_failed"
            + (f" — {len(vacuous)} recorded sabotage(s) broke NOTHING, which means the test "
               "cannot fail rather than that the claim is guarded" if vacuous else "")
        )

    # THE FIRST ATTESTATION ADAPTER (GRPH-544). The gates above already decided this item is
    # finished; this records WHAT WAS CHECKED in a form the completion gate can read, so the
    # verdict stops being a status change somebody made and becomes proof attached to the item.
    #
    # sign_off is the adapter that needs no external service, which is what keeps the offline
    # guarantee true: a default install always has one, so completion can never become
    # unreachable for want of CI.
    #
    # `commit` is REQUIRED to attest and deliberately not defaulted. An attestation names the
    # revision it vouches for; inventing one — a sentinel, or the item's PR string — would
    # produce a receipt that looks binding and vouches for nothing, and the gate reading it
    # later could not tell the difference.
    #
    # No commit means NO RECEIPT AT ALL, and deliberately not a note explaining the absence.
    # The first version wrote one, and it fired on every sign_off in the tree — today none of
    # them pass a commit — which is the always-firing warning this repo already refuses
    # elsewhere ("a warning that always fires is noise, and noise is how the real ones get
    # ignored", test_hosted_hardening). The gap stays countable without it: an un-attested
    # completion is exactly an item where `attestation_receipts(item.evidence)` is empty, and
    # that is a query rather than a receipt on every row.
    if commit:
        attestation = items_svc.normalize_evidence([{
            "kind": "attestation",
            "adapter": "fleet.sign_off",
            "commit": commit,
            "predicates": [
                {"name": "independent_review",
                 "passed": True,
                 "detail": f"signed off by {agent_id}"
                           + (" under danger mode — no independent agent was available"
                              if danger else f", independent of {item.built_by or 'the author'}")},
                {"name": "adversarial_evidence",
                 "passed": True,
                 "detail": (f"effort {item.effort} needs adversarial evidence and the item "
                            "carries an effective sabotage receipt"
                            if needs_adversarial_evidence(item)
                            else f"effort {item.effort} is below the threshold of "
                                 f"{ADVERSARIAL_EFFORT_THRESHOLD}; not required")},
            ],
        }])
        fresh, merged = fresh + attestation, merged + attestation
    release_reservations(db, item_id=item.id)
    item.reviewed_by = agent_id
    # The hold is spent by the verdict. Leaving it set would keep a `done` item looking like
    # something under review, which is the confusion these two columns were split to end.
    item.review_claimed_by = None
    item.review_claimed_at = None
    item.status = "done"
    if danger:
        # A self-review that leaves no trace is indistinguishable from a reviewed one, and the
        # whole bargain of danger mode is that it is VISIBLE. Recorded as a receipt rather than
        # a column because `reviewed_by == built_by` already carries the fact — a second column
        # saying the same thing is one that can later disagree with it — while the receipt
        # records what a column cannot: that at this moment nobody else could have reviewed it.
        note = items_svc.normalize_evidence([{
            "kind": "note",
            "detail": f"self-reviewed by {agent_id} under danger mode — no independent agent "
                      "was available to review it",
        }])
        fresh, merged = fresh + note, merged + note
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
    _require_in_review(item, "bounce")
    if not (reason or "").strip():
        # A bounce without a reason is a rejection the author cannot act on, and it costs
        # them a full cycle to discover that.
        raise ValueError("bounce requires a reason")
    release_reservations(db, item_id=item.id)
    author = item.built_by
    item.status = "next"
    item.claimed_by = None
    item.claimed_at = None
    item.assignee = ""
    item.reviewed_by = None
    item.review_claimed_by = None
    item.review_claimed_at = None
    item.bounce_pinned_to = author
    item.bounce_pinned_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
                                if author else None)
    # KEPT, not just demanded (GRPH-378). Requiring a reason and dropping it left the author
    # exactly where a reasonless bounce would — the rejection arrives, the fix does not.
    item.bounce_reason = reason.strip()
    item.blocker = ""
    db.commit()
    db.refresh(item)
    return item


#: The phases a holding can be in, most specific first. `stale` and `unknown` are not
#: activities — they are the two admissions, and they exist so that "we cannot tell" can
#: never be rendered as "nothing is wrong".
PHASES = ("stale", "blocked", "review", "integrating", "verifying", "building",
          "claimed", "unknown")

#: Evidence kinds that mean the agent has RUN something, as opposed to described it.
#: `note`/`url`/`screenshot` are narration and prove no verification happened.
_VERIFYING_EVIDENCE = ("test", "sabotage")

#: Presence states in which the item's signals are frozen rather than current.
_ABSENT = ("offline", "quarantined")


def holding_phase(item: Item, state: str) -> tuple[str, str]:
    """What is this agent DOING with the item it holds? Returns `(phase, basis)`.

    DERIVED, and derived HERE, for one reason: we own `gbagent`'s loop and none of
    `claude`, `cursor-agent` or `grok`. A phase the child reports would be populated by one
    adapter and blank for the other three, and a blank column reads as an idle agent. Every
    signal below is one that any vendor already writes through the ordinary MCP surface, so
    the fleet is legible without asking a single child for anything new.

    Not computed in the supervisor either. `ALLOWED_TOOLS` is pinned to
    `{fleet_status, propose_allocation}`, so it cannot fetch an item's detail — and widening
    that allowlist to feed a DISPLAY field would hand the supervisor a worker's authority.
    The roster row carries more truth instead; the supervisor's reach does not change.

    `basis` is the point of the pair. An inference nobody can check is one they have to
    trust, so every phase says which signal produced it — including `unknown`, whose basis
    is the admission that none matched.
    """
    # FIRST, and the only rule here that is about the AGENT rather than the item.
    #
    # Phase is displayed on an agent row, so it reads as a claim about that agent. An agent
    # that died mid-item leaves an item that still says `in_progress` forever: derive from
    # the item alone and a dead worker is rendered as busy, indefinitely, which is this
    # repo's recurring defect class — the absence reads as clean. `stale` is the honest
    # answer, and it says the signals below are frozen, not that they are false.
    if state in _ABSENT:
        return "stale", f"agent {state}"
    if item.blocker:
        # The blocker text, not the status: `update_item(blocker=...)` sets the field
        # without requiring the status move, so an agent can be stuck while still
        # `in_progress`. Checking status alone would miss exactly the agent that said so.
        return "blocked", "blocker set"
    if item.status == "blocked":
        return "blocked", "status blocked"
    if item.status == "review":
        return "review", "status review"
    if item.pr:
        # A recorded PR means the push already happened, so what is outstanding is CI and a
        # reviewer — not this agent's typing. Above `verifying` because it is later in the
        # same pass: the tests that produced the receipt ran before the branch went up.
        return "integrating", "pr recorded"
    # `.get` without an isinstance check on purpose: every writer of `item.evidence` goes
    # through `append_evidence`/`normalize_evidence`, which drops non-dicts and coerces an
    # unknown kind to `note`. A defensive guard here could not fire, and this repo has a
    # habit of adding guards that can never fail and then trusting them.
    if any(e.get("kind") in _VERIFYING_EVIDENCE for e in (item.evidence or [])):
        return "verifying", "test receipt"
    if item.status == "in_progress":
        return "building", "status in_progress"
    if item.status in ("next", "backlog"):
        # Claimed — `claimed_by` is what put this item in `held` — but not yet started. A
        # real window: `claim_cluster` reserves work before the agent moves any of it.
        return "claimed", f"claimed, status {item.status}"
    return "unknown", f"no signal matched (status {item.status})"


def _holding_dict(item: Item, state: str) -> dict:
    """One roster holding: what it is, and what is being done with it.

    `id` is the RENDERED key, matching `_item_dict` and every other item the MCP surface
    emits (PRD-13). The stored id is frozen and internal; emitting it as `id` would hand an
    agent a string it cannot quote back, and would leak a retired tag into agent memory
    after a rename. `stored_id` is kept for the web UI, which addresses rows directly.
    """
    phase, basis = holding_phase(item, state)
    return {"id": item.key, "stored_id": item.id, "title": item.title, "status": item.status,
            "phase": phase, "phase_basis": basis, "bounced": was_bounced(item)}


def was_bounced(item: Item) -> bool:
    """Has this item come back at least once? (GRPH-378 keeps the reason, so we can ask.)

    Deliberately NOT a rung on the ladder above. Rework and current activity are independent
    facts: folding "fix" into the phase would force an arbitrary precedence against
    `verifying`, and the bounce would vanish from the row the moment the agent ran a test.
    `building + bounced` and `verifying + bounced` are both worth telling apart, and the
    caller can cross them freely.
    """
    return bool(item.bounce_reason)


def has_orphaned_branch(agent: Agent, state: str) -> bool:
    """Did this agent leave a branch nobody can merge? (GRPH-396)

    DERIVED, because a written flag had exactly one writer — `quarantine()`, which its own
    docstring says is "only ever reached by an agent that is demonstrably alive". So it fired
    for the drifting agent and never for the dead one, which is the case it exists for: a
    crashed agent is precisely the agent that cannot clean up after itself. Found on the walk,
    where a worker killed mid-lease left `walk/step10b` behind and nothing anywhere said so.

    The definition is simply: it declared a branch, and it is not here any more. The fleet
    releases the ITEM on its own; the branch is the part only a human can resolve, which is
    why it belongs on the roster rather than in a log.
    """
    return bool(agent.branch) and state in ("offline", "quarantined")


def review_claim_holder(item: Item, *, now: datetime | None = None,
                        lease_seconds: int = DEFAULT_LEASE_SECONDS) -> str | None:
    """Who is reviewing this right now, or None once their claim has gone stale (GRPH-395).

    Deliberately shaped like `bounce_pin_holder` below, because it is the same fact: a hold
    that must lapse. `claim_review` used to write `reviewed_by` with no expiry and nothing ever
    cleared it, so a reviewer that died removed the item from every other reviewer's candidate
    list for good — while it sat in `review` looking like ordinary queued work.

    A claim with NO timestamp counts as expired. That is what makes the 0071 backfill free
    every item the old behaviour stranded, rather than carrying the strand forward under new
    column names.
    """
    if not item.review_claimed_by:
        return None
    if item.review_claimed_at is None:
        return None
    claimed = _aware(item.review_claimed_at)
    if (now or datetime.now(timezone.utc)) - claimed > timedelta(seconds=lease_seconds):
        return None
    return item.review_claimed_by


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


# The most reservations one presence read resolves. NOT pagination: a live viewport snapshot
# cannot be paged, because half a fleet rendered looks exactly like a whole one. Over the cap
# the payload says `truncated` and reports the true `total` — a silent cut is the
# absence-reads-as-a-clean-result failure this codebase keeps naming.
PRESENCE_CAP = 200


def held_areas(db: Session, project_id: str | None = None, *,
               now: datetime | None = None,
               lease_seconds: int = DEFAULT_LEASE_SECONDS,
               cap: int = PRESENCE_CAP) -> dict:
    """Live reservations, resolved to code nodes, the agent, and the human behind it (D4).

    Reads `active_reservations`, so the LEASE CLOCK governs the glow: nothing here needs
    sweeping, and an agent that died stops holding by the same lapse that already frees its
    items. Inherited rather than reimplemented — one owner for "what is still held".

    **Areas that resolve to no node are reported, not dropped** (`off_map`). Measured on the
    live graph, 15 of 100 item touchpoints resolve to nothing: docs, config, `.cursor/rules/*`,
    and areas that are not repo paths at all (`vercel env`). A payload that silently omitted
    them would render an idle-looking codebase while someone was editing it, and its emptiness
    would be indistinguishable from nobody working. `reason` separates `undescribed` (no node
    at all) from `stale` (a node exists but `fresh=False` — `prune` marks, never deletes).

    No classification beyond that: `AGENTS.md` is a real repo path merely undescribed while
    `vercel env` never will be one, and the server cannot tell them apart from the string.
    Guessing misfiles `web/nginx.conf` one way and `../ascme-labs/**` the other.
    """
    from app.models import ApiKey, User
    from app.services import code_graph as code_svc

    now = now or datetime.now(timezone.utc)
    rows = active_reservations(db, project_id, now=now)
    total = len(rows)
    # Ordered before the cap, so a truncated payload is a deterministic prefix rather than
    # whatever the database happened to return.
    rows = sorted(rows, key=lambda r: (r.agent_id or "", r.area or "", r.item_id or ""))
    truncated = total > cap
    rows = rows[:cap]

    agents = {a.id: a for a in db.scalars(select(Agent)).all()}
    keys = {k.id: k for k in db.scalars(select(ApiKey)).all()}
    users = {u.id: u for u in db.scalars(select(User)).all()}
    nodes = code_svc.list_nodes(db, project_id) if project_id else []
    node_by_path = {n.path: n for n in nodes}

    def holder(agent) -> dict:
        # Agent -> ApiKey -> User is the whole of G4: the colour the graph tints itself with is
        # the one this person's avatar already wears everywhere else in the app. No new
        # palette, and no assignment logic to get wrong.
        key = keys.get(agent.api_key_id) if agent is not None and agent.api_key_id else None
        user = users.get(key.user_id) if key is not None and key.user_id else None
        return {
            "agent_id": agent.id if agent is not None else None,
            "agent_label": agent.label if agent is not None else "",
            "active_role": agent.active_role if agent is not None else None,
            "state": (presence_state(agent, lease_seconds=lease_seconds, now=now)
                      if agent is not None else "offline"),
            "user_id": user.id if user is not None else None,
            "user_initials": user.initials if user is not None else "",
            "user_color": user.avatar if user is not None else None,
        }

    held: list[dict] = []
    off_map: list[dict] = []
    for r in rows:
        base = {
            "area": r.area,
            "item_id": r.item_id,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            **holder(agents.get(r.agent_id)),
        }
        matched = sorted(p for p in node_by_path if code_svc.area_matches(r.area or "", p))
        fresh_paths = [p for p in matched if node_by_path[p].fresh]
        if fresh_paths:
            held.append({**base, "node_paths": fresh_paths, "predicted": bool(r.predicted)})
        if matched and len(fresh_paths) != len(matched):
            # A held area whose map is out of date carries the same message as an unplaceable
            # one, so it is reported rather than left to glow as though current.
            off_map.append({**base, "reason": "stale"})
        elif not matched:
            off_map.append({**base, "reason": "undescribed"})

    return {
        "served_at": now.isoformat(),
        "heartbeat_interval_seconds": heartbeat_interval_seconds(lease_seconds),
        "held": held,
        "off_map": off_map,
        "truncated": truncated,
        "total": total,
    }


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

    for cluster in collision_svc.clusters_for_project(db, project_id,
                                                       lease_seconds=lease_seconds):
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
                                   area=area, expires_at=expires,
                                   predicted=bool(cluster.get("predicted"))))
        db.commit()
        return {"claimed": True,
                "items": [{"id": it.key, "stored_id": it.id, "title": it.title} for it in claimed],
                "areas": cluster.get("areas") or [],
                "predicted": bool(cluster.get("predicted")),
                "reason": ""}

    # WHO is holding what, and until when. "All ready clusters collide with in-flight work" is
    # unactionable to the one caller most likely to see it: a solo human whose previous agent
    # died holding areas, for whom the answer is "wait N seconds" or "that agent is gone".
    # Same failure as an empty `claim_next` (GRPH-379) — a refusal that cannot be told apart
    # from having nothing to do.
    held = sorted({r.agent_id for r in taken if r.agent_id != agent_id})
    soonest = min((r.expires_at for r in taken if r.agent_id != agent_id), default=None)
    if held:
        free_in = max(0, int((_aware(soonest) - now).total_seconds())) if soonest else None
        reason = ("all ready clusters collide with areas held by "
                  + ", ".join(held)
                  + (f" — the earliest frees in {free_in}s" if free_in is not None else ""))
    else:
        reason = "nothing ready to claim"
    return {"claimed": False, "items": [], "areas": [], "predicted": False,
            "held_by": held, "reason": reason}


def holds_reservation(db: Session, *, agent_id: str, item_id: str) -> bool:
    """Does this agent hold an area reservation for this item? (GRPH-435)

    Existence only — one indexed lookup on the two columns that identify the pair. It answers
    the question `items.updated_at` cannot: `claim_cluster` writes its work here rather than
    on the item row, so an agent that took a cluster and touched nothing else looks, by the
    clock, exactly like one that claimed and walked away.

    Deliberately NOT solved by stamping `updated_at` when reservations are written. That
    would make the existing guard true as written, at the cost of moving the item's timestamp
    for a reason unrelated to the item's content — and `updated_at` is read by drift, by
    completeness and by every "has this changed" question in the codebase.

    Expiry is not consulted. The question is whether this agent DID the work, not whether the
    hold is still live; a lapsed reservation is still a record that they took it.
    """
    return db.scalars(
        select(AreaReservation.id)
        .where(AreaReservation.agent_id == agent_id, AreaReservation.item_id == item_id)
        .limit(1)
    ).first() is not None


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

    if role not in ROLES + (ALL_IN_ONE,):
        raise ValueError(f"unknown role: {role!r}")
    # `gate` follows the REVIEWER role (GRPH-543). Completion needs an `attestation`, and only
    # a gate-scoped key may write one — so a credential that can sign work off must be able to
    # attest it, or the verdict it is entitled to give cannot be recorded.
    #
    # This matters most in the single-agent posture, where there is no reviewer agent and the
    # human is the reviewer: without it an all-in-one agent could never finish anything, and
    # its work would park in `review` with nothing explaining why it stopped. That is exactly
    # the failure `test_the_capability_the_hint_used_to_cost_is_kept` was written against, and
    # this gate would otherwise have re-created it one layer down.
    #
    # A worker- or planner-only key does NOT get it. A worker's ceiling is `review` by design,
    # and handing it the means to attest would return the capability the role gate removes.
    scopes = ["read", "write"]
    if role in ("reviewer", ALL_IN_ONE):
        scopes.append("gate")
    row, plaintext = generate_api_key(
        db, user_id, label or f"fleet {role}", scopes, project_id, FLEET_KEY_DAYS)
    # `all-in-one` mints an UNNARROWED credential — all three roles — which is what makes an
    # agent registering on it unrestricted. It is still wave-tagged, so "End wave" sweeps it
    # like any other: the posture differs, the lifecycle does not.
    row.roles = list(ROLES) if role == ALL_IN_ONE else [role]
    # …and records that all-in-one was CHOSEN. `roles` alone cannot say so — all three is also
    # what a key with nothing set resolves to — and without the distinction a `role_hint` from
    # a client config silently narrows the posture picked in the UI.
    row.posture = POSTURE_SINGLE if role == ALL_IN_ONE else None
    row.fleet_wave = wave
    db.commit()
    db.refresh(row)
    return row, plaintext


class EnrolmentError(ValueError):
    """A seat that cannot be redeemed, with a reason a human can act on."""


def _hash_code(code: str) -> str:
    import hashlib

    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def issue_enrolment(db: Session, *, project_id: str, role: str, wave: str | None = None,
                    issued_by: str | None = None, minted_by: str | None = None,
                    reissued_from: str | None = None) -> tuple[Enrolment, str]:
    """Mint one SEAT and return (row, plaintext). The code is shown once.

    One seat per agent, never one per role: two agents redeeming the same code would share an
    enrolment and therefore fail `independent`, so a wave that looked correctly provisioned
    would have review silently disabled inside it.
    """
    import secrets
    import uuid

    if role not in ROLES + (ALL_IN_ONE,):
        raise ValueError(f"unknown role: {role!r}")
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
    code = f"{role.upper().replace('-', '')}-{body}"
    row = Enrolment(
        id=str(uuid.uuid4()), project_id=project_id, code_hash=_hash_code(code),
        code_prefix=code.split("-")[-1][:2], role=role, wave=wave,
        issued_by=issued_by, minted_by=minted_by, reissued_from=reissued_from,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=ENROLMENT_TTL_MINUTES),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, code


def mint_enrolment_as(db: Session, *, minter_id: str, project_id: str, role: str,
                      api_key, wave: str | None = None) -> tuple[Enrolment, str]:
    """A planner mints a seat for an agent it is about to spawn (PRD-19 E7 / D-g).

    An orchestrator cannot paste a code out of a UI, so the capability has to exist for an
    autonomous fleet to be possible at all. Two things keep it from becoming a self-review
    vector, and both are structural rather than extra checks:

    **The role gate makes this planner-only**, and a planner is already refused `claim_next` —
    so it has no authored work a laundered reviewer seat could sign off. The containment is
    that the role holding the capability cannot build.

    **The minted seat records `minted_by` and does NOT set parentage.** Recording the minter as
    the parent is the intuitive move and would break the feature outright: `independent` treats
    siblings under one parent as one call tree, so every seat a planner issued would be
    mutually non-independent and no agent in an autonomously provisioned fleet could review any
    other. Parentage keeps meaning what it means — a subagent declaring it runs inside another
    agent's process — and `minted_by` carries the audit trail instead.
    """
    from app.security import authz

    allowed = eligible_roles(api_key)
    if role != ALL_IN_ONE and role not in allowed:
        # The credential is still the ceiling. A planner reshuffles within what it holds; it
        # does not manufacture authority its own key was never granted.
        raise authz.Forbidden(
            f"this credential is eligible for {', '.join(allowed)}; cannot mint a {role!r} seat",
            hint="mint a credential for that role in the Fleet view first")
    return issue_enrolment(db, project_id=project_id, role=role, wave=wave, minted_by=minter_id)


def reissue_enrolment(db: Session, *, enrolment_id: str) -> tuple[Enrolment, str]:
    """Replace a dead seat, pointing back at it.

    The recovery path for a crashed agent, and the reason a seat needs no `max_uses`: the
    replacement leaves a record rather than erasing the evidence that something died.
    """
    old = db.get(Enrolment, enrolment_id)
    if old is None:
        raise EnrolmentError("no such seat")
    return issue_enrolment(db, project_id=old.project_id, role=old.role, wave=old.wave,
                           issued_by=old.issued_by, minted_by=old.minted_by,
                           reissued_from=old.id)


def enrolment_state(row: Enrolment, *, now: datetime | None = None) -> str:
    """`unused` | `consumed` | `expired` | `revoked` — DERIVED, never swept.

    Same shape as presence: there is no job to forget to run, and no window where a seat reads
    redeemable minutes after it stopped being so.
    """
    now = now or datetime.now(timezone.utc)
    if row.revoked:
        return "revoked"
    if row.consumed_at is not None:
        # Consumed OUTRANKS expiry: the TTL bounds how long a code may be REDEEMED, not how
        # long the session it granted may run. A worker mid-build does not lose its role
        # thirty minutes in — ending the wave is what takes it.
        return "consumed"
    exp = row.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return "expired" if exp is not None and exp <= now else "unused"


def consume_enrolment(db: Session, *, code: str, project_id: str, api_key) -> Enrolment:
    """Redeem a seat, or refuse with a reason. **Nothing is written on a refusal.**

    The ceiling is checked BEFORE consumption, so a code the credential may not honour does
    not burn the seat — an operator who mints the wrong pairing gets to fix it and retry with
    the code they already handed out.

    A ceiling conflict is REFUSED rather than narrowed. Clamping a reviewer seat to `worker`
    would leave the roster showing a worker where a reviewer was deliberately issued, which is
    the one state an operator cannot debug from the UI.
    """
    row = db.scalar(select(Enrolment).where(Enrolment.code_hash == _hash_code(code)))
    if row is None:
        raise EnrolmentError("no such enrolment code")
    if row.project_id != project_id:
        # Named without echoing which project, since the caller may not be entitled to it.
        raise EnrolmentError("that seat belongs to a different project")
    state = enrolment_state(row)
    if state != "unused":
        raise EnrolmentError(f"that seat is already {state}"
                             + (" — reissue it from the Fleet view" if state == "consumed" else ""))
    allowed = eligible_roles(api_key)
    if row.role != ALL_IN_ONE and row.role not in allowed:
        raise EnrolmentError(
            f"this credential is eligible for {', '.join(allowed)}; the seat grants "
            f"{row.role!r}. Mint a credential for that role, or issue a "
            f"{allowed[0]!r} seat")
    return row


def list_enrolments(db: Session, project_id: str | None = None,
                    wave: str | None = None, minted_by: str | None = None) -> list[dict]:
    """Every seat with its DERIVED state, newest first.

    `minted_by` is the scope PRD-22 §6 turns on: mint, list and retire are ONE capability
    with one bound, and it is the provenance already recorded at mint time. A planner
    passing its own id sees the seats it issued and nothing else — not another planner's,
    and not the hand-minted long-lived credentials that were never its business.

    It is also returned on every row, because a seat whose minter is invisible cannot be
    scoped by anyone reading the list, and the audit trail `mint_enrolment_as` records
    instead of parentage is only a trail if something surfaces it.

    **No part of the code comes back, not even a display fragment.** An API key shows a prefix
    because it is long-lived and a human needs to match it against a config; a seat lives for
    thirty minutes and is identified by its role and wave. Returning two characters of a
    six-character code would cut the search space from ~887M to ~923k for no benefit anyone
    asked for — small surface, but surface bought with nothing.
    """
    stmt = select(Enrolment)
    if project_id:
        stmt = stmt.where(Enrolment.project_id == project_id)
    if wave:
        stmt = stmt.where(Enrolment.wave == wave)
    if minted_by:
        stmt = stmt.where(Enrolment.minted_by == minted_by)
    rows = list(db.scalars(stmt.order_by(Enrolment.created_at.desc())).all())
    # Intentionally no state filter: a consumed/expired/revoked row is the record
    # reissue leaves behind. Dropping it here hides the dead seat from the Fleet view.
    return [{
        "id": r.id,
        "role": r.role,
        "wave": r.wave,
        "state": enrolment_state(r),
        "consumed_by": r.consumed_by,
        "minted_by": r.minted_by,
        "reissued_from": r.reissued_from,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
    } for r in rows]


def list_credentials(db: Session, project_id: str | None = None) -> list[dict]:
    """Credentials that can reach this project, with what each is FOR.

    `wave` distinguishes a wave artifact from somebody's long-lived credential — the same
    distinction End wave makes, surfaced so a human can see it before pressing anything.
    Never returns key material: `prefix` is the display fragment already stored.
    """
    from app.models import ApiKey

    stmt = select(ApiKey)
    if project_id:
        stmt = stmt.where(ApiKey.project_id == project_id)
    rows = list(db.scalars(stmt.order_by(ApiKey.created_at.desc())).all())
    agents_by_key: dict[str, int] = {}
    for a in db.scalars(select(Agent)).all():
        if a.api_key_id:
            agents_by_key[a.api_key_id] = agents_by_key.get(a.api_key_id, 0) + 1
    return [{
        "id": r.id,
        "name": r.name,
        "prefix": r.prefix,
        "wave": r.fleet_wave,
        "revoked": bool(r.revoked),
        "posture": r.posture,
        "roles": list(r.roles or []),
        "agents": agents_by_key.get(r.id, 0),
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
    } for r in rows]


def live_waves(db: Session, project_id: str | None = None) -> list[str]:
    """Waves that still own something — newest first.

    A wave is PROVISIONED while it holds at least one un-revoked seat or wave-tagged key.
    Everything else is history, and offering to end it again is noise: on the acceptance walk
    the selector listed three waves of which none had a single live seat between them.
    """
    from app.models import ApiKey

    labels: set[str] = set()
    seats = select(Enrolment).where(Enrolment.revoked.is_(False))
    keys = select(ApiKey).where(ApiKey.fleet_wave.isnot(None), ApiKey.revoked.is_(False))
    if project_id:
        seats = seats.where(Enrolment.project_id == project_id)
        keys = keys.where(ApiKey.project_id == project_id)
    for row in db.scalars(seats).all():
        if row.wave:
            labels.add(row.wave)
    for row in db.scalars(keys).all():
        if row.fleet_wave:
            labels.add(row.fleet_wave)

    def order(w: str) -> int:
        return int(w[5:]) if w.startswith("wave-") and w[5:].isdigit() else -1

    return sorted(labels, key=lambda w: (order(w), w), reverse=True)


def revoke_expired_keys(db: Session, *, project_id: str) -> int:
    """Revoke credentials that have already expired.

    EXPIRED ONLY, and deliberately not "unused": a key minted five minutes ago for a machine
    nobody has set up yet has never been used, and sweeping on that signal would revoke an
    operator's own setup before they finished it. Expiry is unambiguous — the key is already
    dead and this only tidies the list.
    """
    from app.models import ApiKey

    now = datetime.now(timezone.utc)
    rows = [r for r in db.scalars(
        select(ApiKey).where(ApiKey.project_id == project_id,
                             ApiKey.revoked.is_(False),
                             ApiKey.expires_at.isnot(None))).all()
        if r.expires_at and (r.expires_at.replace(tzinfo=timezone.utc)
                             if r.expires_at.tzinfo is None else r.expires_at) <= now]
    for r in rows:
        r.revoked = True
    db.commit()
    return len(rows)


def next_wave(db: Session, project_id: str) -> str:
    """The next unused `wave-N` for this project.

    Computed SERVER-SIDE because the client got it wrong: the Fleet view hardcoded `wave-1`,
    so every wave since PRD-17 landed in one bucket — 19 seats and 15 keys deep by the time
    anyone noticed. End wave therefore always ended *everything*, and two waves could never
    run side by side. A number the UI has to remember to increment is a number that stays 1.

    Reads both tables: a wave owns seats now and owned keys before PRD-19, and reusing a label
    from either would let End wave reach back into a cohort somebody already finished with.
    """
    from app.models import ApiKey

    labels = set(db.scalars(
        select(Enrolment.wave).where(Enrolment.project_id == project_id)).all())
    labels |= set(db.scalars(
        select(ApiKey.fleet_wave).where(ApiKey.project_id == project_id)).all())
    highest = 0
    for label in labels:
        if label and label.startswith("wave-") and label[5:].isdigit():
            highest = max(highest, int(label[5:]))
    return f"wave-{highest + 1}"


def revoke_unused_seats(db: Session, *, project_id: str, wave: str | None = None) -> int:
    """Revoke seats nobody redeemed. Consumed ones are left alone — they are the record of
    which agent took what, and End wave is the thing that stops live sessions."""
    rows = [r for r in db.scalars(_wave_seats(project_id, wave)).all()
            if r.consumed_at is None]
    for r in rows:
        r.revoked = True
    db.commit()
    return len(rows)


def issue_wave(db: Session, *, project_id: str, roles: list[str], wave: str,
               issued_by: str | None = None) -> list[tuple[Enrolment, str]]:
    """One seat per entry, so `["worker", "worker"]` issues TWO.

    Repeats are the point rather than a quirk of the API: two agents sharing a seat share a
    session and cannot review each other, so a wave of two workers needs two codes.
    """
    return [issue_enrolment(db, project_id=project_id, role=r, wave=wave, issued_by=issued_by)
            for r in roles]


def _wave_seats(project_id: str | None, wave: str | None):
    """The seats a wave owns. ONE selector, used by both the preview and the act — two would
    let the confirm name a number the button then does not deliver."""
    stmt = select(Enrolment).where(Enrolment.revoked.is_(False))
    if wave:
        stmt = stmt.where(Enrolment.wave == wave)
    if project_id:
        stmt = stmt.where(Enrolment.project_id == project_id)
    return stmt


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

    # The wave's SEATS (PRD-19 D-e). This is what makes a credential no longer need to be
    # per-wave: revoking the grant stops the agent, and the config keeps authenticating.
    seats = list(db.scalars(_wave_seats(project_id, wave)).all())

    agents = []
    for k in keys:
        agents.extend(db.scalars(select(Agent).where(Agent.api_key_id == k.id)).all())
    seat_ids = [s.id for s in seats]
    if seat_ids:
        for a in db.scalars(select(Agent).where(Agent.enrolment_id.in_(seat_ids))).all():
            if a not in agents:
                agents.append(a)

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
        # An un-acked ROLE directive is simply dropped: nothing assumed it delivered, and the
        # role it named is moot now. The session_expired directive needs no seeding here — it
        # is derived from the seat, so revoking the seat below IS the notification.
        a.role_acked_at = a.role_assigned_at
    for k in keys:
        k.revoked = True
    for s in seats:
        s.revoked = True
    db.commit()
    return {"keys_revoked": len(keys), "seats_revoked": len(seats), "agents": len(agents),
            "leases_released": len(released), "reservations_released": reservations}


def retire_wave(db: Session, *, minter_id: str, project_id: str | None,
                wave: str | None = None) -> dict:
    """A planner retires the seats IT minted. PRD-22 §6.

    Spin-up was agent-callable and spin-down was not, and that asymmetry fails in the
    direction that costs money: a fleet that can grow and cannot shrink. Mint, list and
    retire are one capability with one scope — `minted_by` — which is the provenance
    `mint_enrolment_as` already records.

    The containment argument is the one minting already makes: the capability is
    planner-only and a planner cannot build, so it has no authored work that revoking its
    own seats could launder.

    **Scope: seats this caller minted, and nothing else.** Not another planner's, and not
    API keys — `end_wave` revokes keys because a human is ending a whole wave; a planner
    never minted a key and retiring one would be a surprise it never promised. The human
    `end_wave` stays and stays broader; this is a scoped subset, not a replacement.

    **Effect: revoke those seats and release what agents on them hold, in one
    transaction.** A half-retired wave — seats dead, leases held — is the genuinely
    confusing state: work no living agent can finish, held by credentials that no longer
    authenticate, and nothing in the roster explaining why the queue is stuck.

    **It does not stop processes, and the return value refuses to imply otherwise.** The
    server has no process control; that is PRD-22's premise, not an omission. Termination
    follows because the planner then calls the supervisor's local `stop`, or the
    supervisor's backstop poll notices. So the result carries `agents_still_running` —
    agents on the retired seats that were seen within the presence TTL and are therefore
    probably still executing, right now, against seats that no longer authenticate.
    Without that number `{"seats_revoked": 4}` reads as "the wave is over", which is
    exactly the misreading that leaves four children building in the dark.
    """
    now = datetime.now(timezone.utc)
    stmt = select(Enrolment).where(Enrolment.minted_by == minter_id,
                                   Enrolment.revoked.is_(False))
    if project_id:
        stmt = stmt.where(Enrolment.project_id == project_id)
    if wave:
        stmt = stmt.where(Enrolment.wave == wave)
    seats = list(db.scalars(stmt).all())

    seat_ids = [s.id for s in seats]
    agents: list[Agent] = []
    if seat_ids:
        agents = list(db.scalars(select(Agent).where(Agent.enrolment_id.in_(seat_ids))).all())

    released, reservations = [], 0
    for a in agents:
        for it in db.scalars(select(Item).where(Item.claimed_by == a.id)).all():
            it.claimed_by = None
            it.claimed_at = None
            it.assignee = ""
            if it.status == "in_progress":
                it.status = "next"
            released.append(it.id)
        for row in db.scalars(
                select(AreaReservation).where(AreaReservation.agent_id == a.id)).all():
            db.delete(row)
            reservations += 1
        # An un-acked role directive is moot once the seat is gone; the session_expired
        # directive is derived from the seat, so revoking it IS the notification.
        a.role_acked_at = a.role_assigned_at

    still_running = [a.id for a in agents
                     if presence_state(a, now=now) != "offline"]

    for seat in seats:
        seat.revoked = True
    db.commit()

    return {
        "seats_revoked": len(seats),
        "agents": len(agents),
        "leases_released": len(released),
        "reservations_released": reservations,
        # Never omitted and never implied. An empty list means the supervisor has nothing
        # left to stop; a populated one means these processes are still going, and
        # retiring a seat did not and could not touch them.
        "agents_still_running": still_running,
        "stopped_no_processes": True,
    }


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
    return {"keys": len(keys), "seats": len(list(db.scalars(_wave_seats(project_id, wave)).all())),
            "agents": len(agent_ids), "leases": leases, "reservations": reservations}


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
        "built_by": it.built_by,
        "built_by_label": labels.get(it.built_by) if it.built_by else None,
        # WHO IS ON IT NOW. Items in this queue are `review`, so `reviewed_by` is empty here by
        # definition — the verdict is written at sign-off, which is when the item leaves. The
        # queue's question is "is somebody already looking at this", and after the split
        # (GRPH-395) that is the live claim, not the verdict.
        "reviewed_by": review_claim_holder(it),
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

    # AGENTS ON A SINGLE-POSTURE CREDENTIAL CANNOT BE RE-TASKED — `assign_role` refuses them,
    # because the posture is a property of the credential rather than a role ceiling. Proposing
    # a reviewer among them is a plan that can never be committed: the Fleet view would offer
    # an Apply the server is structurally required to refuse.
    #
    # The narrow condition is the POSTURE, not the all-in-one role. An agent that resolved to
    # all-in-one merely because its credential was unnarrowed and it stated no preference IS
    # re-taskable, and the ordinary allocation below is both committable and better for it.
    #
    # Nor do they need a reviewer proposed: an all-in-one agent files into the review pool and
    # pulls from it like every other posture, and both independence gates already govern the
    # outcome, so N of them review each other. What they need is a cluster each.
    single = [a for a in roster if a["credential_posture"] == POSTURE_SINGLE]
    if single and len(single) == n:
        mapping = [{"agent": a["id"], "role": ALL_IN_ONE,
                    "cluster": (free_clusters[i]["items"] if i < len(free_clusters) else [])}
                   for i, a in enumerate(roster)]
        return {
            "workers": n, "reviewers": 0, "mapping": mapping,
            "rationale": (
                f"{n} all-in-one agent(s) and {len(free_clusters)} free cluster(s): each takes "
                "a cluster. " + ("They review each other — an all-in-one agent files into the "
                                 "review pool and pulls from it, and cannot pass its own work."
                                 if n > 1 else
                                 "One agent has nobody to review for, so review is the human's "
                                 "— or the project owner turns on self-review.")
            ),
        }
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
    if key is not None and role != ALL_IN_ONE and is_single_posture(key):
        # The same narrowing that `register_agent` refuses, arriving through the other door.
        # Fixing only the registration path would leave a planner able to demote a deliberately
        # single-agent credential into a role that cannot finish its own work.
        raise authz.Forbidden(
            f"{agent_id} authenticates with an all-in-one credential, which is a posture "
            f"rather than a role ceiling; it cannot be re-tasked to {role!r}",
            hint="mint a role-narrowed credential in the Fleet view for a fleet agent")
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


def pending_directive(agent: Agent, *, expired: bool = False) -> dict | None:
    """The directive an agent has not yet collected, or None.

    Derived from the two timestamps rather than stored as a flag, so there is no state to
    leave set after delivery — and no way for the roster and the outbox to disagree.
    """
    if agent is None:
        return None
    if expired:
        # Checked FIRST: an agent whose seat is gone does not care what role it was last
        # assigned. Rides the existing outbox rather than a new channel — the whole point of
        # D6 was that intent travels on whatever the agent polls next.
        #
        # **And unlike a role change, this one REPEATS, deliberately.** A role change is an
        # EVENT and redelivering it would have an agent re-adopt a role it already holds. An
        # expired session is a STATE: it stays true until the agent re-enrols, so every poll
        # should keep saying so. Acking it would leave a stuck agent hearing nothing while
        # every role-gated call it makes is refused.
        return {
            "type": "session_expired",
            "role": None,
            "reason": "the wave ended, or your seat was revoked",
            "next": "call register_agent with a fresh enrolment_code from the Fleet view",
        }
    if agent.role_assigned_at is None:
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
    expired = session_expired(db, agent)
    directive = pending_directive(agent, expired=expired)
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
