"""Deployment-wide usage aggregate (PRD-47 S14 / GRPH-965).

One service function backs the Usage page: identity, KPIs, the per-day MCP chart,
by-project table, license limits, model usage by harness, and busiest API keys.
Agent-call telemetry is retained for only ``AGENT_CALL_RETENTION_DAYS``; longer
ranges are served from what exists and named ``partial`` rather than padded with zeroes.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Agent,
    AgentCall,
    ApiKey,
    AttemptTelemetry,
    Item,
    MemoryShard,
    OrgMembership,
    Organization,
    Project,
    User,
)
from app.providers import llm_meter
from app.security import authz
from app.services import instance_update, items as items_svc, quotas
from app.services.fleet import presence_ttl_seconds

RANGE_CHOICES = (7, 30, 90)
MAX_BUCKETS = 45

#: Harnesses whose compute runs on hardware the deployment already pays for, so their cost is a
#: REAL zero and not an unknown one. ``llm_meter.LOCAL_PROVIDERS`` covers the local LLM providers;
#: ``gbagent`` is the local agent runtime gbfleet ships, and PRD-47 S14's footnote names it:
#: *"Local models (gbagent) show $0."*
LOCAL_HARNESSES = frozenset(llm_meter.LOCAL_PROVIDERS) | {"gbagent"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(dt: datetime) -> datetime:
    d = dt.astimezone(timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _user_org(db: Session, user_id: str) -> Organization | None:
    org_id = db.scalar(
        select(OrgMembership.org_id).where(OrgMembership.user_id == user_id).limit(1)
    )
    return db.get(Organization, org_id) if org_id else None


def _bucket_count(range_days: int) -> int:
    """Daily buckets, coarsened above MAX_BUCKETS (PRD-47 S14)."""
    if range_days <= MAX_BUCKETS:
        return range_days
    # Merge consecutive days so the chart stays readable.
    step = max(1, (range_days + MAX_BUCKETS - 1) // MAX_BUCKETS)
    return (range_days + step - 1) // step


def _agent_call_daily(db: Session, project_ids: list[str], since: datetime) -> dict[str, int]:
    if not project_ids:
        return {}
    day = func.date(AgentCall.ts)
    rows = db.execute(
        select(day, func.count())
        .where(AgentCall.project_id.in_(project_ids), AgentCall.ts >= since)
        .group_by(day)
        .order_by(day)
    )
    return {str(r[0]): int(r[1]) for r in rows}


def _agent_call_daily_by_project(
    db: Session, project_ids: list[str], since: datetime,
) -> dict[str, dict[str, int]]:
    if not project_ids:
        return {}
    day = func.date(AgentCall.ts)
    rows = db.execute(
        select(day, AgentCall.project_id, func.count())
        .where(AgentCall.project_id.in_(project_ids), AgentCall.ts >= since)
        .group_by(day, AgentCall.project_id)
        .order_by(day)
    )
    out: dict[str, dict[str, int]] = {}
    for d, pid, n in rows:
        key = str(d)
        out.setdefault(key, {})[pid] = int(n)
    return out


def _fill_series(daily: dict[str, int], range_days: int, end: datetime) -> list[dict[str, Any]]:
    step = 1 if range_days <= MAX_BUCKETS else max(1, (range_days + MAX_BUCKETS - 1) // MAX_BUCKETS)
    buckets = (range_days + step - 1) // step
    start = _day_start(end - timedelta(days=range_days - 1))
    out: list[dict[str, Any]] = []
    for i in range(buckets):
        b_start = start + timedelta(days=i * step)
        b_end = min(b_start + timedelta(days=step), _day_start(end) + timedelta(days=1))
        total = 0
        d = b_start
        while d < b_end:
            total += daily.get(d.date().isoformat(), 0)
            d += timedelta(days=1)
        out.append({
            "start": b_start.isoformat(),
            "end": (b_end - timedelta(seconds=1)).isoformat(),
            "value": total,
        })
    return out


def _delta(current: int, prior: int) -> int | None:
    if prior == 0 and current == 0:
        return 0
    if prior == 0:
        return None
    return current - prior


def _limits_payload(db: Session, org: Organization | None) -> list[dict[str, Any]]:
    """D5: undeclared limits never get a computed percentage."""
    if not settings.hosted_mode or org is None:
        return [
            {"id": "projects", "label": "Projects", "used": None, "limit": None, "declared": False},
            {"id": "seats", "label": "Seats", "used": None, "limit": None, "declared": False},
            {"id": "shards", "label": "Memory shards", "used": None, "limit": None, "declared": False},
            {"id": "mcp_calls", "label": "MCP calls / month", "used": None, "limit": None, "declared": False},
        ]
    plan = quotas.plan_of(org)
    used = quotas.usage(db, org.id)
    return [
        {"id": "projects", "label": "Projects", "used": used["projects"],
         "limit": plan.max_projects, "declared": True},
        {"id": "seats", "label": "Seats", "used": used["seats"],
         "limit": plan.max_seats, "declared": True},
        {"id": "shards", "label": "Memory shards", "used": used["shards"],
         "limit": plan.max_shards, "declared": True},
        {"id": "mcp_calls", "label": "MCP calls / month", "used": used["calls_this_month"],
         "limit": plan.max_calls_per_month, "declared": True},
    ]


def _on_pace_note(limits: list[dict[str, Any]]) -> str | None:
    declared = [l for l in limits if l["declared"] and l["limit"]]
    if not declared:
        return "This deployment does not declare plan limits — bars are omitted rather than invented."
    now = _now()
    day_of_month = now.day
    days_in_month = 30  # on-pace is indicative, not billing-precise
    notes: list[str] = []
    for row in declared:
        if row["id"] != "mcp_calls":
            continue
        used, limit = row["used"] or 0, row["limit"] or 0
        if limit <= 0:
            continue
        pace = (used / day_of_month) * days_in_month if day_of_month else 0
        if pace > limit * 1.1:
            notes.append("MCP calls are ahead of a linear month pace.")
        elif pace < limit * 0.5 and day_of_month > 10:
            notes.append("MCP calls are well under a linear month pace.")
    return " ".join(notes) if notes else None


def _model_usage(db: Session, project_ids: list[str], since: datetime) -> dict[str, Any]:
    """Spawns, tokens and estimated cost per harness and model (PRD-47 S14 / GRPH-1002).

    Read from PRD-38 attempt records over the same finished-delegation population
    ``harness.roll`` grades, so this panel and the Harness page its link leads to cannot
    disagree about a window.

    Absence keeps its meaning on the way out, which is why every sum carries the count it
    rests on. ``tokens`` is None when NO attempt for that pair reported; it does not become
    0, because a zero in a token column claims the harness ran and used nothing. Cost adds
    the third answer: 0.0 is a real zero (local compute), None is unpriced, and neither is
    a number this deployment did not produce.
    """
    from app.services.delegation import UNDECLARED

    rows = db.execute(
        select(
            AttemptTelemetry.vendor,
            AttemptTelemetry.model,
            AttemptTelemetry.tokens_in,
            AttemptTelemetry.tokens_out,
        ).where(
            AttemptTelemetry.project_id.in_(project_ids),
            # `derived_at` NOT NULL IS the population: finished delegations only. Redundant
            # beside the window filter today (NULL >= since is never true), but it is the line
            # that keeps a launch which never reached an outcome out of the panel if that
            # filter is ever widened to coalesce(derived_at, reported_at).
            AttemptTelemetry.derived_at.is_not(None),
            AttemptTelemetry.derived_at >= since,
        )
    ).all() if project_ids else []

    groups: dict[tuple[str, str], dict[str, int]] = {}
    for vendor, model, tin, tout in rows:
        g = groups.setdefault((vendor or UNDECLARED, model or ""), {
            "spawns": 0, "tokens_in": 0, "tokens_out": 0, "tokens_reported": 0,
        })
        g["spawns"] += 1
        # Summed over the attempts that REPORTED, with that count kept beside them — the
        # `harness.roll` convention, and what stops a pair nobody measured from aggregating
        # into a zero.
        if tin is not None or tout is not None:
            g["tokens_in"] += tin or 0
            g["tokens_out"] += tout or 0
            g["tokens_reported"] += 1

    out: list[dict[str, Any]] = []
    for (vendor, model), g in groups.items():
        reported = g["tokens_reported"] > 0
        if vendor in LOCAL_HARNESSES:
            cost: float | None = 0.0
        elif not reported:
            # Pricing an unmeasured pair would multiply a list price by a fabricated zero
            # token count and print money nobody spent.
            cost = None
        else:
            cost = llm_meter.estimate_cost(vendor, model, g["tokens_in"], g["tokens_out"])
        out.append({
            "vendor": vendor,
            "model": model or UNDECLARED,
            "spawns": g["spawns"],
            "tokens": (g["tokens_in"] + g["tokens_out"]) if reported else None,
            "tokens_reported": g["tokens_reported"],
            "cost_usd": cost,
        })

    # Measured rows first, gaps after: a pair that reported nothing is not competing with one
    # that did, and interleaving them is how a column of gaps gets read as a column of zeroes.
    out.sort(key=lambda r: (r["tokens"] is None, -(r["tokens"] or 0), -r["spawns"]))

    spawns = sum(r["spawns"] for r in out)
    reported_total = sum(r["tokens_reported"] for r in out)
    return {
        "rows": out,
        "spawns": spawns,
        "tokens_reported": reported_total,
        "note": (
            f"{reported_total} of {spawns} attempts in this window reported tokens; "
            f"the rest are shown as not reported rather than as zero."
        ) if spawns and reported_total < spawns else None,
    }


def aggregate(db: Session, user_id: str, range_days: int = 30) -> dict[str, Any]:
    if range_days not in RANGE_CHOICES:
        range_days = 30

    project_ids = authz.readable_project_ids(db, user_id)
    projects = list(db.scalars(select(Project).where(Project.id.in_(project_ids)))) if project_ids else []
    org = _user_org(db, user_id)
    now = _now()
    retention = int(settings.agent_call_retention_days)
    since = now - timedelta(days=range_days - 1)
    retention_since = now - timedelta(days=retention)
    effective_since = max(since, retention_since)
    coverage = "full" if effective_since <= since else "partial"

    mcp_daily = _agent_call_daily(db, project_ids, effective_since)
    mcp_by_project_daily = _agent_call_daily_by_project(db, project_ids, effective_since)

    # KPI totals for current and prior equal-length windows
    window = timedelta(days=range_days)
    cur_since = now - window
    prev_since = now - window * 2
    cur_calls = db.scalar(
        select(func.count()).select_from(AgentCall).where(
            AgentCall.project_id.in_(project_ids), AgentCall.ts >= cur_since,
        )
    ) or 0 if project_ids else 0
    prev_calls = db.scalar(
        select(func.count()).select_from(AgentCall).where(
            AgentCall.project_id.in_(project_ids),
            AgentCall.ts >= prev_since,
            AgentCall.ts < cur_since,
        )
    ) or 0 if project_ids else 0

    presence_cutoff = now - timedelta(seconds=presence_ttl_seconds())
    agent_sessions = db.scalar(
        select(func.count(func.distinct(Agent.id))).where(
            Agent.project_id.in_(project_ids), Agent.last_seen_at >= presence_cutoff,
        )
    ) or 0 if project_ids else 0
    prev_agent_sessions = agent_sessions  # presence is a snapshot, not historical

    active_projects = sum(
        1 for p in projects
        if db.scalar(
            select(func.count()).select_from(AgentCall).where(
                AgentCall.project_id == p.id, AgentCall.ts >= cur_since,
            )
        )
    )
    shard_total = db.scalar(
        select(func.count()).select_from(MemoryShard).where(
            MemoryShard.project_id.in_(project_ids), MemoryShard.status != "rejected",
        )
    ) or 0 if project_ids else 0

    seats_used = quotas.seat_count(db, org.id) if org and settings.hosted_mode else None

    kpis = [
        {"id": "mcp_calls", "label": "MCP calls", "value": cur_calls,
         "delta": _delta(cur_calls, prev_calls),
         "sparkline": _fill_series(mcp_daily, min(range_days, retention), now)},
        {"id": "agent_sessions", "label": "Agent sessions", "value": agent_sessions,
         "delta": _delta(agent_sessions, prev_agent_sessions),
         "sparkline": _fill_series({}, min(range_days, retention), now)},
        {"id": "active_projects", "label": "Active projects", "value": active_projects,
         "delta": None, "sparkline": _fill_series({}, min(range_days, retention), now)},
        {"id": "seats", "label": "Seats in use", "value": seats_used,
         "delta": None, "sparkline": _fill_series({}, min(range_days, retention), now)},
        {"id": "shards", "label": "Memory shards", "value": shard_total,
         "delta": None, "sparkline": _fill_series({}, min(range_days, retention), now)},
    ]

    # Stacked chart: per-project series aligned to buckets
    bucket_series = _fill_series(mcp_daily, min(range_days, retention), now)
    project_meta = {p.id: {"id": p.id, "tag": p.tag, "name": p.name} for p in projects}
    chart_projects = []
    for pid, meta in project_meta.items():
        proj_daily = {d: mcp_by_project_daily.get(d, {}).get(pid, 0) for d in mcp_daily}
        chart_projects.append({
            **meta,
            "series": _fill_series(proj_daily, min(range_days, retention), now),
            "total": sum(proj_daily.values()),
        })
    chart_projects.sort(key=lambda r: -r["total"])

    by_project: list[dict[str, Any]] = []
    for p in projects:
        calls = db.scalar(
            select(func.count()).select_from(AgentCall).where(
                AgentCall.project_id == p.id, AgentCall.ts >= cur_since,
            )
        ) or 0
        agents = db.scalar(
            select(func.count(func.distinct(Agent.id))).where(Agent.project_id == p.id)
        ) or 0
        shards = db.scalar(
            select(func.count()).select_from(MemoryShard).where(
                MemoryShard.project_id == p.id, MemoryShard.status != "rejected",
            )
        ) or 0
        done = db.scalar(
            select(func.count()).select_from(Item).where(
                Item.project_id == p.id, Item.status == "done",
            )
        ) or 0
        by_project.append({
            "id": p.id, "tag": p.tag, "name": p.name,
            "calls": calls, "agents": agents, "shards": shards, "done": done,
        })
    by_project.sort(key=lambda r: -r["calls"])

    # Busiest API keys
    key_rows = db.execute(
        select(
            AgentCall.api_key_id,
            func.count().label("calls"),
            func.max(AgentCall.ts).label("last_call"),
        )
        .where(AgentCall.project_id.in_(project_ids), AgentCall.ts >= cur_since)
        .group_by(AgentCall.api_key_id)
        .order_by(func.count().desc())
        .limit(10)
    ) if project_ids else []
    busiest: list[dict[str, Any]] = []
    for row in key_rows:
        key = db.get(ApiKey, row.api_key_id)
        owner = db.get(User, key.user_id) if key and key.user_id else None
        busiest.append({
            "id": row.api_key_id,
            "name": (key.name or key.prefix) if key else row.api_key_id,
            "owner": (owner.handle or owner.name) if owner else "—",
            "calls": int(row.calls),
            "last_seen": row.last_call.isoformat() if row.last_call else None,
        })

    limits = _limits_payload(db, org)
    running = instance_update.running()

    return {
        "identity": {
            "mode": "hosted" if settings.hosted_mode else "self-host",
            "host": settings.app_base_url or "",
            "version": running["version"],
            "git_sha": running["git_sha"],
            "plan": org.plan if org else "self-host",
            "license": org.plan if org and settings.hosted_mode else "self-host",
        },
        "range_days": range_days,
        "retention_days": retention,
        "coverage": coverage,
        "kpis": kpis,
        "chart": {
            "buckets": bucket_series,
            "projects": chart_projects,
            "coverage": coverage,
            "note": (
                f"Bars cover the newest {retention} days of agent-call telemetry; "
                f"older days in a {range_days}d window are not measured."
            ) if coverage == "partial" else None,
        },
        "by_project": by_project,
        "limits": limits,
        "on_pace_note": _on_pace_note(limits),
        "model_usage": _model_usage(db, project_ids, cur_since),
        "busiest_keys": busiest,
    }


def to_csv(payload: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["project_tag", "project_name", "mcp_calls", "agents", "shards", "done"])
    for row in payload.get("by_project", []):
        w.writerow([row["tag"], row["name"], row["calls"], row["agents"], row["shards"], row["done"]])
    return buf.getvalue()
