"""Observe Live board (PRD-33 D5): one aggregation over facts Graphban already writes.

Composes `fleet.list_agents` and `fleet.held_areas`. Does not query Agent in the
router, does not fetch GitHub, does not invent a sixth phase. A missing
measurement is a named third state: unreserved / unrecorded / unattributed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, ApiKey, Item, User
from app.services import fleet as fleet_svc
from app.services import items as items_svc

# D9: hard cap on agents in the payload. Census (`user_counts`) is still the
# full set. No number in the PRD; match presence's board size, not a page.
AGENT_CAP = fleet_svc.PRESENCE_CAP


def _pr_url(item: Item | None) -> str | None:
    """D4: first recorded URL wins. Graphban does not fetch a forge."""
    if item is None:
        return None
    pr = item.pr
    if isinstance(pr, dict):
        for v in pr.values():
            if isinstance(v, str) and items_svc.is_pr_url(v):
                return v
        for v in pr.values():
            if isinstance(v, str) and v.startswith("http"):
                return v
    if item.github_url and items_svc.is_pr_url(item.github_url):
        return item.github_url
    for e in item.evidence or []:
        if isinstance(e, dict) and items_svc.is_pr_url(e.get("url") or ""):
            return e["url"]
    return None


def _holding_pr(item: Item | None) -> dict:
    url = _pr_url(item)
    if url:
        return {"state": "recorded", "url": url}
    return {"state": "unrecorded"}


def _files_for(agent_id: str, areas: dict) -> list[dict]:
    files: list[dict] = []
    for row in areas.get("held") or []:
        if row.get("agent_id") != agent_id:
            continue
        kind = "predicted" if row.get("predicted") else "leased"
        files.append({
            "area": row.get("area") or "",
            "kind": kind,
            "reason": None,
            "node_paths": row.get("node_paths") or [],
        })
    for row in areas.get("off_map") or []:
        if row.get("agent_id") != agent_id:
            continue
        files.append({
            "area": row.get("area") or "",
            "kind": "off_map",
            "reason": row.get("reason"),
            "node_paths": row.get("node_paths") or [],
        })
    return files


def _file_state(agent_state: str, holdings: list, files: list[dict]) -> str:
    """D3/D16: dominant kind. offline > leased > predicted > off_map > unreserved > idle."""
    if agent_state == "offline":
        return "offline"
    kinds = {f["kind"] for f in files}
    for k in ("leased", "predicted", "off_map"):
        if k in kinds:
            return k
    if holdings:
        return "unreserved"
    return "idle"


def _user_meta(user: User | None) -> dict:
    if user is None:
        return {"user_id": None, "label": "Unattributed", "initials": "", "color": None}
    return {
        "user_id": user.id,
        "label": user.name or user.handle or user.id,
        "initials": user.initials or "",
        "color": user.avatar,
    }


def _is_online(state: str) -> bool:
    return state != "offline"


def _sort_agents(agents: list[dict]) -> list[dict]:
    """Online before offline, then last_seen_at descending, nulls last (D17)."""
    def split(online: bool) -> list[dict]:
        xs = [a for a in agents if _is_online(a["state"]) is online]
        stamped = [a for a in xs if a.get("last_seen_at")]
        missing = [a for a in xs if not a.get("last_seen_at")]
        stamped.sort(key=lambda a: a["last_seen_at"], reverse=True)
        return stamped + missing
    return split(True) + split(False)


def board(db: Session, project_id: str, *, user_filter: str | None = None,
          viewer_id: str | None = None, cap: int = AGENT_CAP,
          list_agents=None, held_areas=None) -> dict:
    """One Live payload. Inject list_agents/held_areas only in tests (D14 CALL)."""
    list_fn = list_agents if list_agents is not None else fleet_svc.list_agents
    areas_fn = held_areas if held_areas is not None else fleet_svc.held_areas

    roster = list_fn(db, project_id)
    areas = areas_fn(db, project_id)

    live = [a for a in roster if not a.get("dismissed")]
    total_agents = len(live)

    keys = {k.id: k for k in db.scalars(select(ApiKey)).all()}
    users = {u.id: u for u in db.scalars(select(User)).all()}
    agent_rows = {
        row.id: row
        for row in db.scalars(select(Agent).where(Agent.project_id == project_id)).all()
    }

    stored_ids = [h.get("stored_id") for a in live for h in (a.get("holdings") or []) if h.get("stored_id")]
    items: dict[str, Item] = {}
    if stored_ids:
        for it in db.scalars(select(Item).where(Item.id.in_(stored_ids))).all():
            items[it.id] = it

    annotated: list[dict] = []
    for a in live:
        row = agent_rows.get(a["id"])
        key = keys.get(row.api_key_id) if row is not None and row.api_key_id else None
        user = users.get(key.user_id) if key is not None and key.user_id else None
        files = _files_for(a["id"], areas)
        holdings_in = a.get("holdings") or []
        holdings = [{
            "id": h.get("id"),
            "title": h.get("title"),
            "status": h.get("status"),
            "phase": h.get("phase"),
            "phase_basis": h.get("phase_basis"),
            "pr": _holding_pr(items.get(h.get("stored_id"))),
        } for h in holdings_in]
        state = a.get("state") or "offline"
        annotated.append({
            "id": a["id"],
            "key": a.get("key"),
            "label": a.get("label") or "",
            "role": a.get("active_role"),
            "state": state,
            "last_seen_at": a.get("last_seen_at"),
            "worktree": a.get("worktree"),
            "branch": a.get("branch"),
            "branch_orphaned": a.get("branch_orphaned"),
            "file_state": _file_state(state, holdings, files),
            "files": files,
            "holdings": holdings,
            "_user": _user_meta(user),
        })

    groups: dict = {}
    for a in annotated:
        meta = a.pop("_user")
        uid = meta["user_id"]
        g = groups.get(uid)
        if g is None:
            g = {**meta, "online": 0, "total": 0, "agents": []}
            groups[uid] = g
        g["agents"].append(a)
        g["total"] += 1
        if _is_online(a["state"]):
            g["online"] += 1
    for g in groups.values():
        g["agents"] = _sort_agents(g["agents"])

    def user_sort_key(uid):
        if uid is None:
            return (2, "")
        if viewer_id and uid == viewer_id:
            return (0, "")
        return (1, (groups[uid]["label"] or "").lower())

    ordered_ids = sorted(groups.keys(), key=user_sort_key)
    user_counts = [
        {"user_id": groups[uid]["user_id"], "label": groups[uid]["label"],
         "online": groups[uid]["online"], "total": groups[uid]["total"]}
        for uid in ordered_ids
    ]
    unattributed_count = groups[None]["total"] if None in groups else 0

    filt = (user_filter or "").strip() or None
    if filt == "unattributed":
        keep = [None] if None in groups else []
    elif filt:
        keep = [filt] if filt in groups else []
    else:
        keep = list(ordered_ids)

    remaining = cap
    users_out = []
    payload_agents = 0
    for uid in keep:
        g = groups[uid]
        take = g["agents"][:max(remaining, 0)]
        remaining -= len(take)
        payload_agents += len(take)
        users_out.append({
            "user_id": g["user_id"],
            "label": g["label"],
            "initials": g["initials"],
            "color": g["color"],
            "online": g["online"],
            "total": g["total"],
            "agents": take,
        })
        if remaining <= 0:
            break

    return {
        "served_at": datetime.now(timezone.utc).isoformat(),
        "heartbeat_interval_seconds": fleet_svc.heartbeat_interval_seconds(),
        "presence_ttl_seconds": fleet_svc.presence_ttl_seconds(),
        "truncated": total_agents > payload_agents,
        "total_agents": total_agents,
        "unattributed_count": unattributed_count,
        "users": users_out,
        "user_counts": user_counts,
    }
