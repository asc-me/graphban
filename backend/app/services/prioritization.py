"""Dependency-aware prioritization (feature C).

Readiness comes from the structured dependency graph (an item is blocked until every
item it depends on is done), not just the free-text `blocker`. Ready items are ranked by
a composite score: status, dependency fan-out (unblocks-many first), demand (request
votes rolled onto the linked item), effort, and staleness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Item, Link, Request, utcnow
from app.services import items as items_svc


@dataclass
class Context:
    items: list[Item]
    by_id: dict[str, Item]
    deps: dict[str, list[str]] = field(default_factory=dict)        # id -> items it depends on
    dependents: dict[str, list[str]] = field(default_factory=dict)  # id -> items that depend on it
    votes: dict[str, int] = field(default_factory=dict)             # id -> summed linked-request votes
    # Blockers that live in ANOTHER project (GRPH-1063), loaded for their status only. Kept out
    # of `by_id` so nothing iterating this project's items starts walking someone else's.
    external: dict[str, Item] = field(default_factory=dict)
    now: object = None

    def item(self, item_id: str) -> Item | None:
        """This project's item, or a cross-project blocker of one of them."""
        return self.by_id.get(item_id) or self.external.get(item_id)


def context(db: Session, project_id: str | None) -> Context:
    items = items_svc.list_items(db, project_id=project_id)
    by_id = {it.id: it for it in items}
    ctx = Context(items=items, by_id=by_id, now=utcnow())

    # Scope link/request scans to the project (tenant isolation + efficiency, AL-70). Links are
    # scoped by ENDPOINT, not by `Link.project_id` (GRPH-1063): a cross-project edge is stored
    # in one project, and a blocker in another repo's project must still block here. Filtering
    # on the row's project silently dropped it, and a dropped blocker reads as "ready".
    link_q = select(Link).where(Link.type == "dependency")
    req_q = select(Request).where(Request.linked_to.is_not(None))
    if project_id:
        mine = select(Item.id).where(Item.project_id == project_id)
        link_q = link_q.where(or_(Link.a.in_(mine), Link.b.in_(mine)))
        req_q = req_q.where(Request.project_id == project_id)

    far: set[str] = set()
    for lk in db.scalars(link_q).all():
        # a depends on b
        if lk.a not in by_id:
            continue
        if lk.b not in by_id:
            far.add(lk.b)
        ctx.deps.setdefault(lk.a, []).append(lk.b)
        ctx.dependents.setdefault(lk.b, []).append(lk.a)
    if far:
        # Loaded whatever the caller can read: readiness is not a read of the blocker, and a
        # blocker the caller cannot see must not read as "unblocked". An id that is no item at
        # all (a deleted row, a request) stays out, exactly as it did when both were local.
        ctx.external = {it.id: it for it in db.scalars(select(Item).where(Item.id.in_(far)))}

    for req in db.scalars(req_q).all():
        if req.linked_to in by_id:
            ctx.votes[req.linked_to] = ctx.votes.get(req.linked_to, 0) + (req.votes or 0)
    return ctx


def blocked_by(ctx: Context, item: Item) -> list[str]:
    """Dependency ids that aren't done yet (what's holding this item)."""
    return [
        d for d in ctx.deps.get(item.id, [])
        if (dep := ctx.item(d)) is not None and dep.status != "done"
    ]


def unblocks(ctx: Context, item: Item) -> int:
    return len(ctx.dependents.get(item.id, []))


def ready(ctx: Context, item: Item) -> bool:
    """Startable now: no manual blocker and no unfinished dependency."""
    return not item.blocker and not blocked_by(ctx, item)


def _age_days(ctx: Context, item: Item) -> float:
    if item.created_at is None:
        return 0.0
    created = item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=timezone.utc)
    return max(0.0, (ctx.now - created).total_seconds() / 86400.0)


def score(ctx: Context, item: Item) -> float:
    """Higher = do sooner. Ready-first is enforced by the caller; this ranks within."""
    status_weight = {"next": 3.0, "in_progress": 2.0, "backlog": 0.0}.get(item.status, 0.0)
    return (
        status_weight
        + 2.0 * unblocks(ctx, item)           # unblocking many is high-leverage
        + 0.5 * ctx.votes.get(item.id, 0)     # demand from linked requests
        - 0.1 * (item.effort or 0)            # small nudge toward smaller items
        + 0.02 * min(_age_days(ctx, item), 60)  # keep old work from rotting
    )


def prioritized(
    db: Session,
    project_id: str | None,
    statuses: tuple[str, ...] = ("backlog", "next"),
    include_blocked: bool = True,
) -> list[dict]:
    """Backlog/next items with priority metadata, ready-first then by score."""
    ctx = context(db, project_id)
    out = []
    for it in ctx.items:
        if it.status not in statuses:
            continue
        blk = blocked_by(ctx, it)
        is_ready = not it.blocker and not blk
        if not is_ready and not include_blocked:
            continue
        out.append({
            "item": it,
            "ready": is_ready,
            "blocked_by": blk,
            "unblocks": unblocks(ctx, it),
            "votes": ctx.votes.get(it.id, 0),
            "score": round(score(ctx, it), 3),
        })
    out.sort(key=lambda r: (not r["ready"], -r["score"], r["item"].sort_order))
    return out
