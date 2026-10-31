"""The per-entitlement headroom ledger (PRD-62 S3).

Runners report events; the server DERIVES each entitlement's D4 state from them on read.
Nothing here stores a state, so there is no column that can disagree with the events.

States: `available` (a child registered), `unknown` (never measured — ranked below available,
never treated as available), `exhausted` (a VendorLimit; the vendor's text is quoted verbatim,
D5), `broken` (failed before registering for a non-quota reason), `unreachable` (every runner
that declared it has lapsed — derived from presence, never stored).

**No burst onto an unmeasured entitlement.** An `unknown` or `broken` entitlement admits ONE
in-flight child until its first event arrives; that child registering moves it to `available`.
`broken` is probed the same way, so one registering child is how it recovers.

**Only a runner that declares the entitlement may report on it.** The reporter must be a
registered agent whose `entitlement_ids` names it; otherwise any worker in the org could post
`limit_hit` and lock an entitlement out of every `spawn_start`. The way back from `exhausted`
is that same holder reporting `registered` once its own vendor session works again (D5: the
reset is never parsed here, so the server cannot lift it on a clock).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound, Validation
from app.models import Agent, Entitlement, EntitlementEvent, Project, utcnow
from app.services import quotas

EVENTS: tuple[str, ...] = ("spawn_start", "registered", "limit_hit", "spawn_failed", "spend")
#: Events that settle the state. `spawn_start` and `spend` do not.
_SETTLING = {"registered": "available", "limit_hit": "exhausted", "spawn_failed": "broken"}
#: A `spawn_start` with no outcome stops counting as in flight after this long, so a runner
#: that died mid-spawn cannot hold the one slot forever.
INFLIGHT_TTL = timedelta(minutes=15)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _events(db: Session, entitlement_id: str) -> list[EntitlementEvent]:
    return list(db.scalars(select(EntitlementEvent)
                           .where(EntitlementEvent.entitlement_id == entitlement_id)
                           .order_by(EntitlementEvent.id)).all())


def _in_flight(events: list[EntitlementEvent], now: datetime) -> int:
    """Spawns started and not yet settled. A settling event closes the oldest open start."""
    open_starts: list[datetime] = []
    for e in events:
        if e.event == "spawn_start":
            open_starts.append(_aware(e.created_at))
        elif e.event in _SETTLING and open_starts:
            open_starts.pop(0)
    return sum(1 for t in open_starts if now - t < INFLIGHT_TTL)


def _unreachable(db: Session, row: Entitlement) -> bool:
    from app.services import fleet as fleet_svc  # fleet imports widely; keep this lazy

    # Scoped to the entitlement's org in SQL: a runner can only declare its own org's
    # entitlements, so agents elsewhere are never candidates. `entitlement_ids` is JSON, so
    # the membership test stays in Python, over this org's live agents only.
    agents = [a for a in db.scalars(
        select(Agent).join(Project, Project.id == Agent.project_id)
        .where(Agent.dismissed_at.is_(None), Project.org_id == row.org_id)).all()
              if row.id in (a.entitlement_ids or [])]
    return bool(agents) and all(fleet_svc.presence_state(a) in ("offline", "quarantined")
                                for a in agents)


def headroom(db: Session, row: Entitlement, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    events = _events(db, row.id)
    settled = [e for e in events if e.event in _SETTLING]
    last = settled[-1] if settled else None
    state = _SETTLING[last.event] if last else "unknown"
    if _unreachable(db, row):
        state = "unreachable"
    spawns = sum(1 for e in events if e.event == "registered")
    reported = [e for e in events if e.event == "spend" and e.tokens is not None]
    return {
        "state": state,
        # Verbatim (D5): never parsed into a time here, so no reset is invented.
        "vendor_text": last.vendor_text if last and state in ("exhausted", "broken") else "",
        "since": _aware(last.created_at).isoformat() if last else None,
        "in_flight": _in_flight(events, now),
        # Unreported is a count, never a zero spend (same shape as `spend.totals`).
        "spend": {"tokens": sum(e.tokens for e in reported), "reported": len(reported),
                  "unreported": max(spawns - len(reported), 0)},
    }


def report(db: Session, org_id: str, entitlement_id: str, event: str, *,
           agent_id: str | None = None, vendor_text: str = "", tokens=None) -> dict:
    """Record one runner event. `spawn_start` is also the admission check: it answers
    `admitted`, and records nothing when it refuses."""
    quotas.require_capacity_routing(db, org_id)
    if event not in EVENTS:
        raise Validation(f"event must be one of: {', '.join(EVENTS)}")
    # Locked before the admission read: two `spawn_start`s on an `unknown` entitlement would
    # otherwise both read in_flight=0 and both be admitted — the burst this ledger forbids.
    # A no-op on SQLite, which serializes writers anyway; raced on Postgres in
    # tests/test_lock_concurrency.py (GRPH-432).
    row = db.get(Entitlement, entitlement_id, with_for_update=True)
    if row is None or row.org_id != org_id:
        raise NotFound(f"entitlement {entitlement_id!r} not found in this org")
    reporter = db.get(Agent, agent_id) if agent_id else None
    if reporter is None or entitlement_id not in (reporter.entitlement_ids or []):
        raise Validation("report_entitlement_event is the holder's report: call it as a "
                         "registered agent that declared this entitlement at register_agent")
    if event == "spend" and tokens is not None and (
            isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0):
        raise Validation("tokens must be a non-negative integer, or omitted when unreported")
    if event in ("limit_hit", "spawn_failed") and not str(vendor_text).strip():
        raise Validation(f"{event} needs vendor_text: the vendor's own words, verbatim")
    admitted, reason = True, ""
    if event == "spawn_start":
        before = headroom(db, row)
        if before["state"] in ("exhausted", "unreachable"):
            admitted, reason = False, f"entitlement is {before['state']}"
        elif before["state"] in ("unknown", "broken") and before["in_flight"] >= 1:
            admitted, reason = False, (f"entitlement is {before['state']} and already has one "
                                       "child in flight; wait for its first report")
        if not admitted:
            return {"admitted": False, "reason": reason, "headroom": before}
    db.add(EntitlementEvent(entitlement_id=row.id, event=event, agent_id=agent_id,
                            vendor_text=str(vendor_text).strip(),
                            tokens=tokens if event == "spend" else None, created_at=utcnow()))
    db.commit()
    return {"admitted": admitted, "reason": reason, "headroom": headroom(db, row)}


# ---- PRD-62 S5: forecast and utilization ------------------------------------------------------

WEEK = timedelta(days=7)
#: R7: this many whole weeks are read, and this many of them must agree before a card fires.
CAPACITY_WEEKS, CAPACITY_MIN_WEEKS = 4, 3
#: R7: a week that used less than this share of a stated budget "reset with headroom unused".
UNUSED_SHARE = 0.5
#: R7: this many of those weeks with a limit hit is "routinely runs out early".
EARLY_MIN_WEEKS = 2
#: Said wherever a not-recorded count is shown, so a zero is never read where nobody looked.
NOT_RECORDED = ("not recorded: no supervisor reports it yet. This is unknown, not zero.")


def _spend_between(events: list[EntitlementEvent], start: datetime, end: datetime) -> dict:
    window = [e for e in events if start <= _aware(e.created_at) < end]
    reported = [e for e in window if e.event == "spend" and e.tokens is not None]
    spawns = sum(1 for e in window if e.event == "registered")
    return {"tokens": sum(e.tokens for e in reported), "reported": len(reported),
            "unreported": max(spawns - len(reported), 0),
            "limit_hits": sum(1 for e in window if e.event == "limit_hit")}


def _projection(row: Entitlement, events: list[EntitlementEvent], now: datetime,
                week: dict) -> dict:
    """When this entitlement runs out at this week's burn rate, or why that cannot be said.

    `token_budget` is read as the amount per reset period; the registry holds no other
    period, so the basis says so. No budget and no reported spend are each a stated reason,
    never a far-off date.
    """
    if row.token_budget is None:
        return {"at": None, "reason": "no token_budget stated"}
    spend = [e for e in events if e.event == "spend" and e.tokens is not None
             and now - WEEK <= _aware(e.created_at) <= now]
    if not spend or week["tokens"] <= 0:
        return {"at": None, "reason": "no reported spend this week"}
    remaining = row.token_budget - week["tokens"]
    if remaining <= 0:
        return {"at": now.isoformat(), "reason": "", "basis": "this week's tokens already "
                "meet or pass the stated token_budget"}
    elapsed_h = max((now - min(_aware(e.created_at) for e in spend)).total_seconds() / 3600, 1.0)
    rate = week["tokens"] / elapsed_h
    return {"at": (now + timedelta(hours=remaining / rate)).isoformat(), "reason": "",
            "basis": f"{week['tokens']} reported tokens over {elapsed_h:.1f}h, against a "
                     f"token_budget of {row.token_budget} read as per reset period"}


def forecast(db: Session, row: Entitlement, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    events = _events(db, row.id)
    base = headroom(db, row, now=now)
    week = _spend_between(events, now - WEEK, now + timedelta(seconds=1))
    return {
        "id": row.id, "vendor": row.vendor, "kind": row.kind, "plan_label": row.plan_label,
        "state": base["state"], "vendor_text": base["vendor_text"], "since": base["since"],
        # The vendor's reset words and the owner's stated cadence, verbatim: D5 never parses
        # a reset into a time, so there is no countdown to show.
        "reset_cadence": row.reset_cadence or None,
        "token_budget": row.token_budget,
        "week": week,
        "projected_exhaustion": _projection(row, events, now, week),
    }


def _capacity_rows(db: Session, org_id: str) -> list[Entitlement]:
    return list(db.scalars(select(Entitlement).where(Entitlement.org_id == org_id)
                           .order_by(Entitlement.vendor, Entitlement.kind,
                                     Entitlement.created_at)).all())


def utilization(db: Session, org_id: str, *, now: datetime | None = None) -> dict:
    """Org level: the share of stated headroom used last week, and what is not recorded."""
    now = now or datetime.now(timezone.utc)
    start, end = now - 2 * WEEK, now - WEEK
    used = budget = unreported = with_budget = without_budget = 0
    for row in _capacity_rows(db, org_id):
        if row.token_budget is None:
            without_budget += 1
            continue
        spent = _spend_between(_events(db, row.id), start, end)
        with_budget += 1
        budget += row.token_budget
        used += spent["tokens"]
        unreported += spent["unreported"]
    return {
        "week_start": start.isoformat(), "week_end": end.isoformat(),
        # None when no entitlement states a budget: nothing was measured against, so the
        # share is unknown, not 0%.
        "share_used": (used / budget) if budget else None,
        # A spend with no token count is not zero tokens: with any unreported, share_used only
        # counts what was reported, so it is a floor on the real share, never the share itself.
        "share_is_lower_bound": bool(budget) and unreported > 0,
        "tokens_used": used, "budget": budget or None,
        "entitlements_with_budget": with_budget, "entitlements_without_budget": without_budget,
        "unreported_spends": unreported,
        "waves_ended_by_capacity": {"count": None, "note": NOT_RECORDED},
        "items_deferred_to_reset": {"count": None, "note": NOT_RECORDED},
    }


def capacity(db: Session, org_id: str, *, now: datetime | None = None) -> dict:
    """The Observe Harness capacity block: per entitlement, then the org rollup."""
    quotas.require_capacity_routing(db, org_id)
    now = now or datetime.now(timezone.utc)
    return {"org_id": org_id, "as_of": now.isoformat(),
            "entitlements": [forecast(db, r, now=now) for r in _capacity_rows(db, org_id)],
            "utilization": utilization(db, org_id, now=now)}


def routine_findings(db: Session, org_id: str, *, now: datetime | None = None) -> list[dict]:
    """R7's evidence: entitlements that routinely reset with headroom unused, or run out early.

    Reads the last `CAPACITY_WEEKS` whole weeks, only those that began after the entitlement
    existed. A week with unreported spend is skipped for the "unused" reading — missing numbers
    would look like headroom. Fewer than `CAPACITY_MIN_WEEKS` readable weeks fires nothing.
    """
    now = now or datetime.now(timezone.utc)
    out: list[dict] = []
    for row in _capacity_rows(db, org_id):
        events = _events(db, row.id)
        created = _aware(row.created_at)
        weeks = []
        for k in range(1, CAPACITY_WEEKS + 1):
            start, end = now - k * WEEK, now - (k - 1) * WEEK
            if start >= created:
                weeks.append(_spend_between(events, start, end))
        if len(weeks) < CAPACITY_MIN_WEEKS:
            continue
        unused = [w for w in weeks if row.token_budget and w["unreported"] == 0
                  and w["tokens"] < UNUSED_SHARE * row.token_budget]
        early = [w for w in weeks if w["limit_hits"]]
        base = {"entitlement_id": row.id, "vendor": row.vendor, "kind": row.kind,
                "plan_label": row.plan_label, "weeks_read": len(weeks)}
        if len(unused) >= CAPACITY_MIN_WEEKS:
            out.append({**base, "kind_of": "unused", "weeks": len(unused),
                        "token_budget": row.token_budget,
                        "tokens_by_week": [w["tokens"] for w in unused]})
        if len(early) >= EARLY_MIN_WEEKS:
            out.append({**base, "kind_of": "early", "weeks": len(early)})
    return out
