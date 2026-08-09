"""Item (tracker) service — shared by REST routers and the MCP server."""
from __future__ import annotations

import logging

from datetime import timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import Item, Project, utcnow
from app.services import keys

logger = logging.getLogger(__name__)

STATUSES = ["backlog", "next", "in_progress", "review", "done", "blocked"]
FIDELITIES = ["low", "high"]  # low = specifiable now; high = needs a prototype (AL-68)
DEFAULT_LEASE_SECONDS = 600  # a claim with no heartbeat within this window is reclaimable


def _stored_prd_id(db: Session, prd_id: str | None) -> str | None:
    """The frozen id a caller's PRD key means, for storing in `Item.prd_id`.

    `keys` resolves the entity a call *addresses*; this covers the entity a call
    *references*. Without it a caller who passes a live rendering (`GRPH-P12`) freezes
    that rendering into the row, and `coverage` — the one place that joins on the raw
    string — stops seeing the item. Reads hide it: `_key_of` renders a dangling
    reference back as itself, so the item looks correctly linked from every surface.

    Unresolvable values are stored as given rather than dropped, matching how the rest
    of the resolve path degrades: a dangling reference is recoverable, a silently
    discarded link is not.
    """
    if not prd_id:
        return prd_id
    return keys.resolve_prd(db, prd_id) or prd_id


def list_items(db: Session, project_id: str | None = None, status: str | None = None) -> list[Item]:
    stmt = select(Item)
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    if status:
        stmt = stmt.where(Item.status == status)
    stmt = stmt.order_by(Item.sort_order.asc(), Item.created_at.desc())
    return list(db.scalars(stmt).all())


def get_item(db: Session, item_id: str) -> Item | None:
    return db.get(Item, keys.resolve_item(db, item_id) or item_id)


def create_item(
    db: Session,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    effort: int = 0,
    status: str = "backlog",
    project_id: str = "core",
    reporter: dict | None = None,
    date: str = "",
    touchpoints: list[str] | None = None,
    prd_id: str | None = None,
    prd_section: str = "",
    fidelity: str = "low",
) -> Item:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    if fidelity not in FIDELITIES:
        raise ValueError(f"invalid fidelity: {fidelity}")
    if db.get(Project, project_id) is None:
        raise ValueError(f"unknown project: {project_id!r}")
    max_order = db.scalar(select(func.max(Item.sort_order))) or 0
    # The id is frozen identity; `number` is what the key renders from (PRD-13).
    item_id, number = keys.mint(db, project_id, "item")
    item = Item(
        id=item_id,
        number=number,
        project_id=project_id,
        title=title,
        description=description or "",
        tags=tags or [],
        touchpoints=touchpoints or [],
        effort=int(effort or 0),
        status=status,
        sort_order=max_order + 1,
        reporter=reporter or {},
        date=date,
        prd_id=_stored_prd_id(db, prd_id),
        prd_linked_at=utcnow() if prd_id else None,
        prd_section=prd_section or "",
        fidelity=fidelity,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    if item.touchpoints:
        from app.services.clustering import sync_code_links
        sync_code_links(db, item)
    return item


_EVIDENCE_KINDS = {"test", "url", "screenshot", "health", "note"}


def normalize_evidence(raw) -> list[dict]:
    """Coerce evidence receipts to {kind, detail, url}; drop empties (AL-53).

    `kind` is advisory (test | url | screenshot | health | note) and falls back to
    `note`; a receipt with neither detail nor url is dropped."""
    out: list[dict] = []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "note").lower()
        if kind not in _EVIDENCE_KINDS:
            kind = "note"
        detail = str(e.get("detail") or "").strip()
        url = str(e.get("url") or "").strip()
        if not detail and not url:
            continue
        out.append({"kind": kind, "detail": detail, "url": url})
    return out


def update_item(db: Session, item_id: str, **fields) -> Item | None:
    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        return None
    if "status" in fields and fields["status"] is not None:
        if fields["status"] not in STATUSES:
            raise ValueError(f"invalid status: {fields['status']}")
    if fields.get("fidelity") is not None and fields["fidelity"] not in FIDELITIES:
        raise ValueError(f"invalid fidelity: {fields['fidelity']}")
    prev_status = item.status
    # Captured BEFORE the status moves. `intent_hold` is about work in flight and goes
    # quiet once an item is done, so asking after the transition always answers None —
    # which silently turned the completion receipt into dead code.
    hold_at_completion = (
        _pending_hold(db, item)
        if fields.get("status") == "done" and prev_status != "done" else None
    )
    if fields.get("prd_id") is not None:
        fields = {**fields, "prd_id": _stored_prd_id(db, fields["prd_id"])}
        # Stamped when the link CHANGES, never on an ordinary edit. Re-saving an item
        # already on this PRD must not restamp it, or every touch after approval would
        # look like freshly added scope and the drift number would climb on activity
        # alone — a metric that rises when you work is one people learn to ignore.
        if fields["prd_id"] != item.prd_id:
            item.prd_linked_at = utcnow()
    for key in ("title", "description", "status", "tags", "effort", "blocker", "pr", "date",
                "github_url", "assignee", "touchpoints", "prd_id", "prd_section", "fidelity"):
        if key in fields and fields[key] is not None:
            setattr(item, key, fields[key])
    if fields.get("evidence") is not None:
        item.evidence = normalize_evidence(fields["evidence"])
    db.commit()
    db.refresh(item)

    if "touchpoints" in fields and fields["touchpoints"] is not None and item.touchpoints:
        from app.services.clustering import sync_code_links
        sync_code_links(db, item)

    # Work can reach `in_progress` without ever taking a lease — a human moving a card, an
    # agent that edits rather than claims. Stamping here too is what keeps the hold from
    # covering only the subset of work that happens to use the claim path.
    if item.status == "in_progress" and prev_status != "in_progress":
        stamp_baseline_at_start(db, item)

    if item.status == "done" and prev_status != "done":
        _record_superseded_intent(db, item, hold_at_completion)
        _classify_against_goal(db, item)
        _auto_extract_lessons(db, item)
    return item


def _classify_against_goal(db: Session, item: Item) -> None:
    """Fire the platform judge on completion (GRPH-249).

    Here rather than at link time on purpose: at link time an item is an intention with
    nothing delivered to judge, and a judgement of an intention is a judgement of a
    sentence somebody typed. At completion it has evidence, touchpoints, and work behind
    it.

    Never allowed to break the completion. A judge that errors, times out, or is not
    configured must not stop an agent marking work done — the classification is a read on
    the work, not a gate on it, and making delivery depend on a model being reachable is
    how a feature gets routed around.
    """
    from app.services import prds as prd_svc  # local: prds imports this module

    try:
        prd_svc.classify_work(db, item)
    except Exception:  # noqa: BLE001
        logger.warning("platform judge: classification failed for %s", item.id, exc_info=True)


def _pending_hold(db: Session, item: Item) -> dict | None:
    from app.services import prds as prd_svc  # local: prds imports this module

    return prd_svc.intent_hold(db, item)


def _record_superseded_intent(db: Session, item: Item, hold: dict | None) -> None:
    """Stamp a completion that happened against intent which has since moved (GRPH-312).

    The hold is delivered on every read, but an agent can complete an item without ever
    looking — and then its work is classified against superseded intent and the resulting
    drift is blamed on delivery rather than on the invalidation nobody saw. Recording the
    mismatch at the moment of completion makes it attributable afterwards, which is the
    part that survives the agent walking away.

    `hold` is passed in because it has to be read before the status moves: the hold is
    about work in flight and goes quiet on `done`, so computing it here would always find
    nothing.

    Written as `evidence`, not a new column: this is a receipt about the work, which is
    exactly what that field already holds, and it travels wherever the item's evidence
    travels.
    """
    if hold is None:
        return
    item.evidence = (item.evidence or []) + normalize_evidence([{
        "kind": "note",
        "detail": (f"Completed against superseded intent: work started under "
                   f"{hold['started_against']}, the governing baseline is now "
                   f"{hold['baseline_version']}. Classify against the baseline in force "
                   f"when this was built, not the current one."),
    }])
    db.commit()


def _auto_extract_lessons(db: Session, item: Item) -> None:
    """On completion, distill lessons into memory shards (respects project.auto_extract)."""
    from app.models import Project
    from app.services import memory as memory_svc

    project = db.get(Project, item.project_id)
    if project is not None and not project.auto_extract:
        return
    # Skip if we've already extracted for this item.
    existing = [s for s in memory_svc.list_shards(db, project_id=item.project_id)
                if s.source == f"lesson from {item.id}"]
    if existing:
        return
    from app.services import platform as platform_svc

    try:
        lessons = platform_svc.extractor_for(db, item.project_id).extract(title=item.title, description=item.description)
    except Exception:
        return  # never let extraction failure block a status change
    for text in lessons:
        # Candidates for human review, not auto-trusted memory (AL-49).
        memory_svc.add_memory(
            db, text_body=text, scope="item", source=f"lesson from {item.id}",
            item_id=item.id, project_id=item.project_id, fresh=True,
            status="candidate", origin="agent:auto-extract",
        )


def reorder_items(db: Session, ordered_ids: list[str]) -> list[Item]:
    """Persist a new drag order. `ordered_ids` is the full desired top→bottom order."""
    for idx, iid in enumerate(ordered_ids):
        item = db.get(Item, keys.resolve_item(db, iid) or iid)
        if item is not None:
            item.sort_order = idx
    db.commit()
    return list_items(db)


def search_items(
    db: Session,
    query: str = "",
    status: str | None = None,
    limit: int = 25,
    project_id: str | None = None,
    tags: list[str] | None = None,
) -> list[Item]:
    # Status/project are simple column filters (SQL); the free-text query and tag
    # matching run in Python so `query` can match a tag too and stay dialect-agnostic
    # (tags is a JSON column). The result set here is small.
    stmt = select(Item)
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    if status:
        stmt = stmt.where(Item.status == status)
    rows = list(db.scalars(stmt.order_by(Item.sort_order.asc())).all())

    q = query.lower().strip()
    want_tags = {t.lower() for t in (tags or [])}

    def matches(it: Item) -> bool:
        item_tags = {t.lower() for t in (it.tags or [])}
        if want_tags and not (want_tags & item_tags):
            return False
        if q and q not in it.title.lower() and q not in (it.description or "").lower() and not any(
            q in t for t in item_tags
        ):
            return False
        return True

    return [it for it in rows if matches(it)][:limit]


def get_backlog(db: Session, limit: int = 20, project_id: str | None = None) -> list[Item]:
    stmt = select(Item).where(Item.status.in_(["backlog", "next"]))
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    stmt = stmt.order_by(Item.sort_order.asc()).limit(limit)
    return list(db.scalars(stmt).all())


def get_item_details(db: Session, item_id: str) -> dict | None:
    from app.models import MemoryShard, Request

    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        return None
    # Query by the RESOLVED id, never the caller's string. `item_id` may be any form
    # that resolves — a key rendered under the project's current tag, a retired tag, or
    # a pre-tag legacy id — while these columns hold the frozen stored id. Using the
    # caller's string made linked memory and requests silently vanish the moment a
    # project was retagged and an agent looked the item up by its new key (PRD-13).
    shards = db.scalars(select(MemoryShard).where(MemoryShard.item_id == item.id)).all()
    reqs = db.scalars(select(Request).where(Request.linked_to == item.id)).all()
    return {
        # Rendered, not stored — every other read surface renders, and this one didn't.
        "id": item.key,
        "title": item.title,
        "description": item.description,
        "status": item.status,
        "tags": item.tags,
        "effort": item.effort,
        "fidelity": item.fidelity,
        "blocker": item.blocker,
        "pr": item.pr,
        "linked_shards": [{"id": s.id, "text": s.text, "source": s.source} for s in shards],
        "linked_requests": [{"id": r.key, "title": r.title, "type": r.type} for r in reqs],
        # In-flight invalidation (GRPH-242/312). This is the read an agent makes right
        # before starting work, so it is the one place the hold most needs to appear — and
        # it was the one place it did not: this builds its own dict rather than going
        # through `_item_dict`, so "the hold rides on every item an agent reads" was true
        # of every surface except the most important one. Absent (not null) when there is
        # no hold, so it costs nothing on the overwhelming majority of reads.
        **({"intent_hold": hold} if (hold := _pending_hold(db, item)) else {}),
    }


def suggest_next(db: Session, project_id: str | None = None) -> Item | None:
    """The best item to start now: dependency-ready backlog/next, ranked by the composite
    priority score (status, unblocks-many, votes, effort, staleness)."""
    from app.services import prioritization as prio

    ranked = prio.prioritized(db, project_id, statuses=("next", "backlog"), include_blocked=False)
    return ranked[0]["item"] if ranked else None


# ---- Assignment / agent claiming (feature A) ----

def _aware(dt):
    """SQLite hands datetimes back naive; treat a naive value as UTC for comparisons."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_claimable(it: Item, cutoff) -> bool:
    """An item is claimable if it isn't blocked and is either fresh unclaimed backlog/next,
    or work with a stale (abandoned) lease."""
    if it.blocker:
        return False
    stale = it.claimed_by is not None and it.claimed_at is not None and _aware(it.claimed_at) < cutoff
    if stale:
        return it.status in ("next", "backlog", "in_progress")
    if it.claimed_by is None:
        return it.status in ("next", "backlog")
    return False  # someone holds a live lease


def _ready_candidates(db: Session, project_id: str | None, lease_seconds: int) -> list[Item]:
    from app.services import prioritization as prio

    ctx = prio.context(db, project_id)
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    out = []
    for it in ctx.items:
        if not _is_claimable(it, cutoff):
            continue
        # Fresh backlog/next must be dependency-ready; a stale in-progress reclaim is already
        # underway, so we don't re-gate it on dependencies.
        if it.status in ("backlog", "next") and not prio.ready(ctx, it):
            continue
        out.append(it)
    out.sort(key=lambda it: (-prio.score(ctx, it), it.sort_order))
    return out


def claim_next(
    db: Session, agent_id: str, project_id: str | None = None, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> Item | None:
    """Atomically assign the best ready item to `agent_id` and move it to in_progress.

    Concurrency-safe: the UPDATE guard means only one caller wins a given row, so two agents
    never claim the same item. Returns the claimed item, or None if nothing is ready.
    """
    for cand in _ready_candidates(db, project_id, lease_seconds):
        claimed = _try_claim(db, cand, agent_id)
        if claimed is not None:
            return claimed
        # Lost the race for this candidate — try the next.
    return None


def _try_claim(db: Session, cand: Item, agent_id: str) -> Item | None:
    """Atomically claim `cand` for `agent_id`. Optimistic-concurrency guard: only win the row
    if `claimed_by` is still what we observed (None for fresh, the stale holder for a reclaim),
    so two agents never take the same item. Dialect-safe — no time math in SQL."""
    stmt = update(Item).where(Item.id == cand.id)
    stmt = (
        stmt.where(Item.claimed_by.is_(None))
        if cand.claimed_by is None
        else stmt.where(Item.claimed_by == cand.claimed_by)
    )
    stmt = stmt.values(claimed_by=agent_id, claimed_at=utcnow(), assignee=agent_id, status="in_progress")
    if db.execute(stmt).rowcount == 1:
        db.commit()
        db.expire_all()
        item = db.get(Item, cand.id)
        stamp_baseline_at_start(db, item)
        return item
    db.commit()
    return None


def stamp_baseline_at_start(db: Session, item: Item) -> None:
    """Record which agreed intent this item's work started against (GRPH-242).

    Written once and never overwritten: the question it answers is "what was agreed when
    work began", and restamping on a later claim would erase exactly the mismatch the
    in-flight hold is derived from. An item with no PRD, or on a PRD with no baseline, has
    no agreed intent to have started against and stays NULL.
    """
    if item is None or item.baseline_at_claim or not item.prd_id:
        return
    from app.services import prds as prd_svc  # local: prds imports this module

    base = prd_svc.baseline_of(db, item.prd_id)
    if base is None:
        return
    item.baseline_at_claim = base.version
    db.commit()


def claim_item(db: Session, item_id: str, agent_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Item | None:
    """Claim one specific item if it's currently claimable. Used to grab a related cluster."""
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    it = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if it is None or not _is_claimable(it, cutoff):
        return None
    return _try_claim(db, it, agent_id)


def heartbeat(db: Session, item_id: str, agent_id: str) -> Item | None:
    """Extend the lease on a claimed item. Returns the item, or None if the agent isn't the holder."""
    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None or item.claimed_by != agent_id:
        return None
    item.claimed_at = utcnow()
    db.commit()
    db.refresh(item)
    return item


def release_item(db: Session, item_id: str, agent_id: str, to_status: str = "next") -> Item | None:
    """Give a claimed item back to the queue. Returns the item, or None if not the holder."""
    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None or item.claimed_by != agent_id:
        return None
    item.claimed_by = None
    item.claimed_at = None
    item.assignee = ""
    if item.status == "in_progress" and to_status in STATUSES:
        item.status = to_status
    db.commit()
    db.refresh(item)
    return item
