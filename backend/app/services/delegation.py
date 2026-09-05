"""Delegation as a ledger fact (PRD-35): the brief, the record, the requested tier.

A fleet delegates in four harnesses and, before this, recorded it in none of them. The
only trace was `Agent.parent_agent_id`, written when the child registered — so a child that
died before registering left the parent looking idle and the item looking ready. That is
the absence-reads-as-clean class, and this table is the third state for it: `expired`, a
delegation nothing ever claimed.

Three parties, one shape (D1), copied from `propose_allocation` / `assign_role`:

- **The server states.** `brief` is what a delegate must be told, produced from the item.
  It SUGGESTS a lane and a tier and names the evidence (`basis`) for each. It never rates
  the item's difficulty — the only signal the server has is what happened last time.
- **The harness executes.** `delegate` writes what the delegator asked for; the spawn is
  invisible here and nothing pretends otherwise. `lane` and `tier` are REQUIRED with no
  default, because a default is the server choosing (D5).
- **The delegate declares.** At link time the claimant's `capabilities.model` / `.tier`
  are copied beside what was requested. A mismatch is a row, never a refusal (D8).

Linking is lineage-only (D7): a claim by anyone but a declared child of the delegator
closes the delegation as `superseded`. A child is declared two ways, and the row says
which (`linked_by`): `parent`, it registered with `parent_agent_id` because it runs inside
the delegator's turn; or `seat`, it registered on an enrolment the delegator minted, which
is how a SPAWNED process is a child — `register_agent` tells a spawned process not to
declare a parent, because that field feeds review independence and a process is
independent. A stranger's claim is not evidence the child arrived, and a record that said
`claimed` would hide the parent's silence.

States (D9): `open`, `claimed`, `finished`, `expired`, `closed`. `open`/`claimed`/`expired`
are derived from the row and the clock. `closed` and `finished` are STORED, at the event
that produced them (a withdrawal, a stranger's claim, a bounce, a sign-off): a historical
attempt's outcome cannot be reproduced from the item's current state once a later attempt
has moved it, and an `attempts` history that re-derived every row from today's item would
say "signed off" about the attempt that bounced.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, Delegation, Enrolment, Item, MemoryShard
logger = logging.getLogger(__name__)

LANES = ("frontend", "backend", "mixed")
TIERS = ("cheap", "frontier")
STATES = ("open", "claimed", "finished", "expired", "closed")
BASES = ("none", "bounced", "blocked", "released", "previous")
CLOSE_REASONS = ("withdrawn", "superseded")
OUTCOMES = ("signed_off", "bounced", "blocked", "released")
CHECKLISTS = ("mcp_tool", "migration", "frontend", "docs")
UNDECLARED = "undeclared"

#: D17: the brief's caps. Response size never touches the manifest ceiling.
SUMMARY_MAX = 600
LESSONS_MAX = 5
NOTE_MAX = 200
#: D19: the board carries everything open plus this many closed/finished/expired per
#: delegator, inside the feed's retention window. Older history is on the item.
BOARD_CLOSED_MAX = 10


class DelegationRefused(Exception):
    """A `delegate` call the server will not write. `code` is the MCP error class."""

    def __init__(self, message: str, *, code: str = "conflict", hint: str | None = None,
                 detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.detail = detail or {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---- the suggestions (D5, D6) ---------------------------------------------------------------

def _is_web(path: str) -> bool:
    p = path.strip().lstrip("./")
    return p == "web" or p.startswith("web/")


def lane_for(touchpoints: list | None) -> dict:
    """Lane from touchpoints only (D6): `web/**` alone is `frontend`; anything else is
    `backend`; both is `mixed`. `basis` names the paths that decided it — for `mixed`, all
    of them, because either half alone would have given a different answer."""
    paths = [t for t in (touchpoints or []) if isinstance(t, str) and t.strip()]
    web = [p for p in paths if _is_web(p)]
    other = [p for p in paths if not _is_web(p)]
    if web and other:
        return {"value": "mixed", "basis": web + other}
    if web:
        return {"value": "frontend", "basis": web}
    return {"value": "backend", "basis": other}


def checklist_for(touchpoints: list | None) -> str | None:
    """Which AGENTS.md task class the item falls under, from its touchpoints. None when
    nothing matches — the brief says so rather than guessing a class."""
    paths = [t for t in (touchpoints or []) if isinstance(t, str)]
    if any("alembic/versions" in p or "models/__init__" in p for p in paths):
        return "migration"
    if any(p.endswith("mcp_server.py") for p in paths):
        return "mcp_tool"
    if paths and all(_is_web(p) for p in paths):
        return "frontend"
    if paths and all(p.endswith(".md") or p.startswith("docs/") for p in paths):
        return "docs"
    return None


def tier_for(previous: Delegation | None) -> dict:
    """The suggestion, with its evidence. `none` when there is no history — cheap first.
    A prior attempt that ended in a bounce, a block or a lost lease suggests one tier up.
    Any other prior attempt (withdrawn, superseded, expired) suggests what was asked last
    time, because nothing was learned from it."""
    if previous is None:
        return {"value": "cheap", "basis": "none"}
    if previous.outcome in ("bounced", "blocked", "released"):
        return {"value": "frontier", "basis": previous.outcome}
    return {"value": previous.requested_tier, "basis": "previous"}


# ---- state (D9) ----------------------------------------------------------------------------

def state(row: Delegation, *, now: datetime | None = None) -> str:
    if row.closed_reason:
        return "closed"
    if row.outcome:
        return "finished"
    if row.agent_id:
        return "claimed"
    now = now or _now()
    created = _aware(row.created_at) or now
    if created + timedelta(seconds=int(row.lease_seconds or 0)) <= now:
        return "expired"
    return "open"


def _is_open(row: Delegation, *, now: datetime) -> bool:
    return state(row, now=now) == "open"


def _unlinked(row: Delegation) -> bool:
    """Open or expired: nothing has claimed it and nothing has closed it."""
    return not row.agent_id and not row.closed_reason and not row.outcome


def _declared(agent: Agent | None) -> tuple[str | None, str | None]:
    caps = (agent.capabilities or {}) if agent is not None else {}
    model = caps.get("model")
    tier = caps.get("tier")
    return (model if isinstance(model, str) and model else None,
            tier if isinstance(tier, str) and tier else None)


def row_dict(row: Delegation, *, item_key: str | None = None,
             now: datetime | None = None) -> dict:
    now = now or _now()
    st = state(row, now=now)
    created = _aware(row.created_at)
    declared_tier = row.declared_tier
    if row.agent_id and not declared_tier:
        declared_tier = UNDECLARED
    out: dict[str, Any] = {
        "id": row.id,
        "item": item_key or row.item_id,
        "state": st,
        "lane": row.lane,
        "requested_tier": row.requested_tier,
        "declared_tier": declared_tier,
        "declared_model": row.declared_model,
        # Only a declared tier can match or mismatch; `undeclared` is neither (D8).
        "mismatch": bool(row.agent_id and row.declared_tier
                         and row.declared_tier != row.requested_tier),
        "delegated_by": row.delegated_by,
        "agent_id": row.agent_id,
        "linked_by": row.linked_by,
        "outcome": row.outcome,
        "closed_reason": row.closed_reason,
        "closed_by": row.closed_by,
        "note": row.note or "",
        "created_at": created.isoformat() if created else None,
        "claimed_at": _aware(row.claimed_at).isoformat() if row.claimed_at else None,
        "age_seconds": max(0, int((now - created).total_seconds())) if created else None,
    }
    return out


# ---- the brief (D2) ------------------------------------------------------------------------

def _summary(description: str) -> str:
    text = (description or "").strip()
    first = text.split("\n\n", 1)[0].strip()
    return first[:SUMMARY_MAX]


def attempts_for(db: Session, item: Item) -> list[Delegation]:
    """Every delegation on the item that is no longer open, oldest first."""
    rows = db.scalars(select(Delegation).where(Delegation.item_id == item.id)
                      .order_by(Delegation.created_at, Delegation.id)).all()
    now = _now()
    return [r for r in rows if not _is_open(r, now=now)]


def _attempt(row: Delegation) -> dict:
    return {
        "requested_tier": row.requested_tier,
        "declared_model": row.declared_model,
        "declared_tier": (row.declared_tier or (UNDECLARED if row.agent_id else None)),
        "outcome": row.outcome,
        "state": state(row),
    }


def _pinned(item: Item) -> dict | None:
    from app.services import fleet as fleet_svc

    holder = fleet_svc.bounce_pin_holder(item)
    if holder is None:
        return None
    until = _aware(item.bounce_pinned_until)
    return {"to": holder, "until": until.isoformat() if until else None}


def _text(item: Item, *, summary: str, touchpoints: list[str], blocked_by: list[str],
          checklist: str | None, lessons: list[dict], previous: dict | None,
          attempts: list[dict]) -> str:
    """Prose for a spawn prompt, derived from the fields and nothing else (D16). Deliberately
    excludes the lane and tier suggestions so a pasted brief cannot become the default."""
    lines = [f"Item {item.key}: {item.title}"]
    if summary:
        lines.append(summary)
    if touchpoints:
        lines.append("Touchpoints: " + ", ".join(touchpoints))
    else:
        lines.append("Touchpoints: none recorded")
    if blocked_by:
        lines.append("Blocked by: " + ", ".join(blocked_by))
    if checklist:
        lines.append(f"Task class: {checklist} (follow that checklist in AGENTS.md)")
    if lessons:
        lines.append("Lessons: " + " | ".join(f"{l['id']}: {l['text']}" for l in lessons))
    if previous:
        prev = (f"Previous attempt: requested {previous['requested_tier']}, "
                f"declared {previous['declared_model'] or UNDECLARED}, "
                f"outcome {previous['outcome'] or previous['state']}")
        if previous.get("bounce_reason"):
            prev += f" — {previous['bounce_reason']}"
        lines.append(prev)
    if len(attempts) > 1:
        lines.append(f"Attempts so far: {len(attempts)}")
    return "\n".join(lines)


def brief(db: Session, item: Item, *, user_id: str | None = None) -> dict:
    from app.services import fleet_profiles
    from app.services import items as items_svc
    from app.services import prioritization

    touchpoints = [t for t in (item.touchpoints or []) if isinstance(t, str) and t.strip()]
    ctx = prioritization.context(db, item.project_id)
    blocked_by = [ctx.by_id[d].key for d in prioritization.blocked_by(ctx, item)]
    shards = db.scalars(select(MemoryShard).where(MemoryShard.item_id == item.id)
                        .order_by(MemoryShard.id).limit(LESSONS_MAX)).all()
    lessons = [{"id": s.id, "text": s.text} for s in shards]
    history = attempts_for(db, item)
    prev_row = history[-1] if history else None
    previous = None
    if prev_row is not None:
        previous = {**_attempt(prev_row),
                    "bounce_reason": item.bounce_reason if prev_row.outcome == "bounced" else ""}
    attempts = [_attempt(r) for r in history]
    summary = _summary(item.description)
    checklist = checklist_for(touchpoints)
    return {
        "item": item.key,
        "title": item.title,
        "summary": summary,
        "touchpoints": touchpoints,
        "blocked_by": blocked_by,
        "ready": bool(items_svc.claimable(item)) and prioritization.ready(ctx, item),
        "checklist": checklist,
        "lessons": lessons,
        "lane": lane_for(touchpoints),
        "tier": tier_for(prev_row),
        "previous": previous,
        "attempts": attempts,
        "pinned": _pinned(item),
        "text": _text(item, summary=summary, touchpoints=touchpoints, blocked_by=blocked_by,
                      checklist=checklist, lessons=lessons, previous=previous,
                      attempts=attempts),
        # PRD-37 D9: the caller's profile and the project's policy, for the supervisor that
        # resolves the tier. NOT in `text` — the spawn text carries no suggestion (PRD-35 D5).
        **fleet_profiles.attach(db, {}, user_id=user_id, project_id=item.project_id),
    }


# ---- PRD-37 D7: the measured axes, with their sample sizes ------------------------------------

#: Seconds from claim to finish at which the latency axis reads 0. An hour is the PRD-36
#: child wall-clock default; a child that takes that long scored nothing on speed.
LATENCY_FLOOR_S = 3600


def measured(db: Session, project_id: str | None) -> list[dict]:
    """What finished delegations say about each vendor x model x lane x requested tier.

    `quality` is signed-off over finished attempts; `latency` is the median claim-to-finish
    time folded onto 0-1 (instant 1.0, `LATENCY_FLOOR_S` or slower 0.0). Both carry `n`, and
    the READER decides whether `n` is enough (gbfleet's MIN_SAMPLE) - this side states counts,
    never verdicts. Grouped per lane and per tier requested, NEVER pooled (PRD-35 named the
    bias: frontier only sees what cheap failed), so a row here is one cell, and an absent cell
    is unmeasured rather than zero.

    The vendor is the child's declared `capabilities.vendor` (what drives review diversity),
    the model its declared `capabilities.model` - the same fields the delegation record copies
    at link time (D8). A child that declared neither is counted under `undeclared`, which the
    matrix will not match and the doctor will show.
    """
    stmt = select(Delegation).where(Delegation.outcome.is_not(None))
    if project_id:
        stmt = stmt.where(Delegation.project_id == project_id)
    rows = db.scalars(stmt).all()
    cells: dict[tuple[str, str, str, str], dict] = {}
    agents: dict[str | None, Agent | None] = {}
    for row in rows:
        if row.agent_id not in agents:
            agents[row.agent_id] = db.get(Agent, row.agent_id) if row.agent_id else None
        agent = agents[row.agent_id]
        caps = (agent.capabilities or {}) if agent is not None else {}
        vendor = caps.get("vendor") if isinstance(caps.get("vendor"), str) and caps.get("vendor") else UNDECLARED
        model = row.declared_model or UNDECLARED
        key = (vendor, model, row.lane, row.requested_tier)
        cell = cells.setdefault(key, {"finished": 0, "signed_off": 0, "durations": []})
        cell["finished"] += 1
        cell["signed_off"] += 1 if row.outcome == "signed_off" else 0
        claimed, finished = _aware(row.claimed_at), _aware(row.finished_at)
        if claimed and finished and finished >= claimed:
            cell["durations"].append((finished - claimed).total_seconds())
    out = []
    for (vendor, model, lane, tier), cell in sorted(cells.items()):
        durations = sorted(cell["durations"])
        latency = None
        if durations:
            mid = len(durations) // 2
            median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
            latency = {"value": round(max(0.0, min(1.0, 1.0 - median / LATENCY_FLOOR_S)), 3),
                       "n": len(durations), "median_seconds": round(median, 1)}
        out.append({
            "vendor": vendor, "model": model, "lane": lane, "tier": tier,
            "quality": {"value": round(cell["signed_off"] / cell["finished"], 3), "n": cell["finished"]},
            "latency": latency,
        })
    return out


# ---- the write (D3, D14, D15) ---------------------------------------------------------------

def _open_rows(db: Session, item: Item) -> list[Delegation]:
    rows = db.scalars(select(Delegation).where(Delegation.item_id == item.id)).all()
    return [r for r in rows if _unlinked(r)]


def delegate(db: Session, *, agent: Agent, item: Item, lane: str, tier: str,
             note: str = "", lease_seconds: int, seat: bool = False, api_key=None,
             wave: str | None = None) -> tuple[Delegation, str | None, str | None]:
    """Write what the delegator asked for. Claims nothing, spawns nothing. Returns the new
    row, the id of the caller's own open delegation it withdrew (PRD-35 D14), and the bound
    seat's enrolment code when `seat` was asked for (PRD-36 D2), else None.

    A bound seat is refused, before anything is written, when the item's touch areas are
    reserved by someone else (PRD-36 D13): a steered claim bypasses the divvy, so the
    collision check the divvy would have made happens here and names the holder.
    """
    from app.services import fleet as fleet_svc
    from app.services import items as items_svc

    if lane not in LANES:
        raise DelegationRefused(f"lane must be one of {list(LANES)}", code="validation")
    if tier not in TIERS:
        raise DelegationRefused(f"tier must be one of {list(TIERS)}", code="validation")
    if item.blocker:
        raise DelegationRefused(f"{item.key} is blocked: {item.blocker}",
                                hint="clear the blocker before delegating")
    if item.claimed_by == agent.id:
        raise DelegationRefused(f"you hold {item.key}; release it or build it yourself",
                                hint="a delegation is for work you are not holding")
    if not items_svc.claimable(item, lease_seconds=lease_seconds):
        raise DelegationRefused(
            f"{item.key} is not ready: status {item.status}"
            + (f", held by {item.claimed_by}" if item.claimed_by else ""),
            hint="only a claimable item can be delegated")
    holder = fleet_svc.bounce_pin_holder(item)
    if holder is not None and holder != agent.id:
        if lineage(db, db.get(Agent, holder), agent.id) is None:
            until = _aware(item.bounce_pinned_until)
            raise DelegationRefused(
                f"{item.key} is pinned to its author {holder} after a bounce",
                hint="wait for the pin to lapse, or let the author retry",
                detail={"pinned_to": holder, "pinned_until": until.isoformat() if until else None})
    now = _now()
    holders: list[str] = []
    if seat:
        from app.services import collision as collision_svc

        # The mint gate is the mint gate: whoever may call mint_enrolment may bind a seat.
        fleet_svc.check_tool_role(db, tool="mint_enrolment", api_key=api_key, agent_id=agent.id)
        areas, _ = collision_svc.touch_areas(db, item, item.project_id)
        taken = fleet_svc.active_reservations(db, item.project_id, now=now)
        blocked = [r.area for r in taken if r.agent_id != agent.id]
        if fleet_svc.areas_collide(areas, blocked):
            holders = sorted({r.agent_id for r in taken
                              if r.agent_id != agent.id
                              and fleet_svc.areas_collide(areas, [r.area])})
            raise DelegationRefused(
                f"{item.key}'s areas are reserved by {', '.join(holders)}; a bound seat "
                "would claim straight through that collision",
                hint="delegate another item, or delegate without a seat and let the divvy decide",
                detail={"held_by": ", ".join(holders)})
    withdrew: str | None = None
    for row in _open_rows(db, item):
        if row.delegated_by == agent.id:
            # D14: the owner withdraws by re-delegating. Nothing else can know its child died.
            row.closed_reason = "withdrawn"
            row.closed_at = now
            withdrew = row.id
            continue
        if _is_open(row, now=now):
            age = int((now - (_aware(row.created_at) or now)).total_seconds())
            raise DelegationRefused(
                f"{item.key} already has an open delegation ({row.id}) from {row.delegated_by}, "
                f"{age}s old",
                hint="wait for it to expire; only its owner can withdraw it",
                detail={"delegation_id": row.id, "delegated_by": row.delegated_by,
                        "age_seconds": age})
        # An expired one is left as it is: it stays the record of the spawn that never came.
    row = Delegation(
        id=_new_id(),
        project_id=item.project_id, item_id=item.id, delegated_by=agent.id,
        lane=lane, requested_tier=tier, note=(note or "")[:NOTE_MAX],
        created_at=now, lease_seconds=int(lease_seconds),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    code: str | None = None
    if seat:
        _, code = fleet_svc.mint_enrolment_as(
            db, minter_id=agent.id, project_id=item.project_id, role="worker",
            api_key=api_key, wave=wave, item_id=item.id, delegation_id=row.id)
    return row, withdrew, code


def _new_id() -> str:
    import uuid

    return "dlg_" + uuid.uuid4().hex[:12]


# ---- the link (D7, D8) ----------------------------------------------------------------------

def lineage(db: Session, claimant: Agent | None, delegator_id: str) -> str | None:
    """How `claimant` is a child of `delegator_id`, or None. `parent` outranks `seat` only
    in the sense that it is checked first; both are the delegator's own declaration."""
    if claimant is None:
        return None
    if claimant.parent_agent_id == delegator_id:
        return "parent"
    if claimant.enrolment_id:
        seat = db.get(Enrolment, claimant.enrolment_id)
        if seat is not None and seat.minted_by == delegator_id:
            return "seat"
    return None


def on_claim(db: Session, item: Item, claimant_id: str) -> None:
    """Called after every successful claim, on all four paths (`_try_claim` is the one write
    point). Links a declared child; supersedes anyone else. Also ends any earlier linked
    attempt on this item whose lease was lost: the item is in someone else's hands now.

    Swallows: a delegation write must never fail the claim it describes.
    """
    try:
        now = _now()
        claimant = db.get(Agent, claimant_id)
        rows = db.scalars(select(Delegation).where(Delegation.item_id == item.id)).all()
        changed = False
        for row in rows:
            if _unlinked(row):
                how = lineage(db, claimant, row.delegated_by)
                if how:
                    model, tier = _declared(claimant)
                    row.agent_id = claimant_id
                    row.linked_by = how
                    row.declared_model = model
                    row.declared_tier = tier
                    row.claimed_at = now
                else:
                    row.closed_reason = "superseded"
                    row.closed_by = claimant_id
                    row.closed_at = now
                changed = True
            elif row.agent_id and row.agent_id != claimant_id and not row.outcome \
                    and not row.closed_reason:
                row.outcome = "released"
                row.finished_at = now
                changed = True
        if changed:
            db.commit()
    except Exception:  # noqa: BLE001 — never fail the claim
        logger.exception("delegation: on_claim failed for %s", item.id)
        db.rollback()


def on_outcome(db: Session, item: Item, outcome: str) -> None:
    """Record how a linked attempt ended (D9). Flushes; the caller's transaction commits."""
    if outcome not in OUTCOMES:
        return
    try:
        now = _now()
        rows = db.scalars(select(Delegation).where(Delegation.item_id == item.id)).all()
        for row in rows:
            if row.agent_id and not row.outcome and not row.closed_reason:
                row.outcome = outcome
                row.finished_at = now
        db.flush()
    except Exception:  # noqa: BLE001 — never fail the transition
        logger.exception("delegation: on_outcome(%s) failed for %s", outcome, item.id)


# ---- the board (D11, D19) ------------------------------------------------------------------

def for_board(db: Session, project_id: str, *, now: datetime | None = None,
              retention_days: int | None = None) -> dict[str, dict]:
    """Per delegator: counts per state, the oldest open age, and a bounded row list.
    ONE query for the whole board. Agents with nothing are ABSENT — the board renders that
    as `delegations: null`, never `[]`."""
    now = now or _now()
    stmt = select(Delegation).where(Delegation.project_id == project_id)
    if retention_days is not None and retention_days > 0:
        cutoff = now - timedelta(days=retention_days)
        stmt = stmt.where(Delegation.created_at >= cutoff)
    rows = db.scalars(stmt.order_by(Delegation.created_at.desc(), Delegation.id.desc())).all()
    if not rows:
        return {}
    item_ids = {r.item_id for r in rows}
    items = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(item_ids))).all()}
    out: dict[str, dict] = {}
    for row in rows:
        g = out.setdefault(row.delegated_by, {
            **{s: 0 for s in STATES}, "oldest_open_seconds": None, "rows": [], "_closed": 0,
        })
        d = row_dict(row, item_key=(items[row.item_id].key if row.item_id in items else None),
                     now=now)
        g[d["state"]] += 1
        if d["state"] == "open":
            age = d["age_seconds"] or 0
            if g["oldest_open_seconds"] is None or age > g["oldest_open_seconds"]:
                g["oldest_open_seconds"] = age
            g["rows"].append(d)
        else:
            if g["_closed"] < BOARD_CLOSED_MAX:
                g["rows"].append(d)
            g["_closed"] += 1
    for g in out.values():
        g.pop("_closed", None)
    return out
