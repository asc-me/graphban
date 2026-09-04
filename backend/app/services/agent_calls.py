"""The observed feed (PRD-34): what an agent asked Graphban for, one row per call.

Live (PRD-33) answers *who is here*. This answers *what are they doing*, from the one
stream the server can observe without a daemon on anyone's machine — its own calls.
Reads included: `get_context`, `search_code`, `get_item_details` are most of what a
working agent does, and a feed that hid them would show the same quiet roster Live
already refuses to show.

Three rules, each the reason for a function here:

- **Never fail the call.** `record` swallows and logs, like `events.record`. A feed write
  error is not an agent's problem. But a *sweep* failure is counted (`SWEEP_FAILED`, on
  `/health`) rather than swallowed, because a retention job that keeps failing is a table
  that grows in silence (D18).
- **One target string, from an allowlist.** `TARGETS` maps a tool to the one argument (or
  result field) that names what the call was about. Nothing else from the call is stored.
  An unknown tool gets `""`, not a crash and not its arguments (D4).
- **Two statements per board.** `summary` is one `GROUP BY` and one latest-row query
  whatever the agent count (D19). Silence states are derived from those rows at query
  time — `never`, `quiet`, `active` — and never stored (D21).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Agent, AgentCall

logger = logging.getLogger(__name__)

SOURCES = ("observed", "reported")
CALL_STATES = ("never", "quiet", "active")
STATUS_STATES = ("unreported", "reported", "stale")

TARGET_MAX = 120
#: D6: a successful call to these is presence, not activity — no observed row. PR 2 writes a
#: `reported` row when a heartbeat CARRIES a status that changed. Refusals are still rows.
PRESENCE_ONLY_TOOLS = frozenset({"heartbeat"})
#: D10: the sweep runs on the write path, every Nth insert per process. A module counter
#: rather than a timer because `main.py` has no scheduler and this PRD does not add one.
SWEEP_EVERY = 200
#: D18: how many times the amortised sweep has raised in this process. Exposed on
#: `/health` so a retention job that keeps failing is visible on the deployed box.
SWEEP_FAILED = 0
_inserts = 0


# ---- targets (D4) ---------------------------------------------------------------------------

def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _item_id(args: dict, result: Any) -> str:
    return _s(args.get("id"))


def _update_item(args: dict, result: Any) -> str:
    id_ = _s(args.get("id"))
    status = _s(args.get("status"))
    return f"{id_} → {status}" if id_ and status else id_


def _claimed(args: dict, result: Any) -> str:
    """The claim tools name their target in the RESULT — nothing was chosen on the way in."""
    if isinstance(result, dict):
        if isinstance(result.get("id"), str):
            return result["id"]
        cluster = result.get("cluster") or result.get("items") or []
        ids = [c.get("id") for c in cluster if isinstance(c, dict) and isinstance(c.get("id"), str)]
        return ", ".join(ids)
    return ""


def _query(args: dict, result: Any) -> str:
    return _s(args.get("query")) or _s(args.get("q"))


def _path(args: dict, result: Any) -> str:
    return _s(args.get("path")) or _s(args.get("node")) or _s(args.get("node_id")) or _s(args.get("query"))


def _prd_id(args: dict, result: Any) -> str:
    return _s(args.get("prd_id"))


def _shard_id(args: dict, result: Any) -> str:
    """`add_memory`: the returned shard id, never the text."""
    if isinstance(result, dict):
        return _s(result.get("id"))
    return ""


def _nothing(args: dict, result: Any) -> str:
    return ""


TARGETS: dict[str, Callable[[dict, Any], str]] = {
    "get_context": _item_id,
    "get_item_details": _item_id,
    "heartbeat": _item_id,
    "release_item": _item_id,
    "claim_review": _item_id,
    "sign_off": _item_id,
    "bounce": _item_id,
    "review_recommendation": _item_id,
    "link_items": _item_id,
    "unlink_items": _item_id,
    "update_item": _update_item,
    "claim_next": _claimed,
    "next_cluster": _claimed,
    "claim_cluster": _claimed,
    "create_item": _claimed,
    "search_code": _query,
    "search_items": _query,
    "search_memory": _query,
    "graph_query": _query,
    "code_neighbors": _path,
    "get_code_map": _path,
    "get_prd": _prd_id,
    "prd_coverage": _prd_id,
    "prd_acceptance": _prd_id,
    "grill_prd": _prd_id,
    "answer_grill": _prd_id,
    "decompose_prd": _prd_id,
    "update_prd": _prd_id,
    "close_prd": _prd_id,
    "add_memory": _shard_id,
    "register_agent": _nothing,
    "fleet_status": _nothing,
    "list_projects": _nothing,
}


def target_for(tool: str, args: dict | None, result: Any) -> str:
    """One string naming what the call was about. Never raises, never stores arguments."""
    fn = TARGETS.get(tool, _nothing)
    try:
        out = fn(args if isinstance(args, dict) else {}, result)
    except Exception:  # noqa: BLE001 — an extractor must never reach the caller
        logger.exception("target extractor for %r raised", tool)
        return ""
    if not isinstance(out, str):
        return ""
    return out[:TARGET_MAX]


# ---- writes (D2, D10, D18) ------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def retention_days() -> int:
    return int(settings.agent_call_retention_days)


def _cutoff(now: datetime | None = None) -> datetime | None:
    days = retention_days()
    if days <= 0:
        return None
    return (now or _now()) - timedelta(days=days)


def sweep(db: Session, project_id: str, *, now: datetime | None = None) -> int:
    """Delete rows older than retention for one project. Returns the count. Raises on failure —
    `record` is what counts and swallows; a direct caller (the admin endpoint) wants the error."""
    cutoff = _cutoff(now)
    if cutoff is None:
        return 0
    res = db.execute(delete(AgentCall).where(AgentCall.project_id == project_id,
                                             AgentCall.ts < cutoff))
    db.commit()
    return int(res.rowcount or 0)


def record(db: Session, *, project_id: str | None, api_key_id: str | None,
           agent_id: str | None, tool: str, target: str = "", ok: bool = True,
           error_code: str | None = None, duration_ms: int | None = None,
           source: str = "observed", status: str | None = None,
           files: list[str] | None = None) -> None:
    """Write one row. Swallows everything: the feed must never fail the call it describes.

    Skipped, with a debug line, when the call resolved to no project or no credential — a
    row with nowhere to appear is not worth a foreign-key error on the agent's response.
    """
    global _inserts, SWEEP_FAILED
    if not project_id or not api_key_id:
        logger.debug("agent_calls: %r skipped (project=%r key=%r)", tool, project_id, api_key_id)
        return
    if source not in SOURCES:
        source = "observed"
    try:
        db.add(AgentCall(
            project_id=project_id, agent_id=agent_id or None, api_key_id=api_key_id,
            source=source, tool=(tool or "")[:64], target=(target or "")[:TARGET_MAX],
            ok=bool(ok), error_code=(error_code or None),
            duration_ms=int(duration_ms) if duration_ms is not None else None,
            status=(status[:200] if isinstance(status, str) else None),
            files=list(files) if files else None,
        ))
        db.commit()
    except Exception:  # noqa: BLE001 — telemetry must never break the call
        logger.exception("agent_calls: failed to record %r", tool)
        db.rollback()
        return
    _inserts += 1
    if _inserts % SWEEP_EVERY == 0:
        try:
            sweep(db, project_id)
        except Exception:  # noqa: BLE001 — counted, not swallowed (D18)
            SWEEP_FAILED += 1
            logger.warning("agent_calls: sweep failed for %r (total failures %d)",
                           project_id, SWEEP_FAILED, exc_info=True)
            db.rollback()


# ---- reads (D6, D19, D21) -------------------------------------------------------------------

def _row(c: AgentCall) -> dict:
    out: dict[str, Any] = {
        "id": c.id,
        "at": c.ts.isoformat() if c.ts else None,
        "source": c.source,
        "tool": c.tool,
        "target": c.target,
        "ok": bool(c.ok),
    }
    if c.error_code:
        out["error_code"] = c.error_code
    if c.duration_ms is not None:
        out["duration_ms"] = c.duration_ms
    if c.source == "reported":
        out["status"] = c.status or ""
        out["files"] = list(c.files or [])
    return out


def feed(db: Session, project_id: str, agent_id: str, *, limit: int = 50,
         now: datetime | None = None) -> dict | None:
    """Newest first. `state: "never"` with no rows is the empty; `rows: []` with `state: "ok"`
    is invalid and this function cannot produce it. None when the agent is not on this
    project — the router turns that into 404 without touching `Agent` itself (A12/A16)."""
    row = db.get(Agent, agent_id)
    if row is None or row.project_id != project_id:
        return None
    now = now or _now()
    cutoff = _cutoff(now)
    stmt = select(AgentCall).where(AgentCall.agent_id == agent_id)
    if cutoff is not None:
        stmt = stmt.where(AgentCall.ts >= cutoff)
    rows = db.scalars(stmt.order_by(AgentCall.id.desc()).limit(max(1, min(int(limit), 200)))).all()
    return {
        "served_at": now.isoformat(),
        "retention_days": retention_days(),
        "state": "ok" if rows else "never",
        "rows": [_row(c) for c in rows],
    }


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def summary(db: Session, project_id: str, agent_ids: list[str], *,
            window_seconds: int, interval_seconds: int,
            now: datetime | None = None) -> dict:
    """Per-agent last call + window count, and per-credential unattributed counts.

    TWO statements, whatever `len(agent_ids)` (D19): one GROUP BY over the window, one
    latest-row-per-agent. Returns
    ``{"agents": {agent_id: {...}}, "unattributed": {api_key_id: count}}``.
    Every id in `agent_ids` is present in `agents`, at `never` when it has no rows.
    """
    now = now or _now()
    cutoff = _cutoff(now)
    since = now - timedelta(seconds=max(1, int(window_seconds)))
    if cutoff is not None and since < cutoff:
        since = cutoff

    # 1. Counts in the window, grouped by (agent, key). NULL agents land in the credential
    #    census; attributed ones in `calls_in_window`.
    counts = db.execute(
        select(AgentCall.agent_id, AgentCall.api_key_id, func.count(AgentCall.id))
        .where(AgentCall.project_id == project_id, AgentCall.ts >= since)
        .group_by(AgentCall.agent_id, AgentCall.api_key_id)
    ).all()
    in_window: dict[str, int] = {}
    unattributed: dict[str, int] = {}
    for agent_id, key_id, n in counts:
        if agent_id is None:
            unattributed[key_id] = unattributed.get(key_id, 0) + int(n)
        else:
            in_window[agent_id] = in_window.get(agent_id, 0) + int(n)

    # 2. Latest row per agent, inside retention. `id` is monotonic with `ts` for one
    #    process, and the sweep only removes old rows, so max(id) is the newest call.
    agents: dict[str, dict] = {}
    if agent_ids:
        latest_ids = select(func.max(AgentCall.id)).where(
            AgentCall.project_id == project_id, AgentCall.agent_id.in_(agent_ids))
        if cutoff is not None:
            latest_ids = latest_ids.where(AgentCall.ts >= cutoff)
        latest_ids = latest_ids.group_by(AgentCall.agent_id)
        rows = db.scalars(select(AgentCall).where(AgentCall.id.in_(latest_ids))).all()
        latest = {c.agent_id: c for c in rows}
        for aid in agent_ids:
            c = latest.get(aid)
            if c is None:
                agents[aid] = {"last_call": None, "calls_in_window": in_window.get(aid, 0),
                               "silence_seconds": None, "call_state": "never"}
                continue
            at = _aware(c.ts)
            silence = max(0, int((now - at).total_seconds())) if at else None
            state = "active" if silence is not None and silence <= interval_seconds else "quiet"
            agents[aid] = {
                "last_call": {"tool": c.tool, "target": c.target,
                              "at": at.isoformat() if at else None, "ok": bool(c.ok)},
                "calls_in_window": in_window.get(aid, 0),
                "silence_seconds": silence,
                "call_state": state,
            }
    return {"agents": agents, "unattributed": unattributed}
