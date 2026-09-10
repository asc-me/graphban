"""Code-locality clustering (feature B).

Relate items by shared *touchpoints* (files / globs / modules they affect) plus existing
typed links, so an agent can pick up a whole code-neighborhood in one pass instead of
context-switching. Also auto-populates the `code` link type from touchpoint overlap.
"""
from __future__ import annotations

import fnmatch

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Item, Link
from app.services import items as items_svc


def why_match(x: str, y: str) -> str:
    """WHICH rule relates two touchpoints, or "" for none (GRPH-810).

    The rule name is the whole point. "These two items were serialised" is not actionable;
    "they were serialised because they are files in the same directory" is — a reader can
    look at the two paths and say whether that is true of the work.

    `directory` is the broad one, and it was the one nobody could see. Every pair of files in
    one directory relates, whatever they are, so a directory of five files collapses five items
    into one cluster. That is a defensible clustering heuristic and a costly reservation rule,
    and until this it was impossible to tell which of the two you were looking at.
    """
    x, y = x.strip(), y.strip()
    if not x or not y:
        return ""
    if x == y:
        return "exact"
    if ("*" in x and fnmatch.fnmatch(y, x)) or ("*" in y and fnmatch.fnmatch(x, y)):
        return "glob"
    if "/" in x and "/" in y and x.rsplit("/", 1)[0] == y.rsplit("/", 1)[0]:
        return "directory"
    return ""


def _match(x: str, y: str) -> bool:
    """Two touchpoints relate if equal, one glob-matches the other, or they share a directory.

    One definition, expressed through `why_match`: two functions answering "do these relate?"
    is how the reservation check and the partition came to disagree in the first place.
    """
    return bool(why_match(x, y))


def shared_touchpoints(a: list[str], b: list[str]) -> list[str]:
    """Touchpoints of `a` that relate to any touchpoint of `b`."""
    return sorted({x for x in (a or []) if any(_match(x, y) for y in (b or []))})


def _links_for(db: Session, item_id: str) -> dict[str, set[str]]:
    """other_item_id -> {link types} for links touching item_id."""
    rows = db.scalars(select(Link).where(or_(Link.a == item_id, Link.b == item_id))).all()
    out: dict[str, set[str]] = {}
    for lk in rows:
        other = lk.b if lk.a == item_id else lk.a
        out.setdefault(other, set()).add(lk.type)
    return out


def related_items(db: Session, item: Item, project_id: str | None) -> list[dict]:
    """Items related to `item` by shared touchpoints and/or existing links, best-first.

    Returns [{item, score, shared, link_types}]. Touchpoint overlap weighs most; a dependency
    link adds a strong base, any other link a small one.
    """
    a_tps = item.touchpoints or []
    links = _links_for(db, item.id)
    out = []
    for other in items_svc.list_items(db, project_id=project_id):
        if other.id == item.id:
            continue
        shared = shared_touchpoints(a_tps, other.touchpoints or [])
        types = links.get(other.id, set())
        if not shared and not types:
            continue
        score = len(shared) * 2 + (2 if "dependency" in types else 0) + (1 if types else 0)
        out.append({"item": other, "score": score, "shared": shared, "link_types": sorted(types)})
    out.sort(key=lambda r: (-r["score"], r["item"].sort_order))
    return out


def next_cluster(
    db: Session, agent_id: str, project_id: str | None, max_items: int = 3, lease_seconds: int | None = None,
) -> list[dict]:
    """Claim the best ready item, then claim its related *ready* neighbours (up to max_items),
    assigning the whole cluster to `agent_id`. Returns [{item, shared, link_types}] for each
    claimed item — the seed first. Empty list when nothing is ready.
    """
    lease = lease_seconds or items_svc.DEFAULT_LEASE_SECONDS
    seed = items_svc.claim_next(db, agent_id, project_id=project_id, lease_seconds=lease)
    if seed is None:
        return []
    batch = [{"item": seed, "shared": [], "link_types": [], "seed": True}]
    for rel in related_items(db, seed, project_id):
        if len(batch) >= max_items:
            break
        try:
            claimed = items_svc.claim_item(db, rel["item"].id, agent_id, lease_seconds=lease)
        except (items_svc.OutOfScope, items_svc.ReachesOutsideTheRepo):
            # A neighbour outside the seat's scope (GRPH-827). Skipped rather than fatal: the
            # seed was claimable and IS the work, and a related item in another PRD is exactly
            # what a scoped wave should decline to drag in. The seed cannot hit this — it came
            # through `claim_next`, which filters. GRPH-832 rides the same path for the same
            # reason: a neighbour that acts on a running system is dragged in by nothing.
            continue
        if claimed is not None:
            batch.append({"item": claimed, "shared": rel["shared"], "link_types": rel["link_types"], "seed": False})
    return batch


def sync_code_links(db: Session, item: Item, project_id: str | None = None) -> int:
    """Ensure a `code` link exists between `item` and every item sharing a touchpoint.
    Idempotent — skips pairs that already have any link. Returns the number created."""
    if not item.touchpoints:
        return 0
    from app.services import links as links_svc

    pid = project_id or item.project_id
    already = set(_links_for(db, item.id).keys())
    created = 0
    for other in items_svc.list_items(db, project_id=pid):
        if other.id == item.id or other.id in already:
            continue
        shared = shared_touchpoints(item.touchpoints, other.touchpoints or [])
        if shared:
            links_svc.create_link(
                db, a=item.id, b=other.id, type_="code",
                reason="shares " + ", ".join(shared[:3]),
                confidence=min(1.0, 0.4 + 0.2 * len(shared)), project_id=pid,
            )
            created += 1
    return created
