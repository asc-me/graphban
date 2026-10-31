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
    if event in ("limit_hit", "spawn_failed"):
        # S4: the reason to hold an item for this entitlement is gone, so the hold ends NOW and
        # routing recalculates from the ledger — not when the TTL runs out.
        from app.services import routing
        routing.end_reservations(db, row.id, event)
    db.commit()
    if event in ("limit_hit", "spawn_failed"):
        routing.release_failed_claims(db, row.id, agent_id)
    return {"admitted": admitted, "reason": reason, "headroom": headroom(db, row)}
