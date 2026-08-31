"""Collision-aware clustering (PRD-10 v2 core, AL-192).

Partition a set of work items into NON-colliding clusters so concurrently-assigned items
don't touch the same code. Two items *collide* when their code touch-areas overlap; a
cluster is a connected component of the collision graph — items within one component should
go to a single agent (or be serialized), while distinct components are safe to run in
parallel (one Grok Build worktree each, AL-201).

Touch-areas come from `touch_areas`: an item's own touchpoints when set (human/actual —
highest confidence), else PREDICTED — code-map semantic inference (`search_code`) plus the
touchpoints of items linked to it (learned patterns). Measured paths **union** with
declared ones (`update_item` / P30 D10); they do not replace, and an empty write is not
a write — wiping the list would read as "no collision". Overlap uses the same
glob/dir-aware match as code-locality clustering (`clustering.shared_touchpoints`).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Item
from app.services import clustering
from app.services import code_graph
from app.services import items as items_svc

_PREDICT_TOP_K = 5
_PREDICT_MIN_SIM = 0.15  # ignore weak semantic matches when inferring touch-areas


def predict_touch_areas(db: Session, item: Item, project_id: str | None,
                        top_k: int = _PREDICT_TOP_K) -> list[str]:
    """Predict likely-touched paths for an item that has no touchpoints yet, from two
    signals: code-map semantic inference (nearest code nodes to the item's text) and
    learned patterns (touchpoints of items linked to this one). Deduped, sorted."""
    areas: set[str] = set()

    # inference: the nearest code nodes to the item's title + description
    text = f"{item.title} {item.description or ''}".strip()
    if text:
        for node, sim in code_graph.search_code(db, text, project_id, top_k=top_k):
            if sim >= _PREDICT_MIN_SIM and node.path:
                areas.add(node.path)

    # learned: touchpoints of items already linked to this one (dependency/code/semantic)
    for rel in clustering.related_items(db, item, project_id):
        areas.update(rel["item"].touchpoints or [])

    return sorted(areas)


def touch_areas(db: Session, item: Item, project_id: str | None) -> tuple[list[str], str]:
    """(areas, source): the item's own touchpoints when set (`actual`), else `predicted`."""
    if item.touchpoints:
        return list(item.touchpoints), "actual"
    return predict_touch_areas(db, item, project_id), "predicted"


def collision_clusters(db: Session, items: list[Item], project_id: str | None) -> list[dict]:
    """Group `items` into non-colliding clusters by touch-area overlap.

    Returns clusters (largest first), each:
      {items: [id...], areas: [path...], collides: bool, predicted: bool}
    where `collides` marks a multi-item cluster (assign together / serialize) and
    `predicted` marks a cluster whose grouping leaned on inferred (not actual) areas —
    lower confidence, a candidate for human tag-correction.
    """
    areas: dict[str, list[str]] = {}
    predicted: dict[str, bool] = {}
    for it in items:
        a, src = touch_areas(db, it, project_id)
        areas[it.id] = a
        predicted[it.id] = src == "predicted"

    ids = [it.id for it in items]
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if clustering.shared_touchpoints(areas[ids[i]], areas[ids[j]]):
                parent[find(ids[i])] = find(ids[j])

    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)

    clusters = [
        {
            "items": members,
            "areas": sorted({a for m in members for a in areas[m]}),
            "collides": len(members) > 1,
            "predicted": any(predicted[m] for m in members),
        }
        for members in groups.values()
    ]
    clusters.sort(key=lambda c: (-len(c["items"]), c["items"][0]))
    return clusters


def clusters_for_project(db: Session, project_id: str | None, status: str | None = None,
                         lease_seconds: int = items_svc.DEFAULT_LEASE_SECONDS) -> list[dict]:
    """Collision clusters over a project's work pool. Defaults to everything an agent could
    actually take right now — which is NOT the same as the unstarted pool.

    It used to be `status in ("backlog", "next")`, and that quietly excluded ABANDONED work.
    An item whose holder died stays `in_progress`, because the lease expires lazily and nothing
    rewrites the row; `claim_next` reclaims it happily, and the divvy could not see it at all.
    Once `claim_cluster` became the path every posture is taught, that meant a crashed agent's
    item was never offered to anybody again (GRPH-397).

    Sharing `items_svc.claimable` is the point: two definitions of "claimable" is what produced
    the gap, and a triage board should show the same work the claim path will hand out.

    (Dependency readiness stays a `claim_next` filter, as it always has — a blocked item can
    still appear in the partition a planner reads.)
    """
    pool = items_svc.list_items(db, project_id=project_id, status=status)
    if status is None:
        pool = [it for it in pool if items_svc.claimable(it, lease_seconds=lease_seconds)]
    return collision_clusters(db, pool, project_id)
