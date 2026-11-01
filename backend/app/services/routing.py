"""Capacity routing over entitlement headroom (PRD-62 S4).

Credentials never move; work does. This module never runs anything on another runner's
entitlement and never proxies for one: it only decides which claimant an item is OFFERED to.

**The rung order** (after tier and matrix score, which are decided elsewhere): `available`
entitlements first, then the ladder rung (D6: subscription, api_key within budget, local, or
the project policy's `ladder`), soonest stated reset first within a rung (use it before it is
lost), then `unknown`. A kind the ladder omits is never routed to, and an api_key whose stated
`token_budget` is met by this period's reported spend is skipped.
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

from app.models import (Agent, ApiKey, Entitlement, Item, Organization, Project, RoutingDecision,
                        utcnow)
from app.services import fleet_profiles
from app.services import headroom as headroom_svc
from app.services import quotas

RESERVATION_TTL = timedelta(minutes=10)
USABLE = ("available", "unknown")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def rung_of(kind: str) -> str:
    """The ladder rung an entitlement kind sits on; `cloud` rides the api_key rung."""
    return "api_key" if kind == "cloud" else kind


def _budget_spent(db: Session, row: Entitlement, now: datetime) -> bool:
    """An api_key whose stated token_budget is met by the tokens reported this period. The
    budget is read per reset period and the period as the trailing week, as `forecast` does."""
    if row.kind != "api_key" or row.token_budget is None:
        return False
    events = headroom_svc._events(db, row.id)
    spent = headroom_svc._spend_between(events, now - headroom_svc.WEEK, now + timedelta(seconds=1))
    return spent["tokens"] >= row.token_budget


def reviewer_ineligible(db: Session, org_id: str) -> set[str]:
    """Entitlement ids ruled out as a reviewer (D7: anything not ruled out counts). Two things
    rule one out: a `local` model no live reviewer-capable runner declares (nobody enabled it
    for review), and an entitlement every declaring runner of which is read-only. An API key or
    cloud account nobody declares yet is NOT ruled out: it is unknown, and unknown counts."""
    rows = db.scalars(select(Entitlement).where(Entitlement.org_id == org_id)).all()
    holders: dict[str, list[bool]] = {}
    for a in db.scalars(select(Agent).join(Project, Project.id == Agent.project_id)
                        .where(Agent.dismissed_at.is_(None), Project.org_id == org_id)).all():
        can_review = not (a.capabilities or {}).get("readonly")
        for i in a.entitlement_ids or []:
            holders.setdefault(i, []).append(can_review)
    out = set()
    for r in rows:
        held = holders.get(r.id, [])
        if (r.kind == "local" and not any(held)) or (held and not any(held)):
            out.add(r.id)
    return out


def rank(db: Session, org_id: str, *, now: datetime | None = None,
         ladder: list[str] | None = None) -> list[dict]:
    """The org's entitlements with headroom, best first: available before unknown, then the
    ladder rung (D6), then soonest stated reset. An entitlement on a rung the ladder omits, or
    an api_key whose budget is spent this period, is not returned."""
    ladder = list(fleet_profiles.DEFAULT_LADDER if ladder is None else ladder)
    now = now or datetime.now(timezone.utc)
    rows = db.scalars(select(Entitlement).where(Entitlement.org_id == org_id)
                      .order_by(Entitlement.created_at, Entitlement.id)).all()
    out = []
    ruled_out = reviewer_ineligible(db, org_id)
    for r in rows:
        rung = rung_of(r.kind)
        if rung not in ladder:
            continue
        state = headroom_svc.headroom(db, r, now=now)["state"]
        if state in USABLE and not _budget_spent(db, r, now):
            out.append({"id": r.id, "vendor": r.vendor, "state": state, "rung": rung,
                        "rung_rank": ladder.index(rung),
                        "reviewer_eligible": r.id not in ruled_out,
                        "next_reset_at": _aware(r.next_reset_at) if r.next_reset_at else None})
    far = datetime.max.replace(tzinfo=timezone.utc)
    out.sort(key=lambda e: (e["state"] != "available", e["rung_rank"], e["next_reset_at"] or far))
    return out


def _why(entry: dict, ranked: list[dict]) -> str:
    who = f"{entry['id']} ({entry['vendor']}, {entry['rung']} rung)"
    if entry["state"] == "unknown":
        return f"{who} is unknown: ranked below every available one"
    rivals = [e for e in ranked if e["state"] == "available" and e["id"] != entry["id"]]
    mine = entry["next_reset_at"]
    reset = f"resets {mine.isoformat()}" if mine else "no reset stated"
    head = f"{who} is available and {reset}"
    if not rivals:
        return f"{who} is the only available entitlement; {reset}"
    # A higher rung decides before any reset does; say so rather than crediting the reset.
    lower = [e for e in rivals if e["rung_rank"] > entry["rung_rank"]]
    rivals = [e for e in rivals if e["rung_rank"] == entry["rung_rank"]]
    # Say "soonest reset" only for rivals a stated reset actually ranked below this one.
    beaten = [e for e in rivals if mine and (e["next_reset_at"] is None or e["next_reset_at"] > mine)]
    sooner = [e for e in rivals if e["next_reset_at"] and (mine is None or e["next_reset_at"] < mine)]
    tied = [e for e in rivals if e not in beaten and e not in sooner]
    parts = []
    if lower:
        parts.append(f"{entry['rung']} rung is above {', '.join(e['id'] for e in lower)}")
    if beaten:
        parts.append(f"chosen over {', '.join(e['id'] for e in beaten)} by soonest reset")
    if tied:
        parts.append(f"no stated reset separates it from {', '.join(e['id'] for e in tied)}"
                     "; creation order decides")
    if sooner:
        parts.append(f"{', '.join(e['id'] for e in sooner)} reset sooner but were not "
                     "usable for this claim")
    return f"{head}; {'; '.join(parts)}"


def review_reserved(ranked: list[dict], vendor: str) -> bool:
    """D7: does a reviewer-eligible vendor OTHER than `vendor` still have an available or
    unknown entitlement?"""
    return any(e["vendor"] != vendor and e["reviewer_eligible"] for e in ranked)


def reserve(ranked: list[dict]) -> dict:
    """The reserve as the Observe page states it. D7 is per builder: a build on vendor V needs a
    reviewer-eligible vendor OTHER than V with headroom. So for each vendor that can build (any
    entry in `ranked`), work out what holds its reserve, and name the builders whose reserve is
    empty or rests only on `unknown`. Fewer than two eligible vendors always leaves one empty."""
    eligible = [e for e in ranked if e["reviewer_eligible"]]
    builders = sorted({e["vendor"] for e in ranked})
    empty_for, unknown_for = [], []
    for v in builders:
        held = [e for e in eligible if e["vendor"] != v]
        if not held:
            empty_for.append(v)
        elif all(e["state"] == "unknown" for e in held):
            unknown_for.append(v)
    empty = not ranked or bool(empty_for)
    if not ranked:
        warning = "no vendor has headroom: nothing satisfies the review reserve"
    elif empty_for:
        warning = ("nothing satisfies the review reserve for builds on "
                   f"{', '.join(empty_for)}: no other reviewer-eligible vendor has headroom")
    elif unknown_for:
        warning = ("the review reserve for builds on "
                   f"{', '.join(unknown_for)} rests only on unknown entitlements")
    else:
        warning = None
    return {"vendors": sorted({e["vendor"] for e in eligible}), "empty": empty,
            "empty_for": empty_for, "only_unknown": bool(unknown_for),
            "only_unknown_for": unknown_for, "warning": warning}


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
    ranked = rank(db, org.id, now=now,
                  ladder=fleet_profiles.ladder_of(db, item.project_id, _owner(db, agent)))
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


def _owner(db: Session, agent: Agent | None) -> str | None:
    """The user whose fleet profile applies to this runner: its API key's owner (PRD-37 D14)."""
    key = db.get(ApiKey, agent.api_key_id) if agent is not None and agent.api_key_id else None
    return key.user_id if key is not None else None


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
    by_ladder: dict[tuple, list[dict]] = {}
    out: dict[str, dict | None] = {}
    for aid in agent_ids:
        agent = db.get(Agent, aid)
        declared = list(agent.entitlement_ids or []) if agent else []
        if not declared:
            out[aid] = None
            continue
        ladder = tuple(fleet_profiles.ladder_of(db, project_id, _owner(db, agent)))
        if ladder not in by_ladder:
            by_ladder[ladder] = rank(db, org.id, ladder=list(ladder))
        ranked = by_ladder[ladder]
        mine = [e for e in ranked if e["id"] in declared]
        pick = next((e for e in mine if review_reserved(ranked, e["vendor"])), None)
        out[aid] = ({"entitlement_id": pick["id"], "reason": _why(pick, ranked)} if pick else
                    {"entitlement_id": None,
                     "reason": ("review reserve: no other vendor has headroom to review (D7)"
                                if mine else "no declared entitlement has headroom")})
    return out
