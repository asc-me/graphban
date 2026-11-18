"""Capacity routing over entitlement headroom (PRD-62 S4).

Credentials never move; work does. This module never runs anything on another runner's
entitlement and never proxies for one: it only decides which claimant an item is OFFERED to.

**The rung order** (after tier and matrix score, which are decided elsewhere): `available`
entitlements first, then the ladder rung (D6: subscription, api_key within budget, local, or
the project policy's `ladder`), then the project's tie-break within a rung: soonest stated
reset by default (use it before it is lost), or most headroom, or cheapest (GRPH-1505), then
`unknown`. The tie-break is only the LAST key: it never overrides availability, a rung, or the
custom hierarchy's subscription order. A kind the ladder omits is never routed to, and an api_key whose stated
`token_budget` is met by this period's reported spend is skipped.
`exhausted`, `broken` and `unreachable` are skipped (D4). A reset nobody stated sorts after
every stated one and is never invented (D5).

**When nothing above has headroom** (GRPH-1505, project policy `when_exhausted`): fall through
to the next rung (S4, the default), or `hold_until_reset`: the item is built on the ladder's top
rung or not at all, and a held item's reason names the earliest stated reset on that rung, or
says that none is stated.

**Claim shaping (D10).** When the only entitlement with headroom belongs to another runner, the
item is soft-reserved for that entitlement's holders for `RESERVATION_TTL`; claimants without
headroom of their own are not offered it, and the reservation lapses back to the ladder. No push.
Once a reservation for an entitlement has lapsed or ended, that item is never reserved for
that entitlement again — the item is open to every runner to claim normally.
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


def _subscription_order(db: Session, project_id: str | None) -> list[str] | None:
    """The subscription order in force: a list only under custom hierarchy, else None."""
    if not project_id:
        return None
    if fleet_profiles.routing_mode_of(db, project_id) != "custom_hierarchy":
        return None
    policy = fleet_profiles.policy_of(db, project_id) or {}
    order = policy.get("subscription_order")
    if not isinstance(order, list) or not order:
        return None
    return [str(e) for e in order]


#: Kinds whose marginal cost per token is zero: a subscription is prepaid and a local model
#: runs on hardware already paid for. Metered kinds are priced from the model their runners
#: declare; an unpriced one is NULL and sorts after every priced one, never read as free.
_PREPAID = ("subscription", "local")


def _cost_per_mtok(db: Session, row: Entitlement) -> float | None:
    """List price per million tokens (input + output) of using this entitlement now. Prepaid
    kinds are 0.0. A metered one is priced from the model its declaring runners report; when
    they report several, the dearest counts; when any is unpriced, so is the entitlement."""
    if row.kind in _PREPAID:
        return 0.0
    from app.providers import llm_meter

    prices = []
    for a in headroom_svc._declarers(db, row):
        model = str((a.capabilities or {}).get("model") or "")
        price = llm_meter.estimate_cost(row.vendor, model, 1_000_000, 1_000_000)
        if price is None:
            return None
        prices.append(price)
    return max(prices) if prices else None


def _headroom_tokens(db: Session, row: Entitlement, now: datetime) -> int | None:
    """Tokens left of the stated weekly `token_budget` (the period `_budget_spent` reads).
    NULL when no budget is stated: an amount nobody stated is not invented."""
    if row.token_budget is None:
        return None
    events = headroom_svc._events(db, row.id)
    spent = headroom_svc._spend_between(events, now - headroom_svc.WEEK, now + timedelta(seconds=1))
    return max(row.token_budget - spent["tokens"], 0)


_FAR = datetime.max.replace(tzinfo=timezone.utc)


def tie_key(entry: dict, tie_break: str) -> tuple:
    """The router's LAST sort key (GRPH-1505). A value nobody stated sorts after every stated one."""
    if tie_break == "most_headroom":
        left = entry.get("headroom_tokens")
        return (left is None, -(left or 0))
    if tie_break == "cheapest":
        cost = entry.get("cost_per_mtok")
        return (cost is None, cost or 0.0)
    reset = entry.get("next_reset_at")
    return (reset is None, reset or _FAR)


def _sub_key(entry_id: str, rung: str, sub_rank: dict[str, int]) -> int:
    """Under custom hierarchy a subscription sorts by its policy position, an unranked one after
    every ranked one; without it (`sub_rank` empty) every key is 0 and never decides."""
    if not sub_rank or rung != "subscription":
        return 0
    return sub_rank.get(entry_id, len(sub_rank))


def order(facts: list[dict], *, ladder: list[str], tie_break: str,
          subscription_order: list[str] | None = None) -> list[dict]:
    """Rank entitlement facts, best first, with no database read: the ONE ordering both the
    live router (`rank`) and replay use. A fact on a rung the ladder omits, not usable, or an
    api_key whose budget is spent is dropped; the rest sort available before unknown, then the
    ladder rung, then (custom hierarchy) the subscription order, then `tie_key`. The sort is
    stable, so facts arrive in creation order and creation order decides what nothing else does."""
    sub_rank = {eid: i for i, eid in enumerate(subscription_order or [])}
    out = []
    for f in facts:
        if f["rung"] not in ladder or f["state"] not in USABLE or f.get("budget_spent"):
            continue
        out.append({**f, "rung_rank": ladder.index(f["rung"]),
                    "subscription_rank": _sub_key(f["id"], f["rung"], sub_rank),
                    "tie_break": tie_break})
    out.sort(key=lambda e: (e["state"] != "available", e["rung_rank"],
                            e["subscription_rank"], tie_key(e, tie_break)))
    return out


def _facts(db: Session, org_id: str, *, now: datetime, ladder: list[str] | None,
           tie_breaks: tuple[str, ...], ruled_out: set[str]) -> list[dict]:
    """What the router reads about each org entitlement, in creation order. `ladder` None reads
    every rung (the replay snapshot); a tie-break input is read only when `tie_breaks` names it."""
    rows = db.scalars(select(Entitlement).where(Entitlement.org_id == org_id)
                      .order_by(Entitlement.created_at, Entitlement.id)).all()
    out = []
    for r in rows:
        rung = rung_of(r.kind)
        if ladder is not None and rung not in ladder:
            continue
        state = headroom_svc.headroom(db, r, now=now)["state"]
        usable = state in USABLE
        f = {"id": r.id, "vendor": r.vendor, "kind": r.kind, "state": state, "rung": rung,
             "budget_spent": usable and _budget_spent(db, r, now),
             "reviewer_eligible": r.id not in ruled_out,
             "next_reset_at": _aware(r.next_reset_at) if r.next_reset_at else None}
        if "most_headroom" in tie_breaks:
            f["headroom_tokens"] = _headroom_tokens(db, r, now)
        if "cheapest" in tie_breaks:
            f["cost_per_mtok"] = _cost_per_mtok(db, r)
        out.append(f)
    return out


def rank(db: Session, org_id: str, *, now: datetime | None = None,
         ladder: list[str] | None = None, project_id: str | None = None,
         tie_break: str | None = None) -> list[dict]:
    """The org's entitlements with headroom, best first: available before unknown, then the
    ladder rung (D6), then (custom hierarchy) the subscription order, then the project's
    tie-break, soonest stated reset by default. An entitlement on a rung the ladder omits, or
    an api_key whose budget is spent this period, is not returned."""
    ladder = list(fleet_profiles.DEFAULT_LADDER if ladder is None else ladder)
    now = now or datetime.now(timezone.utc)
    tie_break = tie_break or fleet_profiles.tie_break_of(db, project_id)
    facts = _facts(db, org_id, now=now, ladder=ladder, tie_breaks=(tie_break,),
                   ruled_out=reviewer_ineligible(db, org_id))
    return order(facts, ladder=ladder, tie_break=tie_break,
                 subscription_order=_subscription_order(db, project_id))


#: (phrase for "decided by", what a value nobody stated reads as, what a better rival did).
_TIE_WORDS = {
    "soonest_reset": ("soonest reset", "no stated reset", "reset sooner"),
    "most_headroom": ("most headroom", "no stated headroom", "have more headroom"),
    "cheapest": ("cheapest", "no list price", "are cheaper"),
}


def _metric(entry: dict, tie_break: str) -> str:
    if tie_break == "most_headroom":
        left = entry.get("headroom_tokens")
        return (f"has {left} tokens of headroom left this week" if left is not None
                else "states no token budget")
    if tie_break == "cheapest":
        cost = entry.get("cost_per_mtok")
        if cost is None:
            return "is unpriced"
        return (f"is prepaid ({entry.get('kind') or entry['rung']})" if cost == 0
                else f"lists at ${cost:g} per million tokens")
    mine = entry.get("next_reset_at")
    return f"resets {mine.isoformat()}" if mine else "no reset stated"


def _why(entry: dict, ranked: list[dict]) -> str:
    """Why this entry was chosen, naming the rule that actually decided: a higher rung, the
    subscription policy order, or the project's tie-break (GRPH-1505)."""
    tie_break = entry.get("tie_break") or fleet_profiles.DEFAULT_TIE_BREAK
    by, unstated, better = _TIE_WORDS[tie_break]
    who = f"{entry['id']} ({entry['vendor']}, {entry['rung']} rung)"
    if entry["state"] == "unknown":
        return f"{who} is unknown: ranked below every available one"
    rivals = [e for e in ranked if e["state"] == "available" and e["id"] != entry["id"]]
    metric = _metric(entry, tie_break)
    head = f"{who} is available and {metric}"
    if not rivals:
        return f"{who} is the only available entitlement; {metric}"
    # A higher rung decides before any tie-break does; say so rather than crediting the tie-break.
    lower = [e for e in rivals if e["rung_rank"] > entry["rung_rank"]]
    rivals = [e for e in rivals if e["rung_rank"] == entry["rung_rank"]]
    # On the subscription rung under custom hierarchy, the policy order decides before the
    # tie-break: a rival the order put below this one was not beaten by the tie-break.
    mine_sub = entry.get("subscription_rank", 0)
    policy = [e for e in rivals if e.get("subscription_rank", 0) > mine_sub]
    above = [e for e in rivals if e.get("subscription_rank", 0) < mine_sub]
    rivals = [e for e in rivals if e.get("subscription_rank", 0) == mine_sub]
    mine_key = tie_key(entry, tie_break)
    mine_stated = not mine_key[0]
    beaten = [e for e in rivals if mine_stated and mine_key < tie_key(e, tie_break)]
    ahead = [e for e in rivals if tie_key(e, tie_break) < mine_key]
    tied = [e for e in rivals if e not in beaten and e not in ahead]
    parts = []
    if lower:
        parts.append(f"{entry['rung']} rung is above {', '.join(e['id'] for e in lower)}")
    if policy:
        parts.append(f"chosen over {', '.join(e['id'] for e in policy)} by subscription "
                     "policy order")
    if beaten:
        parts.append(f"chosen over {', '.join(e['id'] for e in beaten)} by {by}")
    if tied:
        parts.append(f"{unstated} separates it from {', '.join(e['id'] for e in tied)}"
                     "; creation order decides")
    if ahead:
        parts.append(f"{', '.join(e['id'] for e in ahead)} {better} but were not "
                     "usable for this claim")
    if above:
        parts.append(f"{', '.join(e['id'] for e in above)} rank higher in subscription "
                     "policy order but were not usable for this claim")
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
    if not quotas.capacity_routing_on(db, org):
        return None
    ladder = fleet_profiles.ladder_of(db, item.project_id, _owner(db, agent))
    ranked = rank(db, org.id, now=now, ladder=ladder, project_id=item.project_id)
    hold = fleet_profiles.when_exhausted_of(db, item.project_id) == "hold_until_reset"
    if hold:
        # GRPH-1505: the item is built on the ladder's top rung or not at all. The review
        # reserve still reads every rung (`ranked`): a reviewer is not a build.
        top = [e for e in ranked if e["rung_rank"] == 0]
        if not top:
            return {"allowed": False, "entitlement_id": None,
                    "reason": held_until_reset(db, org.id, ladder[0], now=now)}
        candidates = top
    else:
        candidates = ranked
    mine = [e for e in candidates if e["id"] in declared]
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
    buildable = [e for e in candidates
                 if e["id"] in held and review_reserved(ranked, e["vendor"])]
    if len(buildable) > 1:
        return {"allowed": False, "entitlement_id": None,
                "reason": "other runners have headroom: "
                          f"{', '.join(e['id'] for e in buildable)}"}
    if buildable:
        best = buildable[0]
        if _reservation_spent(db, item.id, best["id"], now=now):
            if hold:
                return {"allowed": False, "entitlement_id": None,
                        "reason": f"hold until reset: {best['id']} on the {ladder[0]} rung has "
                                  "headroom but its hold on this item lapsed; the item is not "
                                  "routed down a rung"}
            return {"allowed": True, "entitlement_id": None,
                    "reason": "no entitlement has headroom; next rung of the ladder"}
        db.add(RoutingDecision(
            item_id=item.id, kind="reservation", entitlement_id=best["id"],
            reason=_why(best, ranked), reserved_until=now + RESERVATION_TTL,
            candidates=snapshot(db, org.id, item.project_id, ladder, declared, now=now),
            created_at=utcnow()))
        db.commit()
        return {"allowed": False, "entitlement_id": best["id"],
                "reason": f"held for {best['id']}: {_why(best, ranked)}"}
    if hold:
        return {"allowed": False, "entitlement_id": None,
                "reason": f"hold until reset: no runner holding a {ladder[0]} entitlement with "
                          "headroom can build this; the item is not routed down a rung"}
    return {"allowed": True, "entitlement_id": None,
            "reason": "no entitlement has headroom; next rung of the ladder"}


# ---- replay (GRPH-1712): the candidates a decision was made from, re-ranked under a draft ----

#: Bumped when the snapshot's shape changes; replay reads only versions it knows.
SNAPSHOT_VERSION = 1
_TIE_BREAKS_ALL = ("soonest_reset", "most_headroom", "cheapest")


def snapshot(db: Session, org_id: str, project_id: str | None, ladder: list[str],
             declared: list[str], *, now: datetime | None = None) -> dict:
    """The ranked candidate set at decision time, as stored on `RoutingDecision.candidates`: the
    policy in force (ladder, tie-break, when-exhausted, subscription order), the claimant's
    declared entitlements, and EVERY org entitlement — its id, kind, rung, headroom state, stated
    next reset and every tie-break input (headroom remaining, cost) — so any proposed ladder or
    tie-break can be re-ranked from it. `entitlements` lists them in creation order, each with
    its `created` index (the live router's last tie-break) and its `rank` under the policy in
    force (None when that policy did not rank it: wrong rung, no headroom).
    """
    now = now or datetime.now(timezone.utc)
    tie_break = fleet_profiles.tie_break_of(db, project_id)
    sub_order = _subscription_order(db, project_id)
    facts = _facts(db, org_id, now=now, ladder=None, tie_breaks=_TIE_BREAKS_ALL,
                   ruled_out=reviewer_ineligible(db, org_id))
    held = _held(db, org_id)
    ranked_ids = [e["id"] for e in order(facts, ladder=ladder, tie_break=tie_break,
                                         subscription_order=sub_order)]
    pos = {eid: i for i, eid in enumerate(ranked_ids)}
    return {
        "v": SNAPSHOT_VERSION,
        "at": now.isoformat(),
        "policy": {"ladder": list(ladder), "tie_break": tie_break,
                   "when_exhausted": fleet_profiles.when_exhausted_of(db, project_id),
                   "subscription_order": sub_order},
        "declared": list(declared),
        "entitlements": [{**f, "next_reset_at": (f["next_reset_at"].isoformat()
                                                 if f["next_reset_at"] else None),
                          "held": f["id"] in held,
                          "created": i, "rank": pos.get(f["id"])}
                         for i, f in enumerate(facts)],
    }


def _stored_facts(snap: dict) -> list[dict]:
    """A snapshot's entitlements back in creation order with datetimes restored, so `order`
    breaks the ties it cannot decide exactly as the live router did. Sorted by the stored
    `created` index, never by the recorded `rank`: a tie the proposed policy cannot break must
    fall back to creation order, as `rank` does live, not to the recorded policy's ordering."""
    stored = snap.get("entitlements") or []
    out = []
    for f in sorted(stored, key=lambda f: f.get("created", 0)):
        reset = f.get("next_reset_at")
        out.append({**f, "next_reset_at": _aware(datetime.fromisoformat(reset)) if reset else None})
    return out


def choose(snap: dict, policy: dict) -> tuple[str, str | None]:
    """What `gate` would decide from a stored snapshot under `policy` — (verdict, entitlement).
    Pure: reads nothing but the snapshot. Verdicts: `claimed` (on one of the claimant's own),
    `held_for` (soft-held for the one other runner that could build it), `held` (hold until
    reset), `refused` (the review reserve, or several other runners could take it), `next_rung`
    (nothing has headroom; the claimant takes the ladder's next rung). The live-reservation and
    lapsed-reservation paths depend on what happened between decisions, not on the policy, and
    are not replayed: a decision is re-ranked from its own candidates alone."""
    ladder = list(policy["ladder"])
    facts = _stored_facts(snap)
    ranked = order(facts, ladder=ladder, tie_break=policy["tie_break"],
                   subscription_order=policy.get("subscription_order"))
    declared = set(snap.get("declared") or [])
    hold = policy["when_exhausted"] == "hold_until_reset"
    candidates = ranked
    if hold:
        candidates = [e for e in ranked if e["rung_rank"] == 0]
        if not candidates:
            return "held", None
    mine = [e for e in candidates if e["id"] in declared]
    for e in mine:
        if review_reserved(ranked, e["vendor"]):
            return "claimed", e["id"]
    if mine:
        return "refused", None
    buildable = [e for e in candidates if e.get("held") and review_reserved(ranked, e["vendor"])]
    if len(buildable) > 1:
        return "refused", None
    if buildable:
        return "held_for", buildable[0]["id"]
    if hold:
        return "held", None
    return "next_rung", None


def replay_proposal(*, tie_break: str | None = None, when_exhausted: str | None = None,
                    ladder: list[str] | None = None, routing_mode: str | None = None,
                    subscription_order: list[str] | None = None) -> dict:
    """Validate a draft policy for `replay` with the same rules the policy write applies.
    Raises `fleet_profiles.ProfileInvalid` naming the bad value."""
    invalid = fleet_profiles.ProfileInvalid
    if tie_break is not None and tie_break not in fleet_profiles.TIE_BREAKS:
        raise invalid(f"tie_break must be one of {list(fleet_profiles.TIE_BREAKS)}")
    if when_exhausted is not None and when_exhausted not in fleet_profiles.WHEN_EXHAUSTED:
        raise invalid(f"when_exhausted must be one of {list(fleet_profiles.WHEN_EXHAUSTED)}")
    if routing_mode is not None and routing_mode not in fleet_profiles.ROUTING_MODES:
        raise invalid(f"routing_mode must be one of {list(fleet_profiles.ROUTING_MODES)}")
    return {"tie_break": tie_break, "when_exhausted": when_exhausted,
            "ladder": fleet_profiles._ladder(ladder) if ladder is not None else None,
            "routing_mode": routing_mode, "subscription_order": subscription_order or None}


REPLAY_DAYS = 7
_REPLAY_EXAMPLES = 5


def replay(db: Session, project_id: str, proposal: dict | None = None, *,
           days: int = REPLAY_DAYS, now: datetime | None = None) -> dict:
    """Re-rank every routing decision on this project's items in the trailing `days` under a
    proposed policy, from the candidates each one stored. `proposal` keys (all optional; an
    absent one keeps what that decision recorded): `ladder`, `tie_break`, `when_exhausted`,
    `routing_mode` + `subscription_order`.

    A decision MOVED when the proposal's outcome (verdict, entitlement) differs from its own
    recorded policy's outcome on the same candidates — so replaying the policy in force moves
    nothing, and the answer describes the candidates as they were then, never today's headroom.
    `held` counts the moved ones the proposal would have held until a reset. Rows written before
    candidates were recorded are `not_replayable`, never counted as "would not have moved"."""
    from app.models import Item

    proposal = proposal or {}
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    rows = db.scalars(select(RoutingDecision).join(Item, Item.id == RoutingDecision.item_id)
                      .where(Item.project_id == project_id, RoutingDecision.created_at >= start)
                      .order_by(RoutingDecision.id)).all()
    replayable = not_replayable = moved = held = 0
    examples = []
    for r in rows:
        snap = r.candidates
        if not isinstance(snap, dict) or snap.get("v") != SNAPSHOT_VERSION:
            not_replayable += 1
            continue
        replayable += 1
        recorded = dict(snap["policy"])
        proposed = dict(recorded)
        for k in ("ladder", "tie_break", "when_exhausted"):
            if proposal.get(k) is not None:
                proposed[k] = proposal[k]
        if proposal.get("routing_mode") is not None:
            proposed["subscription_order"] = (proposal.get("subscription_order") or None
                                              if proposal["routing_mode"] == "custom_hierarchy"
                                              else None)
        before, after = choose(snap, recorded), choose(snap, proposed)
        if before == after:
            continue
        moved += 1
        held += after[0] == "held"
        if len(examples) < _REPLAY_EXAMPLES:
            item = db.get(Item, r.item_id)
            examples.append({"item_key": item.key if item is not None else r.item_id,
                             "at": _aware(r.created_at).isoformat(),
                             "from": {"verdict": before[0], "entitlement_id": before[1]},
                             "to": {"verdict": after[0], "entitlement_id": after[1]}})
    reason = None
    if not rows:
        reason = f"no routing decisions in the last {days} days"
    elif not replayable:
        reason = (f"nothing in the last {days} days is replayable: every decision was recorded "
                  "before routing stored its candidates")
    return {"project_id": project_id, "days": days, "window_start": start.isoformat(),
            "window_end": now.isoformat(), "decisions": len(rows), "replayable": replayable,
            "not_replayable": not_replayable, "moved": moved if replayable else None,
            "held": held if replayable else None, "examples": examples, "reason": reason}


def held_until_reset(db: Session, org_id: str, rung: str, *, now: datetime) -> str:
    """The reason a held item waits (GRPH-1505): the earliest STATED reset among the
    entitlements it would have used, the ladder's top rung, or that none is stated."""
    rows = [r for r in db.scalars(select(Entitlement).where(Entitlement.org_id == org_id)
                                  .order_by(Entitlement.created_at, Entitlement.id)).all()
            if rung_of(r.kind) == rung]
    stated = sorted((_aware(r.next_reset_at), r.id) for r in rows if r.next_reset_at)
    if not rows:
        return (f"hold until reset: the org has no {rung} entitlement, so no reset will "
                "release this item; it is not routed down a rung")
    if not stated:
        return (f"hold until reset: nothing on the {rung} rung has headroom and no reset is "
                f"stated for {', '.join(r.id for r in rows)}; the item waits until one "
                "reports headroom and is not routed down a rung")
    at, eid = stated[0]
    return (f"hold until reset: nothing on the {rung} rung has headroom; the earliest stated "
            f"reset is {eid} at {at.isoformat()}; the item is not routed down a rung")


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


def _reservation_spent(db: Session, item_id: str, entitlement_id: str, *, now: datetime
                       ) -> bool:
    """True when this item was already reserved for `entitlement_id` and that hold is over."""
    rows = db.scalars(select(RoutingDecision).where(
        RoutingDecision.item_id == item_id, RoutingDecision.kind == "reservation",
        RoutingDecision.entitlement_id == entitlement_id)).all()
    for row in rows:
        if row.ended_at is not None or _aware(row.reserved_until) <= now:
            return True
    return False


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
    project = db.get(Project, item.project_id)
    ladder = fleet_profiles.ladder_of(db, item.project_id, _owner(db, agent))
    db.add(RoutingDecision(
        item_id=item.id, kind="claim", entitlement_id=verdict["entitlement_id"],
        reason=verdict["reason"], agent_id=agent_id,
        agent_vendor=str(caps.get("vendor") or ""), agent_model=str(caps.get("model") or ""),
        matched=(verdict["entitlement_id"] in declared) if verdict["entitlement_id"] else None,
        candidates=snapshot(db, project.org_id, item.project_id, ladder, declared),
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


NEVER_ROUTED = "never routed by capacity"


def routing_reason(db: Session, item: Item) -> dict:
    """Why one item was routed where it was: its attempts in order, and the latest verdict with
    `why` verbatim from the stored reason. An item with no rows says so. No per-rung skip trace
    is recorded, so none is returned."""
    rows = db.scalars(select(RoutingDecision).where(RoutingDecision.item_id == item.id)
                      .order_by(RoutingDecision.id)).all()
    out = {"item_key": item.key, "stage": item.status, "claimant": item.claimed_by,
           "attempts": attempts(db, item.id), "latest": None, "reason": None, "at": None}
    if not rows:
        out["reason"] = NEVER_ROUTED
        return out
    last = rows[-1]
    out["latest"] = {"verdict": verdict_of(last), "entitlement_id": last.entitlement_id,
                     "why": last.reason}
    out["at"] = _aware(last.created_at).isoformat()
    return out


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
    if not quotas.capacity_routing_on(db, org):
        return {a: None for a in agent_ids}
    by_ladder: dict[tuple, list[dict]] = {}
    out: dict[str, dict | None] = {}
    hold = fleet_profiles.when_exhausted_of(db, project_id) == "hold_until_reset"
    for aid in agent_ids:
        agent = db.get(Agent, aid)
        declared = list(agent.entitlement_ids or []) if agent else []
        if not declared:
            out[aid] = None
            continue
        ladder = tuple(fleet_profiles.ladder_of(db, project_id, _owner(db, agent)))
        if ladder not in by_ladder:
            by_ladder[ladder] = rank(db, org.id, ladder=list(ladder), project_id=project_id)
        ranked = by_ladder[ladder]
        if hold and not any(e["rung_rank"] == 0 for e in ranked):
            out[aid] = {"entitlement_id": None,
                        "reason": held_until_reset(db, org.id, ladder[0],
                                                   now=datetime.now(timezone.utc))}
            continue
        mine = [e for e in ranked if e["id"] in declared and (not hold or e["rung_rank"] == 0)]
        pick = next((e for e in mine if review_reserved(ranked, e["vendor"])), None)
        out[aid] = ({"entitlement_id": pick["id"], "reason": _why(pick, ranked)} if pick else
                    {"entitlement_id": None,
                     "reason": ("review reserve: no other vendor has headroom to review (D7)"
                                if mine else "no declared entitlement has headroom")})
    return out


# ---- the Harness Capacity tab: what the router did over the rollup window --------------------

#: (key, label, not_recorded) in display order. The router writes a `claim` row for each claim it
#: lets through (on an entitlement, or on none: the ladder's next rung) and a `reservation` row
#: when it holds an item for another runner. It writes NOTHING when the review reserve refuses a
#: build (gate() returns the refusal and the claim is skipped), and nothing defers an item to a
#: reset, so those two groups carry `count: None` and a note, never 0 (the rollup's rule).
VERDICTS: tuple[tuple[str, str, str | None], ...] = (
    ("claimed", "Claimed on an entitlement", None),
    ("held", "Held for another runner", None),
    ("deferred", "Deferred to a reset",
     "not recorded: the router does not write a decision when it defers an item to a reset. "
     "This is unknown, not zero."),
    ("review_reserve", "Review reserve refused a build",
     "not recorded: a review-reserve refusal skips the claim without writing a decision. "
     "This is unknown, not zero."),
    ("next_rung", "Fell to the next ladder rung", None),
)
_RECENT = 5


def verdict_of(row: RoutingDecision) -> str | None:
    """Class a row the router wrote. Only the verdicts it records can come back from here."""
    if row.kind == "reservation":
        return "held"
    if row.kind == "claim":
        return "claimed" if row.entitlement_id else "next_rung"
    return None


def decisions_by_verdict(db: Session, org_id: str, *, now: datetime | None = None) -> dict:
    """RoutingDecision rows for the org over the trailing 7 days, grouped by verdict, with up to
    5 recent rows each (reason verbatim). A verdict the router never records is `count: None`
    with a `note`, not 0. With no rows at all, `reason` says why."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    rows = [r for r in db.scalars(
        select(RoutingDecision).join(Item, Item.id == RoutingDecision.item_id)
        .join(Project, Project.id == Item.project_id)
        .where(Project.org_id == org_id, RoutingDecision.created_at >= start)
        .order_by(RoutingDecision.id.desc())).all()]
    groups = []
    for key, label, not_recorded in VERDICTS:
        if not_recorded:
            groups.append({"verdict": key, "label": label, "count": None, "recent": [],
                           "note": not_recorded})
            continue
        mine = [r for r in rows if verdict_of(r) == key]
        recent = []
        for r in mine[:_RECENT]:
            item = db.get(Item, r.item_id)
            recent.append({"item_key": item.key if item is not None else r.item_id,
                           "entitlement_id": r.entitlement_id, "reason": r.reason,
                           "at": _aware(r.created_at).isoformat()})
        groups.append({"verdict": key, "label": label, "count": len(mine), "recent": recent,
                       "note": None})
    reason = None
    if not rows:
        reason = "no claims routed in window"
    return {"window_start": start.isoformat(), "window_end": now.isoformat(),
            "groups": groups, "reason": reason}
