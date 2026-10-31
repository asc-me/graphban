"""Capacity routing over entitlement headroom (PRD-62 S4).

Credentials never move; work does. This module never runs anything on another runner's
entitlement and never proxies for one: it only decides which claimant an item is OFFERED to.

**The rung order** (after tier and matrix score, which are decided elsewhere): `available`
entitlements first, soonest stated reset first (use it before it is lost), then `unknown`.
`exhausted`, `broken` and `unreachable` are skipped (D4). A reset nobody stated sorts after
every stated one and is never invented (D5).

**Claim shaping (D10).** When the only entitlement with headroom belongs to another runner, the
item is soft-reserved for that entitlement's holders for `RESERVATION_TTL`; claimants without
headroom of their own are not offered it, and the reservation lapses back to the ladder. No push.
It is written only when exactly one buildable entitlement (one with headroom, a live holder and
a review reserve) exists: with two, nobody needs the item held. And a reservation never refuses a
claimant whose own entitlement passes the review reserve: it holds the item against the ladder's
lower rungs, not against another runner with headroom. The reservation ENDS AT
ONCE when its entitlement reports `limit_hit` or `spawn_failed` (`end_reservations`): the reason
for holding the item is gone, so routing recalculates from the ledger instead of waiting out
the TTL.

**Review reserve (D7)**, per VENDOR: a build on vendor A is not scheduled unless some other
vendor has an `available` or `unknown` entitlement left to review it. `reviewer_diversity` is an
advisory rule rather than a setting, so for a capacity-routed claimant the reserve is always on.

A claimant that declared no entitlement, or an org not on a capacity-routing plan, is not routed
at all: `gate` returns None and the claim is exactly what it was before S4.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, Entitlement, Item, Organization, Project, RoutingDecision, utcnow
from app.services import headroom as headroom_svc
from app.services import quotas

RESERVATION_TTL = timedelta(minutes=10)
USABLE = ("available", "unknown")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rank(db: Session, org_id: str, *, now: datetime | None = None) -> list[dict]:
    """The org's entitlements with headroom, best first: available by soonest stated reset,
    then unknown. Skipped states are not returned — see `skipped` for why."""
    rows = db.scalars(select(Entitlement).where(Entitlement.org_id == org_id)
                      .order_by(Entitlement.created_at, Entitlement.id)).all()
    out = []
    for r in rows:
        state = headroom_svc.headroom(db, r, now=now)["state"]
        if state in USABLE:
            out.append({"id": r.id, "vendor": r.vendor, "state": state,
                        "next_reset_at": _aware(r.next_reset_at) if r.next_reset_at else None})
    far = datetime.max.replace(tzinfo=timezone.utc)
    out.sort(key=lambda e: (e["state"] != "available", e["next_reset_at"] or far))
    return out


def _why(entry: dict, ranked: list[dict]) -> str:
    if entry["state"] == "unknown":
        return f"{entry['id']} ({entry['vendor']}) is unknown: ranked below every available one"
    rivals = [e for e in ranked if e["state"] == "available" and e["id"] != entry["id"]]
    reset = (f"resets {entry['next_reset_at'].isoformat()}" if entry["next_reset_at"]
             else "no reset stated")
    if rivals:
        return (f"{entry['id']} ({entry['vendor']}) is available and {reset}; chosen over "
                f"{', '.join(e['id'] for e in rivals)} by soonest reset")
    return f"{entry['id']} ({entry['vendor']}) is the only available entitlement; {reset}"


def review_reserved(ranked: list[dict], vendor: str) -> bool:
    """D7: does a vendor OTHER than `vendor` still have an available or unknown entitlement?"""
    return any(e["vendor"] != vendor for e in ranked)


def gate(db: Session, item: Item, agent_id: str, *, now: datetime | None = None) -> dict | None:
    """Should `agent_id` be offered `item`? None = routing does not apply. Otherwise
    `{allowed, entitlement_id, reason}`; a refusal that shapes a claim writes the reservation."""
    now = now or datetime.now(timezone.utc)
    agent = db.get(Agent, agent_id)
    declared = list(agent.entitlement_ids or []) if agent else []
    if not declared:
        return None
    project = db.get(Project, item.project_id)
    org = db.get(Organization, project.org_id) if project is not None and project.org_id else None
    if org is None or not quotas.plan_of(org).capacity_routing:
        return None
    ranked = rank(db, org.id, now=now)
    mine = [e for e in ranked if e["id"] in declared]
    live = live_reservation(db, item.id, now=now)

    if live is not None and live.entitlement_id in declared:
        return {"allowed": True, "entitlement_id": live.entitlement_id,
                "reason": f"reserved for {live.entitlement_id}: {live.reason}"}

    for entry in mine:
        if review_reserved(ranked, entry["vendor"]):
            return {"allowed": True, "entitlement_id": entry["id"], "reason": _why(entry, ranked)}
    if live is not None:
        return {"allowed": False, "entitlement_id": live.entitlement_id,
                "reason": f"reserved for {live.entitlement_id} until "
                          f"{_aware(live.reserved_until).isoformat()}"}
    if mine:
        return {"allowed": False, "entitlement_id": None,
                "reason": "review reserve: building on this vendor would leave no "
                          "reviewer-eligible vendor with headroom (D7)"}

    # The claimant holds nothing with headroom. If exactly one other entitlement could take the
    # item, hold it for that one; if several could, refuse without holding (any of them may take
    # it); if none could, the ladder's next rung is the claimant's to take.
    held = _held(db, org.id)
    buildable = [e for e in ranked if e["id"] in held and review_reserved(ranked, e["vendor"])]
    if len(buildable) > 1:
        return {"allowed": False, "entitlement_id": None,
                "reason": "other runners have headroom: "
                          f"{', '.join(e['id'] for e in buildable)}"}
    if buildable:
        best = buildable[0]
        db.add(RoutingDecision(
            item_id=item.id, kind="reservation", entitlement_id=best["id"],
            reason=_why(best, ranked), reserved_until=now + RESERVATION_TTL,
            created_at=utcnow()))
        db.commit()
        return {"allowed": False, "entitlement_id": best["id"],
                "reason": f"held for {best['id']}: {_why(best, ranked)}"}
    return {"allowed": True, "entitlement_id": None,
            "reason": "no entitlement has headroom; next rung of the ladder"}


def _held(db: Session, org_id: str) -> set[str]:
    """Entitlement ids some live (undismissed) runner in the org declares. An entitlement nobody
    declares cannot take an item, so a reservation for it would hold the item for nobody."""
    return {e for a in db.scalars(
        select(Agent).join(Project, Project.id == Agent.project_id)
        .where(Agent.dismissed_at.is_(None), Project.org_id == org_id)).all()
        for e in (a.entitlement_ids or [])}


def live_reservation(db: Session, item_id: str, *, now: datetime | None = None
                     ) -> RoutingDecision | None:
    now = now or datetime.now(timezone.utc)
    rows = db.scalars(select(RoutingDecision).where(
        RoutingDecision.item_id == item_id, RoutingDecision.kind == "reservation",
        RoutingDecision.ended_at.is_(None)).order_by(RoutingDecision.id.desc())).all()
    return next((r for r in rows if _aware(r.reserved_until) > now), None)


def held_back(db: Session, item: Item, agent_id: str) -> bool:
    verdict = gate(db, item, agent_id)
    return verdict is not None and not verdict["allowed"]


def record_claim(db: Session, item: Item, agent_id: str) -> None:
    """The attempt record: which entitlement routing chose and why, beside the claimant's
    declared vendor and model. `matched` False is a claim that did not match its reservation."""
    verdict = gate(db, item, agent_id)
    if verdict is None:
        return
    agent = db.get(Agent, agent_id)
    caps = (agent.capabilities or {}) if agent else {}
    declared = list(agent.entitlement_ids or []) if agent else []
    db.add(RoutingDecision(
        item_id=item.id, kind="claim", entitlement_id=verdict["entitlement_id"],
        reason=verdict["reason"], agent_id=agent_id,
        agent_vendor=str(caps.get("vendor") or ""), agent_model=str(caps.get("model") or ""),
        matched=(verdict["entitlement_id"] in declared) if verdict["entitlement_id"] else None,
        created_at=utcnow()))
    db.commit()


def end_reservations(db: Session, entitlement_id: str, reason: str) -> int:
    """End every live reservation held for `entitlement_id`, now. Does not commit."""
    now = utcnow()
    rows = db.scalars(select(RoutingDecision).where(
        RoutingDecision.entitlement_id == entitlement_id, RoutingDecision.kind == "reservation",
        RoutingDecision.ended_at.is_(None))).all()
    for r in rows:
        r.ended_at, r.ended_reason = now, reason
    return len(rows)


def attempts(db: Session, item_id: str) -> list[dict]:
    rows = db.scalars(select(RoutingDecision).where(RoutingDecision.item_id == item_id)
                      .order_by(RoutingDecision.id)).all()
    return [{"kind": r.kind, "entitlement_id": r.entitlement_id, "reason": r.reason,
             "agent_id": r.agent_id, "agent_vendor": r.agent_vendor,
             "agent_model": r.agent_model, "matched": r.matched,
             "reserved_until": _aware(r.reserved_until).isoformat() if r.reserved_until else None,
             "ended_reason": r.ended_reason} for r in rows]


def release_failed_claims(db: Session, entitlement_id: str, agent_id: str | None) -> list[str]:
    """The holder's failure report hands back what it claimed on that entitlement: the item
    returns to the queue and, its reservation having ended, routes again from the ledger."""
    from app.services import items as items_svc

    if not agent_id:
        return []
    ids = set(db.scalars(select(RoutingDecision.item_id).where(
        RoutingDecision.kind == "claim", RoutingDecision.agent_id == agent_id,
        RoutingDecision.entitlement_id == entitlement_id)).all())
    released = []
    for item in db.scalars(select(Item).where(Item.id.in_(ids), Item.claimed_by == agent_id,
                                              Item.status == "in_progress")).all() if ids else []:
        if items_svc.release_item(db, item.id, agent_id, "next",
                                  reason=f"entitlement {entitlement_id} failed") is not None:
            released.append(item.key)
    return released


def axis(db: Session, project_id: str | None, agent_ids: list[str]) -> dict[str, dict | None]:
    """The entitlement axis of a proposal, read-only: per agent, the entitlement routing would
    use and why, or the review-reserve refusal. None = routing does not apply to that agent."""
    project = db.get(Project, project_id) if project_id else None
    org = db.get(Organization, project.org_id) if project is not None and project.org_id else None
    if org is None or not quotas.plan_of(org).capacity_routing:
        return {a: None for a in agent_ids}
    ranked = rank(db, org.id)
    out: dict[str, dict | None] = {}
    for aid in agent_ids:
        agent = db.get(Agent, aid)
        declared = list(agent.entitlement_ids or []) if agent else []
        if not declared:
            out[aid] = None
            continue
        mine = [e for e in ranked if e["id"] in declared]
        pick = next((e for e in mine if review_reserved(ranked, e["vendor"])), None)
        out[aid] = ({"entitlement_id": pick["id"], "reason": _why(pick, ranked)} if pick else
                    {"entitlement_id": None,
                     "reason": ("review reserve: no other vendor has headroom to review (D7)"
                                if mine else "no declared entitlement has headroom")})
    return out
