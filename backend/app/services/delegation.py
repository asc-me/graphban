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

from app.models import Agent, Delegation, Enrolment, Item, MemoryShard, Project
from app.services import harness as harness_svc
from app.services.harness import WINDOW_DAYS
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
#: PRD-38 D10 / PRD-41 D15: `measured_for_lane` plus `capabilities` stay inside this
#: serialised bound. A result payload that grows quietly costs every spawn's context.
MEASURED_FOR_LANE_MAX = 4
BRIEF_MEASURED_BOUND = 400
LAYERS = ("project", "org", "platform", "prior")
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
        "capabilities_at_delegate": list(row.capabilities_at_delegate or []) or None,
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
    from app.services import reach as reach_svc

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
    caps = capabilities_of(item)
    spend = item_spend(db, item.id)
    project = db.get(Project, item.project_id) if item.project_id else None
    policy = (project.fleet_policy or {}) if project is not None else {}
    policy_caps = policy.get("caps") if isinstance(policy, dict) else None
    if isinstance(policy_caps, dict) and policy_caps.get("period"):
        spend.update(period_spend(db, item.project_id, str(policy_caps["period"])))
    measured_lane = measured_for_lane(db, item, caps)
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
        # GRPH-832. Beside `lane` and `checklist` because it is the same kind of answer — a
        # reading of the item with the evidence that produced it — and a planner choosing what
        # to hand out reads all three in one place.
        "reach": reach_svc.describe(item.reach or reach_svc.REPO,
                                    reach_svc.signals(item.description)),
        "tier": tier_for(prev_row),
        "previous": previous,
        "attempts": attempts,
        "pinned": _pinned(item),
        "text": _text(item, summary=summary, touchpoints=touchpoints, blocked_by=blocked_by,
                      checklist=checklist, lessons=lessons, previous=previous,
                      attempts=attempts),
        # PRD-41 D5 / D15: the set the resolver will score, inside the measured_for_lane cap.
        "capabilities": caps,
        "measured_for_lane": measured_lane,
        "spend": spend,
        # PRD-37 D9: the caller's profile and the project's policy, for the supervisor that
        # resolves the tier. NOT in `text` — the spawn text carries no suggestion (PRD-35 D5).
        **fleet_profiles.attach(db, {}, user_id=user_id, project_id=item.project_id),
    }


def capabilities_of(item: Item | None) -> list[str]:
    """Touchpoints only — what `delegate` can know before there is a diff (D5)."""
    return list(harness_svc.capabilities(item))


# ---- PRD-37 D7 / PRD-41 D5: the measured axes, re-keyed on capability ----------------------

#: Seconds from claim to finish at which the latency axis reads 0. An hour is the PRD-36
#: child wall-clock default; a child that takes that long scored nothing on speed.
LATENCY_FLOOR_S = 3600


def item_spend(db: Session, item_id: str | None) -> dict:
    """Reported tokens on this item, for `caps.per_item_tokens`. Unreported is a count,
    never a zero spend."""
    from app.models import AttemptTelemetry

    if not item_id:
        return {"item_tokens": 0, "item_reported": 0, "item_finished": 0}
    rows = db.scalars(select(AttemptTelemetry).where(AttemptTelemetry.item_id == item_id)).all()
    tokens = reported = finished = 0
    for row in rows:
        if row.outcome is None and row.derived_at is None:
            continue
        finished += 1
        if row.tokens_in is not None or row.tokens_out is not None:
            reported += 1
            tokens += int(row.tokens_in or 0) + int(row.tokens_out or 0)
    return {"item_tokens": tokens, "item_reported": reported, "item_finished": finished}


#: D20 period lengths. A month is 30 days, not a calendar month — the cap is a budget,
#: not an accounting period, and a moving window is what the resolver can enforce.
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}


def period_spend(db: Session, project_id: str | None, period: str | None) -> dict:
    """Reported tokens on the project in the cap's window, for `caps.per_period_tokens`."""
    from app.models import AttemptTelemetry

    days = PERIOD_DAYS.get(period or "")
    if not project_id or not days:
        return {"period_tokens": 0, "period_reported": 0, "period_finished": 0}
    cutoff = _now() - timedelta(days=days)
    rows = db.scalars(select(AttemptTelemetry).where(
        AttemptTelemetry.project_id == project_id)).all()
    tokens = reported = finished = 0
    for row in rows:
        when = _aware(row.derived_at) or _aware(row.reported_at)
        if when is None or when < cutoff:
            continue
        if row.outcome is None and row.derived_at is None:
            continue
        finished += 1
        if row.tokens_in is not None or row.tokens_out is not None:
            reported += 1
            tokens += int(row.tokens_in or 0) + int(row.tokens_out or 0)
    return {"period_tokens": tokens, "period_reported": reported, "period_finished": finished}


def _n_from_band(band: str | None) -> int:
    """The lower bound of a snapshot n-band, so the floor check has a number."""
    text = (band or "").replace("–", "-").replace("+", "")
    for part in text.replace("<", "").split("-"):
        digits = "".join(c for c in part if c.isdigit())
        if digits:
            return int(digits)
    return 0


def _cell_out(vendor: str, model: str, capability: str, layer: str, cell: dict) -> dict:
    durations = sorted(cell.get("durations") or [])
    latency = None
    if durations:
        mid = len(durations) // 2
        median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
        latency = {"value": round(max(0.0, min(1.0, 1.0 - median / LATENCY_FLOOR_S)), 3),
                   "n": len(durations), "median_seconds": round(median, 1)}
    finished = cell["finished"]
    reported = cell.get("tokens_reported") or 0
    signed_off_reported = cell.get("signed_off_reported") or 0
    tokens_in = cell.get("tokens_in") or 0
    tokens_out = cell.get("tokens_out") or 0
    coverage = (reported / finished) if finished else 0.0
    if reported and coverage >= harness_svc.COST_COVERAGE and signed_off_reported:
        cost = {"comparable": True, "reported": reported, "finished": finished,
                "tokens_to_signoff": round((tokens_in + tokens_out) / signed_off_reported, 1),
                "tokens_in": tokens_in, "tokens_out": tokens_out}
    else:
        cost = {"comparable": False, "reported": reported, "finished": finished,
                "reason": f"not comparable: {reported} of {finished} attempts reported tokens"}
    out = {
        "vendor": vendor, "model": model, "capability": capability, "layer": layer,
        "quality": {"value": round(cell["signed_off"] / finished, 3) if finished else 0.0,
                    "n": finished},
        "latency": latency,
        "bands": {name: {"value": round(v["signed_off"] / v["n"], 3), "n": v["n"]}
                  for name, v in sorted((cell.get("bands") or {}).items())},
        "cost": cost,
    }
    if cell.get("binary_version"):
        out["binary_version"] = cell["binary_version"]
    if cell.get("inherited_from"):
        out["inherited_from"] = cell["inherited_from"]
    if cell.get("n_band"):
        out["n_band"] = cell["n_band"]
    return out


def _add_attempt(cell: dict, *, signed_off: bool, band: str, duration: float | None,
                 tokens_in: int | None, tokens_out: int | None) -> None:
    cell["finished"] += 1
    cell["signed_off"] += 1 if signed_off else 0
    b = cell["bands"].setdefault(band, {"n": 0, "signed_off": 0})
    b["n"] += 1
    b["signed_off"] += 1 if signed_off else 0
    if duration is not None:
        cell["durations"].append(duration)
    if tokens_in is not None or tokens_out is not None:
        cell["tokens_reported"] += 1
        cell["tokens_in"] += int(tokens_in or 0)
        cell["tokens_out"] += int(tokens_out or 0)
        if signed_off:
            cell["signed_off_reported"] += 1


def _empty_cell() -> dict:
    return {"finished": 0, "signed_off": 0, "durations": [], "bands": {},
            "tokens_in": 0, "tokens_out": 0, "tokens_reported": 0, "signed_off_reported": 0}


def measured(db: Session, project_id: str | None, *, window_days: int | None = None) -> list[dict]:
    """What finished attempts say, keyed on vendor × model × binary_version × capability.

    PRD-41 D3/D5: lane and tier left the key; binary_version stayed (criterion 25). Each
    cell names which layer produced it (`project | org | platform | prior`) so a score
    assembled from four sources cannot hide which one decided. `bands` stay inside the
    cell (PRD-38 D9). Cost is tokens to a signed-off outcome, bounced included,
    suppressed below 80% reporting (D16).

    The project layer is aggregated live from finished delegations so a test that writes
    a row sees it without waiting on a roll. Probe attempts (`sampled=probe`) are
    excluded: D7 / criterion 27 — a probe cell under n=5 feeds no D5 layer, and
    pooling them with natural traffic is how a page split still lied to the resolver.
    Org and platform layers read rollup tables when they exist; an instance with
    neither emits project cells only. A declared version change keeps the previous
    version's cell and emits it again as `prior` labelled with the new version —
    pooling versions into one cell is the sabotage.
    """
    from app.models import (AttemptTelemetry, CapabilityPrior, HarnessRollup,
                            PlatformRollup, Project)

    cutoff = _now() - timedelta(days=WINDOW_DAYS if window_days is None else window_days)
    stmt = select(Delegation).where(Delegation.outcome.is_not(None))
    if project_id:
        stmt = stmt.where(Delegation.project_id == project_id)
    rows = [r for r in db.scalars(stmt).all() if (_aware(r.finished_at) or cutoff) >= cutoff]
    cells: dict[tuple[str, str, str, str], dict] = {}
    agents: dict[str | None, Agent | None] = {}
    items: dict[str | None, Item | None] = {}
    telemetry: dict[str, Any] = {}
    tel_rows = []
    if rows:
        tel_rows = db.scalars(select(AttemptTelemetry).where(
            AttemptTelemetry.delegation_id.in_([r.id for r in rows]))).all()
    for t in tel_rows:
        if t.delegation_id:
            telemetry[t.delegation_id] = t
    versions: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        if row.agent_id not in agents:
            agents[row.agent_id] = db.get(Agent, row.agent_id) if row.agent_id else None
        agent = agents[row.agent_id]
        declared = (agent.capabilities or {}) if agent is not None else {}
        vendor = declared.get("vendor") if isinstance(declared.get("vendor"), str) and declared.get("vendor") else UNDECLARED
        model = row.declared_model or ("" if vendor != UNDECLARED else UNDECLARED)
        if row.item_id not in items:
            items[row.item_id] = db.get(Item, row.item_id) if row.item_id else None
        item = items[row.item_id]
        tel = telemetry.get(row.id)
        if tel is not None and tel.sampled == "probe":
            continue
        cap_list = list(tel.capabilities or []) if tel is not None else capabilities_of(item)
        if not cap_list:
            cap_list = [harness_svc.FAMILY_OTHER]
        band = harness_svc.size_band(item)
        claimed, finished_at = _aware(row.claimed_at), _aware(row.finished_at)
        duration = ((finished_at - claimed).total_seconds()
                    if claimed and finished_at and finished_at >= claimed else None)
        tin = tel.tokens_in if tel is not None else None
        tout = tel.tokens_out if tel is not None else None
        version = (tel.binary_version if tel is not None else None) or ""
        if version:
            versions.setdefault((vendor, model), [])
            if version not in versions[(vendor, model)]:
                versions[(vendor, model)].append(version)
        signed = row.outcome == "signed_off"
        for cap in cap_list:
            key = (vendor, model, version, cap)
            cell = cells.setdefault(key, _empty_cell())
            _add_attempt(cell, signed_off=signed, band=band, duration=duration,
                         tokens_in=tin, tokens_out=tout)
            if version:
                cell["binary_version"] = version
    out = [_cell_out(v, m, c, "project", cell)
           for (v, m, _ver, c), cell in sorted(cells.items())]

    # Org layer: other projects in the same org, from rollups. Absent when there is no org
    # or no sibling traffic — not a zero.
    if project_id:
        project = db.get(Project, project_id)
        org_id = getattr(project, "org_id", None) if project is not None else None
        if org_id:
            siblings = [p.id for p in db.scalars(select(Project).where(
                Project.org_id == org_id, Project.id != project_id)).all()]
            if siblings:
                week_cut = harness_svc.week_of(cutoff)
                org_cells: dict[tuple[str, str, str], dict] = {}
                for roll in db.scalars(select(HarnessRollup).where(
                        HarnessRollup.project_id.in_(siblings))).all():
                    if roll.week < week_cut:
                        continue
                    key = (roll.vendor, roll.model, roll.capability)
                    cell = org_cells.setdefault(key, _empty_cell())
                    natural = roll.finished - (getattr(roll, "probe", 0) or 0)
                    natural_n = max(natural, 0)
                    cell["finished"] += natural_n
                    cell["signed_off"] += min(roll.signed_off, natural_n)
                    cell["tokens_in"] += roll.tokens_in or 0
                    cell["tokens_out"] += roll.tokens_out or 0
                    cell["tokens_reported"] += roll.tokens_reported
                    cell["signed_off_reported"] += roll.signed_off_reported or 0
                    b = cell["bands"].setdefault(roll.size_band, {"n": 0, "signed_off": 0})
                    b["n"] += natural_n
                    b["signed_off"] += min(roll.signed_off, natural_n)
                    if roll.median_seconds is not None:
                        cell["durations"].extend([float(roll.median_seconds)] * max(roll.finished, 1))
                out.extend(_cell_out(v, m, c, "org", cell)
                           for (v, m, c), cell in sorted(org_cells.items()) if cell["finished"])

    week_cut = harness_svc.week_of(cutoff)
    plat_cells: dict[tuple[str, str, str], dict] = {}
    for roll in db.scalars(select(PlatformRollup).where(PlatformRollup.week >= week_cut)).all():
        key = (roll.vendor, roll.model, roll.capability)
        cell = plat_cells.setdefault(key, _empty_cell())
        cell["finished"] += roll.finished
        cell["signed_off"] += roll.signed_off
        b = cell["bands"].setdefault(roll.size_band, {"n": 0, "signed_off": 0})
        b["n"] += roll.finished
        b["signed_off"] += roll.signed_off
    out.extend(_cell_out(v, m, c, "platform", cell)
               for (v, m, c), cell in sorted(plat_cells.items()) if cell["finished"])

    # Fetched snapshot (self-hosted): D5's platform layer when this instance has no
    # in-process overlay. A local cell at the floor already sits in `out` as project
    # and outranks these.
    if not plat_cells:
        for prior in db.scalars(select(CapabilityPrior)).all():
            n = _n_from_band(prior.n_band)
            cell = _empty_cell()
            cell["finished"] = n
            cell["signed_off"] = int(round((prior.rate or 0.0) * n)) if n else 0
            item = _cell_out(prior.vendor, prior.model, prior.capability, "platform", cell)
            item["n_band"] = prior.n_band
            item["snapshot_at"] = prior.snapshot_at
            item["source"] = prior.source
            if prior.binary_version:
                item["binary_version"] = prior.binary_version
            out.append(item)

    # Inherited prior: a newer binary_version with no cell of its own yet still has the
    # previous version's rate, labelled so it cannot be mistaken for a measurement of the
    # new binary (criterion 25).
    for (vendor, model), seen in versions.items():
        if len(seen) < 2:
            continue
        # The last-seen version on a live row is "current"; anything else is previous.
        current = seen[-1]
        previous = seen[-2]
        for cell in list(out):
            if (cell["vendor"], cell["model"], cell["layer"]) != (vendor, model, "project"):
                continue
            if cell.get("binary_version") == previous:
                inherited = dict(cell)
                inherited["layer"] = "prior"
                inherited["inherited_from"] = previous
                inherited["binary_version"] = current
                out.append(inherited)

    return out


def measured_for_lane(db: Session, item: Item, caps: list[str]) -> list[dict]:
    """At most four compact cells for the item's capabilities, inside BRIEF_MEASURED_BOUND.

    Re-keyed on capability (D15). A below-floor cell appears only when no above-floor cell
    exists for that capability. Dropped from the end if the serialised payload would
    overflow the bound — the bound is the feature, not a hint.
    """
    import json

    wanted = [c for c in caps if c][:MEASURED_FOR_LANE_MAX]
    if not wanted:
        return []
    cells = [c for c in measured(db, item.project_id) if c.get("layer") == "project"
             and c.get("capability") in wanted]
    by_cap: dict[str, dict] = {}
    for cell in cells:
        cap = cell["capability"]
        prev = by_cap.get(cap)
        if prev is None or cell["quality"]["n"] > prev["quality"]["n"]:
            by_cap[cap] = cell
    compact = []
    for cap in wanted:
        cell = by_cap.get(cap)
        if cell is None:
            continue
        n = cell["quality"]["n"]
        compact.append({
            "vendor": cell["vendor"], "model": cell["model"], "capability": cap,
            "signed_off": round(cell["quality"]["value"] * n), "n": n,
            "below_floor": n < harness_svc.FLOOR,
        })
        if len(compact) >= MEASURED_FOR_LANE_MAX:
            break
    while compact and len(json.dumps({"capabilities": wanted, "measured_for_lane": compact},
                                     separators=(",", ":")) ) > BRIEF_MEASURED_BOUND:
        compact.pop()
    return compact


def probe_suggestions(db: Session, project_id: str | None) -> list[dict]:
    """A declared vendor/model/binary_version that is new, or newly versioned, is a probe
    suggestion. A supervisor-side release that leaves those three unchanged is not
    (criterion 25). Nothing here starts a probe — S3 does that."""
    from app.models import AttemptTelemetry

    stmt = select(AttemptTelemetry).where(AttemptTelemetry.derived_at.is_not(None))
    if project_id:
        stmt = stmt.where(AttemptTelemetry.project_id == project_id)
    rows = db.scalars(stmt).all()
    by_pair: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        vendor, model = row.vendor or UNDECLARED, row.model or ""
        version = row.binary_version or ""
        pair = (vendor, model)
        by_pair.setdefault(pair, {})
        by_pair[pair][version] = by_pair[pair].get(version, 0) + 1
    out = []
    for (vendor, model), versions in sorted(by_pair.items()):
        if len(versions) == 1:
            version, n = next(iter(versions.items()))
            if n < harness_svc.FLOOR:
                out.append({"vendor": vendor, "model": model, "binary_version": version,
                            "trigger": "new_row", "n": n})
            continue
        ordered = sorted(versions.items(), key=lambda kv: kv[0])
        previous, current = ordered[-2], ordered[-1]
        if current[1] < harness_svc.FLOOR:
            out.append({"vendor": vendor, "model": model, "binary_version": current[0],
                        "trigger": "version_change", "n": current[1],
                        "inherited_from": previous[0]})
    return out


# ---- the write (D3, D14, D15) ---------------------------------------------------------------

def _open_rows(db: Session, item: Item) -> list[Delegation]:
    rows = db.scalars(select(Delegation).where(Delegation.item_id == item.id)).all()
    return [r for r in rows if _unlinked(r)]


def _refuse_by_reach(item: Item, acknowledged: bool) -> None:
    """Two refusals, and they are not the same kind of thing (GRPH-832).

    **A `deploy` item is refused outright, and no argument gets past it.** That is a
    DECLARATION a signed-in person made — the field is unreachable from any agent credential —
    so overriding it here would be this code second-guessing the one input it can trust.

    **Prose that reads like deployment asks, once.** That is a heuristic, and a heuristic must
    never be a boundary: it fires on descriptions of past incidents as readily as on
    instructions, because no reader of free text can tell "rotate the production key" from "a
    worker rotated the production key". So it costs a caller one deliberate argument, which is
    then on the record. Having to type it is the point, exactly as it is for `--allow psql` in
    the supervisor's PATH shim.

    A planner that is an agent CAN acknowledge its way through. What it cannot do is act
    without an acknowledgement existing, attributed, and readable afterwards. That is the
    honest extent of it.
    """
    from app.services import reach as reach_svc

    if (item.reach or reach_svc.REPO) == reach_svc.DEPLOY:
        raise DelegationRefused(
            f"{item.key} is declared `reach=deploy`: it acts on a running system, and a "
            "delegated child gets a worktree and a shell. Do this one yourself.",
            code="validation",
            hint="a person set this field and only a person can clear it; if the item is "
                 "really repository work, change its reach in the UI")
    if acknowledged:
        return
    found = reach_svc.signals(item.description)
    if found:
        raise DelegationRefused(reach_svc.refusal(item.key, found), code="validation",
                                hint="pass acknowledge_reach=true, or set the item's reach "
                                     "to deploy in the UI")


def delegate(db: Session, *, agent: Agent, item: Item, lane: str, tier: str,
             note: str = "", lease_seconds: int, seat: bool = False, api_key=None,
             wave: str | None = None,
             scope: str | None = None,
             acknowledge_reach: bool = False) -> tuple[Delegation, str | None, str | None]:
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
    _refuse_by_reach(item, acknowledge_reach)
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
        # Recorded, not merely checked (GRPH-832): an acknowledgement nobody can look up
        # afterwards is a dialog box. This one has an author and a timestamp already.
        reach_acknowledged=bool(acknowledge_reach),
        capabilities_at_delegate=capabilities_of(item),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    code: str | None = None
    if seat:
        _, code = fleet_svc.mint_enrolment_as(
            db, minter_id=agent.id, project_id=item.project_id, role="worker",
            api_key=api_key, wave=wave, item_id=item.id, delegation_id=row.id,
            # GRPH-827: the wave's scope, carried into the credential the child will hold.
            # EXPLICIT rather than inferred from `item.prd_id`, which was the tempting version:
            # inferring would give an unscoped wave a scope its operator never set, and the
            # complaint being answered is precisely that the operator's flag stopped being
            # true one process down. A scope nobody asked for is a different surprise, not
            # a smaller one.
            prd_id=scope or None)
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
        finished = []
        for row in rows:
            if row.agent_id and not row.outcome and not row.closed_reason:
                row.outcome = outcome
                row.finished_at = now
                finished.append(row)
        db.flush()
        # PRD-38 D1: the attempt record is derived HERE, at the event, for the same reason
        # `outcome` is stored here — an attempt's ending cannot be re-derived from the item
        # once a later attempt has moved it. Swallowed like the rest of this function: a
        # telemetry row must never fail the transition it describes.
        if finished:
            from app.services import harness as harness_svc

            for row in finished:
                harness_svc.derive(db, row)
            harness_svc.purge_unfinished(db, item.project_id)
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
