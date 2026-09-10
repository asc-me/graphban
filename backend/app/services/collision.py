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


def _reasons(a: list[str], b: list[str]) -> list[dict]:
    """The touchpoint pairs that relate two items, and which rule related each (GRPH-810).

    Every matching pair rather than the first, because "they share a directory" and "they
    name the same file" are different situations and a cluster may rest on both. Capped: a
    reason nobody reads is not a reason, and two items with fifty overlapping paths make the
    point in three.
    """
    out: list[dict] = []
    for x in a or []:
        for y in b or []:
            rule = clustering.why_match(x, y)
            if rule:
                out.append({"a": x, "b": y, "rule": rule})
                if len(out) >= 3:
                    return out
    return out


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

    # WHY each cluster is a cluster (GRPH-810). Recorded here because the union-find merges
    # ARE the minimal explanation: a cluster of n members is built from at most n-1 of them,
    # while the pairwise comparisons below are quadratic. Storing the comparisons would be a
    # wall of text; storing the merges is the reason, and nothing else.
    #
    # It matters because the answer is often "these two are files in the same directory",
    # which is a defensible clustering heuristic and a costly reservation rule — and until
    # now there was no way to tell which of the two you were looking at.
    merges: list[dict] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if find(a) == find(b):
                # Already together, by transitivity. NOT recorded: the merge that put them
                # there is already in the list, and adding this pair would suggest a second
                # independent reason where there is one.
                continue
            reasons = _reasons(areas[a], areas[b])
            if not reasons:
                continue
            merges.append({"items": sorted((a, b)), "on": reasons})
            parent[find(a)] = find(b)

    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)

    clusters = [
        {
            "items": members,
            "areas": sorted({a for m in members for a in areas[m]}),
            "collides": len(members) > 1,
            "predicted": any(predicted[m] for m in members),
            # Empty for a single-item cluster, which merged with nothing.
            "because": [m for m in merges if set(m["items"]) <= set(members)],
        }
        for members in groups.values()
    ]
    clusters.sort(key=lambda c: (-len(c["items"]), c["items"][0]))
    return clusters


def clusters_for_project(db: Session, project_id: str | None, status: str | None = None,
                         lease_seconds: int = items_svc.DEFAULT_LEASE_SECONDS,
                         prd_id: str | None = None) -> list[dict]:
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
    if prd_id:
        # GRPH-797. `until` had no way to say which work a wave was for, so it drained the
        # project: a run meant for one PRD delegated an epic and three unrelated items. Parking
        # things in `backlog` is not a defence, because backlog is claimable BY DESIGN — that
        # is GRPH-397, and it is right. The lever has to be here.
        #
        # Filtering the POOL rather than the finished clusters is the whole point: a cluster is
        # a promise that its members do not collide, and dropping members from one afterwards
        # would hand out a promise computed over items that are no longer in it.
        pool = [it for it in pool if (it.prd_id or "") == prd_id]
    return _with_reservations(db, collision_clusters(db, pool, project_id), project_id,
                             lease_seconds=lease_seconds)


def _with_reservations(db: Session, clusters: list[dict], project_id: str | None,
                       *, lease_seconds: int) -> list[dict]:
    """Mark each cluster whose AREAS are reserved, and say when the earliest frees (GRPH-803).

    An item's lease and its areas are different holds, and the pool only knew about the first.
    `claimable` excludes an item somebody has claimed; it says nothing about an item nobody
    holds whose files are reserved by an agent working something else. So every cluster read
    as free, a wave spawned a child for each, and each was refused by `claim_cluster` on
    arrival — one real run burned all ten children and minted nothing.

    `_delegate_next` has always skipped clusters with `held_by`. Nothing ever set it, so that
    guard has never once fired: a check that reads as protection and is not.

    The free-at time is here because "wait" and "give up" are different instructions, and the
    supervisor cannot tell them apart without it.
    """
    from datetime import timezone

    from app.services import fleet as fleet_svc

    now = items_svc.utcnow()
    taken = fleet_svc.active_reservations(db, project_id, now=now)
    if not taken:
        return clusters
    for cluster in clusters:
        areas = list(cluster.get("areas") or [])
        holders, soonest = set(), None
        blocking: list[dict] = []
        for row in taken:
            overlap = fleet_svc.areas_collide(areas, [row.area])
            if not overlap:
                continue
            holders.add(row.agent_id)
            if len(blocking) < 3:
                # WHICH of this cluster's areas the hold covers, and by which rule (GRPH-833).
                # `because` above explains why the cluster's own members merged; this explains
                # why the whole cluster is unavailable, which is the question an operator
                # staring at an idle fleet is actually asking. Capped for the reason `because`
                # is: a reason nobody reads is not a reason.
                mine = overlap[0]
                blocking.append({
                    "area": mine, "reserved": row.area, "by": row.agent_id,
                    "rule": clustering.why_match(mine, row.area) or "prefix",
                })
            expires = row.expires_at
            if expires is not None:
                expires = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
                soonest = expires if soonest is None else min(soonest, expires)
        if holders:
            cluster["held_by"] = sorted(holders)
            cluster["free_in"] = (max(0, int((soonest - now).total_seconds()))
                                  if soonest is not None else None)
            cluster["held_because"] = blocking
    return clusters


def _unexpired(db: Session, project_id: str | None, *, now):
    """Reservations whose clock has not run out, offline holders included.

    Deliberately NOT `active_reservations`, which is the one the divvy asks and which drops a
    dead holder's rows so they stop blocking. This is the diagnostic view: a row that exists
    and is being ignored is exactly what an operator staring at an idle fleet needs to see,
    and the two functions disagreeing is the answer rather than a bug.
    """
    from datetime import timezone

    from app.models import AreaReservation
    from sqlalchemy import select as _select

    rows = db.scalars(_select(AreaReservation)).all()
    out = []
    for r in rows:
        expires = r.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires is None or expires > now:
            out.append(r)
    if project_id:
        from app.models import Item as _Item

        item_ids = {i.id for i in db.scalars(
            _select(_Item).where(_Item.project_id == project_id)).all()}
        out = [r for r in out if r.item_id in item_ids]
    return out


def _holder_state(fleet_svc, agent, now) -> str:
    """`working`/`idle`/`reviewing`, `offline`, or `retired` — one definition, two readers.

    `retired` is a seat that can never register again, so its hold will never be released by
    the holder coming back; `offline` will lapse on its lease, and waiting is reasonable. The
    two look identical from a cluster and call for different moves.

    `unknown` is defensive and, today, UNREACHABLE: `AreaReservation.agent_id` is a
    non-nullable foreign key, so every reservation has an agent row. It is left in so that
    making that column nullable later cannot make a holderless reservation read as live —
    which is the direction that matters. Said here rather than tested, because a test for a
    state the schema forbids would assert against a fixture rather than against the system.
    """
    if agent is None:
        return "unknown"
    if fleet_svc.retired(agent, now=now):
        return "retired"
    return fleet_svc.presence_state(agent, now=now)


def holds(db: Session, project_id: str | None) -> list[dict]:
    """Every live area reservation: what is held, by whom, and until when (GRPH-833).

    **The missing surface, and the cost of not having it was a wrong conclusion rather than a
    slow one.** Mid-wave, with full repository access, the item touchpoints in hand and time to
    think, the operator decided an item was being wrongly held and inferred directory-level
    clustering from the symptom. A later spawn into a genuinely disjoint cluster disproved it.
    `--max-workers 3` yielding one running child is a symptom anybody can see; why, was not
    readable anywhere.

    Clusters already carry `held_by` and `free_in` (GRPH-803) and that was not enough, because
    a cluster is only in the pool while its items are CLAIMABLE. The moment an item is claimed
    its cluster leaves the partition, and its reservation — which is still blocking everybody
    else — is visible on no read at all. This one is keyed on the reservation, so a hold is
    listed whether or not the work it belongs to is still on offer.

    `holder_state` is the field that answers the actual question. "Held by SA-A39" invites the
    reader to go and find SA-A39; "held by SA-A39, which is offline, frees in 412s" IS the
    diagnosis, and it is the sentence the operator spent a wave not having.
    """
    from datetime import timezone

    from app.models import Agent, Item
    from app.services import fleet as fleet_svc

    now = items_svc.utcnow()
    # EVERY unexpired reservation, not `active_reservations` — and that difference is the
    # point rather than an oversight. `active_reservations` already drops a holder the roster
    # calls offline (GRPH-808), so a row this function fetched through it could only ever
    # report a live holder, and `holder_state` would have had a branch that can never fire:
    # a field that reads as protection and is not, which is the shape this repository keeps
    # finding. Listing the ignored rows AND saying they are ignored is what turns "there is a
    # reservation on this path" from a puzzle into a sentence.
    rows = _unexpired(db, project_id, now=now)
    _blocking_rows = fleet_svc.active_reservations(db, project_id, now=now)
    out: list[dict] = []
    for row in sorted(rows, key=lambda r: (r.agent_id or "", r.area or "")):
        agent = db.get(Agent, row.agent_id) if row.agent_id else None
        item = db.get(Item, row.item_id) if row.item_id else None
        expires = row.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        out.append({
            "area": row.area,
            "agent_id": row.agent_id,
            # Three states, not two. `retired` is a seat that can never register again, so its
            # hold will never be released by the holder returning — which is a different
            # instruction from `offline`, where waiting is the right move (GRPH-814).
            "holder_state": _holder_state(fleet_svc, agent, now),
            # Whether this row is actually keeping anybody out. A reservation held by an
            # offline agent still EXISTS — it is simply ignored — and an operator who can see
            # the row but not that fact goes looking for a collision that is not happening.
            "blocking": any(r.id == row.id for r in _blocking_rows),
            "item": item.key if item is not None else row.item_id,
            # Declared or inferred. A predicted area is a guess about files nobody listed, and
            # an operator deciding whether a hold is legitimate needs to know which it is.
            "predicted": bool(row.predicted),
            "free_in": (max(0, int((expires - now).total_seconds()))
                        if expires is not None else None),
        })
    return out
