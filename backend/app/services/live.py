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
from app.services import agent_calls as calls_svc
from app.services import fleet as fleet_svc
from app.services import items as items_svc

# D9: hard cap on agents in the payload. Census (`user_counts`) is still the
# full set. No number in the PRD; match presence's board size, not a page.
AGENT_CAP = fleet_svc.PRESENCE_CAP
# PRD-34 D22: `calls_in_window` counts over ten heartbeat intervals. The presence TTL is
# three and too short to count anything; `call_state` (D21) uses one.
CALL_WINDOW_INTERVALS = 10


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
    """D3/D16: dominant kind. offline > leased > predicted > off_map > unreserved > idle.

    `declared` is PR 3 polish on unreserved only — it must not win this table.
    """
    if agent_state == "offline":
        return "offline"
    kinds = {f["kind"] for f in files}
    for k in ("leased", "predicted", "off_map"):
        if k in kinds:
            return k
    if holdings:
        return "unreserved"
    return "idle"


def _declared_files(holdings_in: list, items: dict[str, Item]) -> list[dict]:
    """Item.touchpoints on an unreserved agent. Labelled declared, never leased."""
    seen: set[str] = set()
    out: list[dict] = []
    for h in holdings_in:
        it = items.get(h.get("stored_id") or "")
        if it is None:
            continue
        for area in it.touchpoints or []:
            if not isinstance(area, str) or not area or area in seen:
                continue
            seen.add(area)
            out.append({
                "area": area,
                "kind": "declared",
                "reason": None,
                "node_paths": [],
            })
    return out


def _reported_files(row: Agent | None) -> list[dict]:
    """What the agent SAYS it is editing (PRD-34 D7). Kind `reported`, never `leased`, and
    never in the D3/D16 priority table — a claim does not move `file_state`."""
    if row is None:
        return []
    return [{"area": f, "kind": "reported", "reason": None, "node_paths": []}
            for f in (row.status_files or []) if isinstance(f, str) and f]


def _status_fields(row: Agent | None, *, ttl: int) -> dict:
    """PRD-34 D6/D11: `unreported` is a word; a stale report says so and is not current."""
    if row is None or not row.status_at:
        return {"status": None, "status_state": "unreported"}
    state = fleet_svc.status_state(row, ttl_seconds=ttl)
    at = row.status_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return {
        "status": {"text": row.status_text or "", "files": list(row.status_files or []),
                   "at": at.isoformat(), "stale": state == "stale"},
        "status_state": state,
    }


def _by_role(annotated: list[dict]) -> dict[str, int]:
    """Full-set census, same shape as fleet_status.by_role (D2, PR 3)."""
    counts = {r: 0 for r in fleet_svc.ROLES}
    counts[fleet_svc.ALL_IN_ONE] = 0
    for a in annotated:
        role = a.get("role") or fleet_svc.ALL_IN_ONE
        if role not in counts:
            counts[role] = 0
        counts[role] += 1
    return counts


def _call_fields(summary: dict | None) -> dict:
    """PRD-34 D7/D11: an agent the summary did not name is `never`, not a missing key."""
    if not summary:
        return {"last_call": None, "calls_in_window": 0, "silence_seconds": None,
                "call_state": "never"}
    return {
        "last_call": summary.get("last_call"),
        "calls_in_window": int(summary.get("calls_in_window") or 0),
        "silence_seconds": summary.get("silence_seconds"),
        "call_state": summary.get("call_state") or "never",
    }


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
          list_agents=None, held_areas=None, call_summary=None) -> dict:
    """One Live payload. Inject list_agents/held_areas/call_summary only in tests (D14 CALL)."""
    list_fn = list_agents if list_agents is not None else fleet_svc.list_agents
    areas_fn = held_areas if held_areas is not None else fleet_svc.held_areas
    summary_fn = call_summary if call_summary is not None else calls_svc.summary
    interval = fleet_svc.heartbeat_interval_seconds()
    window = interval * CALL_WINDOW_INTERVALS
    ttl = fleet_svc.presence_ttl_seconds()

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

    # PRD-34 D6/D19: two statements for the whole board, never one per agent. The board
    # composes; it does not query the feed table itself (PRD-33 A12 extended, A16).
    calls = summary_fn(db, project_id, [a["id"] for a in live],
                       window_seconds=window, interval_seconds=interval)
    per_agent = calls.get("agents") or {}
    unattributed_by_key = calls.get("unattributed") or {}

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
        file_state = _file_state(state, holdings, files)
        if file_state == "unreserved":
            files = files + _declared_files(holdings_in, items)
        # Reported files ride along whatever the lease state says; they are labelled and do
        # not change it (PRD-34 D7). Skip any path already shown as a lease.
        shown = {f["area"] for f in files}
        files = files + [f for f in _reported_files(row) if f["area"] not in shown]
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
            "parent_agent_id": (row.parent_agent_id if row is not None else None)
                or a.get("parent_agent_id"),
            "file_state": file_state,
            "files": files,
            "holdings": holdings,
            # The feed summary (PRD-34 D6). `never` is a word, not a null; the reported
            # status is `unreported` until PR 2 gives heartbeat something to carry.
            **_call_fields(per_agent.get(a["id"])),
            **_status_fields(row, ttl=ttl),
            "_user": _user_meta(user),
            "_key_id": row.api_key_id if row is not None else None,
        })

    groups: dict = {}
    for a in annotated:
        meta = a.pop("_user")
        a.pop("_key_id", None)
        uid = meta["user_id"]
        g = groups.get(uid)
        if g is None:
            g = {**meta, "online": 0, "total": 0, "agents": [],
                 "unattributed_calls": 0, "unattributed_by_key": []}
            groups[uid] = g
        g["agents"].append(a)
        g["total"] += 1
        if _is_online(a["state"]):
            g["online"] += 1

    # PRD-34 D3/D15: calls that named no agent are counted on the credential's owner, by
    # key name, so the operator knows which harness to fix. A key with such calls and NO
    # registered agent still gets a group — an empty roster with a non-zero count is the
    # honest picture of "this credential is calling but never registered".
    for key_id, n in unattributed_by_key.items():
        key = keys.get(key_id)
        user = users.get(key.user_id) if key is not None and key.user_id else None
        meta = _user_meta(user)
        uid = meta["user_id"]
        g = groups.get(uid)
        if g is None:
            g = {**meta, "online": 0, "total": 0, "agents": [],
                 "unattributed_calls": 0, "unattributed_by_key": []}
            groups[uid] = g
        g["unattributed_calls"] += int(n)
        g["unattributed_by_key"].append(
            {"key": (key.name if key is not None else None) or key_id, "calls": int(n)})
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
            "unattributed_calls": g["unattributed_calls"],
            "unattributed_by_key": g["unattributed_by_key"],
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
        "by_role": _by_role(annotated),
        "roles": list(fleet_svc.ROLES),
        "window_seconds": window,
        "retention_days": calls_svc.retention_days(),
        "users": users_out,
        "user_counts": user_counts,
    }
