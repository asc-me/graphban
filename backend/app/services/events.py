"""The audit ledger (AL-43): one owner for recording and reading mutation events.

Written at the boundaries — the MCP dispatcher for agent (API-key) actions and
REST routers for user actions — so every accepted mutation captures who did it.
Recording never raises into the caller: an audit failure must not fail the
operation it audits (best-effort, logged).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import ApiKey, Event, User

logger = logging.getLogger("graphban.events")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes, Postgres aware ones — same wall clock in UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def record(
    db: Session,
    *,
    actor_type: str,
    actor_id: str = "",
    actor_label: str = "",
    surface: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    project_id: str | None = None,
    meta: dict | None = None,
) -> None:
    try:
        db.add(Event(
            actor_type=actor_type, actor_id=actor_id, actor_label=actor_label,
            surface=surface, action=action, target_type=target_type,
            target_id=target_id, project_id=project_id, meta=meta,
        ))
        db.commit()
    except Exception:  # noqa: BLE001 — auditing must never break the audited op
        logger.exception("failed to record event %r", action)
        db.rollback()


def record_key(db: Session, key: ApiKey, *, action: str, target_type: str = "",
               target_id: str = "", project_id: str | None = None, meta: dict | None = None) -> None:
    """Record an agent action, attributed to the API key that performed it — and to the
    HUMAN principal that owns the key, so the audit shows who was behind the agent (AL-197)."""
    owner = db.get(User, key.user_id) if key.user_id else None
    m = dict(meta or {})
    if owner is not None:
        m["principal"] = {"id": owner.id, "label": owner.handle or owner.name}
    record(
        db, actor_type="apikey", actor_id=key.id, actor_label=key.name or key.id,
        surface="mcp", action=action, target_type=target_type, target_id=target_id,
        project_id=project_id, meta=m or None,
    )


def record_user(db: Session, user: User, *, action: str, target_type: str = "",
                target_id: str = "", project_id: str | None = None, meta: dict | None = None) -> None:
    """Record a user action from a REST route, attributed to the logged-in user."""
    record(
        db, actor_type="user", actor_id=user.id, actor_label=user.handle or user.name,
        surface="rest", action=action, target_type=target_type, target_id=target_id,
        project_id=project_id, meta=meta,
    )


# ── Activity: lenses, facets, histogram (PRD-47 S10 / GRPH-961) ──────────────
#
# Every filter and aggregate below reads a COLUMN that already exists on `Event`
# (`actor_type`, `actor_id`, `actor_label`, `surface`, `action`, `target_type`, `ts`).
# The design also asks for a field-level diff and a trace id; neither is a column and
# nothing writes one, so this module neither filters nor aggregates on them and the UI
# says so instead of rendering a plausible blank. GRPH-979 holds that work.

#: The refusals the ledger actually records. Everything else a caller might call
#: "rejected" — a bounced review, a router 422, auto-triage's `auto_reject_shard` — is
#: NOT here, and the lens publishes this tuple so the tile can state its own coverage.
REFUSAL_ACTIONS = ("sign_off_refused", "role_refused", "agent_reject_shard")

#: Keys, seats, invites, roles and memberships. Both spellings of the api-key target
#: type are live in the data — `api_key` from routers/apikeys.py, `apikey` from
#: routers/fleet.py — so naming only one would silently under-count the lens.
ACCESS_TARGET_TYPES = (
    "api_key", "apikey", "org_invite", "org_membership", "membership",
    "team", "agent", "fleet_profile", "org_request",
)
ACCESS_ACTIONS = (
    "create_api_key", "revoke_api_key", "mint_fleet_key", "issue_seats", "reissue_seat",
    "revoke_unused_seats", "revoke_expired_keys", "assign_role", "role_refused",
    "create_platform_invite", "revoke_platform_invite", "create_org_invite",
    "revoke_org_invite", "accept_org_invite", "set_member_role", "remove_member",
    "add_team_member", "remove_team_member", "set_team_grant", "revoke_team_grant",
    "set_project_access", "dismiss_agent", "restore_agent",
)

MEMORY_TARGET_TYPES = ("shard",)
MEMORY_ACTIONS = (
    "add_memory", "publish_memory", "reject_memory", "extract_lessons",
    "agent_publish_shard", "agent_reject_shard", "undo_auto_shard",
    "auto_publish_shard", "auto_reject_shard", "trusted_publish_shard",
)

LENS_IDS = ("everything", "agent_writes", "human_decisions", "keys_access",
            "memory", "rejected")

#: 48 bars whatever the range, so the bar width follows the range and 24h is the
#: design's 30-minute bucket (86400 / 48).
BUCKET_COUNT = 48
RANGES = {"1h": 3600, "24h": 86_400, "7d": 604_800, "30d": 2_592_000}

#: Ceiling on the rows the histogram reads. Past it the newest events win and the
#: payload says `partial`: a histogram that quietly covered only part of its window
#: would read as "nothing happened before that".
HISTOGRAM_SCAN_CAP = 50_000
FACET_LIMIT = 40


class BadFilter(ValueError):
    """A filter value this module does not define. Raised, never widened: an
    unrecognised lens or bucket that fell back to "everything" would answer a question
    nobody asked and look like a clean result."""


def _lens_clause(lens: str):
    if lens == "everything":
        return None
    if lens == "agent_writes":
        return Event.actor_type == "apikey"
    if lens == "human_decisions":
        return Event.actor_type == "user"
    if lens == "keys_access":
        return or_(Event.target_type.in_(ACCESS_TARGET_TYPES),
                   Event.action.in_(ACCESS_ACTIONS))
    if lens == "memory":
        return or_(Event.target_type.in_(MEMORY_TARGET_TYPES),
                   Event.action.in_(MEMORY_ACTIONS))
    if lens == "rejected":
        return Event.action.in_(REFUSAL_ACTIONS)
    raise BadFilter(f"unknown lens: {lens!r}")


def lens_definitions() -> list[dict]:
    """The six lenses, each carrying what it actually selects. Shipped to the client so
    a tile states its own coverage — a count labelled "rejected" that silently means
    three recorded refusal kinds is the absence-reads-as-clean defect wearing a tile."""
    defs = [
        {"id": "everything", "label": "Everything",
         "hint": "Every mutation the ledger recorded, in the selected range."},
        {"id": "agent_writes", "label": "Agent writes",
         "hint": "Recorded against an API key — an agent acting, with the person "
                 "behind the key named where it is known."},
        {"id": "human_decisions", "label": "Human decisions",
         "hint": "Recorded against a person: the web UI and REST, including an "
                 "assistant acting as them."},
        {"id": "keys_access", "label": "Keys & access",
         "hint": "API keys, seats, invites, roles, memberships and project access."},
        {"id": "memory", "label": "Memory changes",
         "hint": "Shards and lessons written, published, rejected or undone — by a "
                 "person, an agent, or auto-triage."},
        {"id": "rejected", "label": "Rejected",
         "hint": f"Only the {len(REFUSAL_ACTIONS)} refusal kinds the ledger records: "
                 f"{', '.join(REFUSAL_ACTIONS)}. A bounced review, a rejected HTTP "
                 "request or an auto-triage rejection is not counted here.",
         "covers": list(REFUSAL_ACTIONS)},
    ]
    return defs


def _series(actor_type: str, action: str) -> str:
    """Which stacked bar an event lands in. Mutually exclusive, so a bucket's total is
    the number of events in it — a refusal counts once, in `rejected`, even though an
    agent key made it. `system` is its own series: auto-triage and the Stripe webhook
    record `actor_type="system"`, and folding a machine's decision into the `human` bar
    would attribute it to a person."""
    if action in REFUSAL_ACTIONS:
        return "rejected"
    if actor_type == "apikey":
        return "agent"
    if actor_type == "user":
        return "human"
    return "system"


SERIES = ("agent", "human", "rejected", "system")


def list_events(db: Session, *, project_ids: list[str], limit: int = 50, offset: int = 0,
                action: str | None = None, lens: str | None = None,
                actor: str | None = None, surface: str | None = None,
                target_type: str | None = None, target_id: str | None = None,
                bucket: int | None = None,
                range_key: str | None = None, now: datetime | None = None) -> dict:
    """Most-recent-first events across the projects the caller may read, plus the
    Activity aggregates: lens counts, the stacked histogram, and the person / surface /
    object facets.

    Every argument is optional and additive. With none of them this is the plain audit
    ledger it always was, and the aggregates come back stated-as-absent rather than
    invented: `histogram.coverage` is `"not_requested"` with no `range_key`, which is a
    different answer from 48 zero buckets.

    `total` counts what the filters matched — it used to ignore `action`, so a filtered
    page reported the size of the whole ledger and `has_more` lied about it.
    """
    now = _aware(now) or _now()
    scope = [Event.project_id.in_(project_ids)]
    if action:
        scope.append(Event.action == action)

    since = until = None
    bucket_seconds = None
    origin = None
    if range_key is not None:
        if range_key not in RANGES:
            raise BadFilter(f"unknown range: {range_key!r}")
        span = RANGES[range_key]
        bucket_seconds = max(1, span // BUCKET_COUNT)
        # Aligned to a bucket boundary, and CEILED. An origin of "now" would slide every
        # bar by the seconds between two reads, so a bucket chip would filter a different
        # window than the bar the user clicked. Flooring instead would put `until` in the
        # past and drop the newest events — up to a whole bar of them — from a view whose
        # job is "what just happened", so the top bar is the one `now` falls in and is
        # partially elapsed.
        epoch = int(now.timestamp())
        origin = datetime.fromtimestamp(epoch + (-epoch % bucket_seconds), tz=timezone.utc)
        since, until = origin - timedelta(seconds=span), origin

    lens_clause = _lens_clause(lens) if lens else None
    actor_c = Event.actor_id == actor if actor is not None else None
    surface_c = Event.surface == surface if surface is not None else None
    # `target_type=""` is a real value — most MCP writes record no target type — so the
    # empty string selects the untyped rows and only None means "no object filter".
    target_c = Event.target_type == target_type if target_type is not None else None
    # The panel's "all events on this target" pivot. Same dimension as `target_type` for
    # facet purposes: picking one target must not collapse the object facet to it.
    target_id_c = Event.target_id == target_id if target_id is not None else None

    window = []
    if since is not None:
        window.append(Event.ts >= since)
    if until is not None:
        window.append(Event.ts < until)

    bucket_c: list = []
    if bucket is not None:
        if since is None or bucket_seconds is None:
            raise BadFilter("bucket needs a range: a bar index means nothing without it")
        idx = int(bucket)
        if not 0 <= idx < BUCKET_COUNT:
            raise BadFilter(f"bucket out of range: {bucket!r}")
        start = since + timedelta(seconds=idx * bucket_seconds)
        bucket_c = [Event.ts >= start, Event.ts < start + timedelta(seconds=bucket_seconds)]

    def conds(exclude: str | None = None, *, with_bucket: bool = True,
              with_lens: bool = True) -> list:
        """Filters minus one dimension, so a facet can list the values the current
        selection of that same dimension has not already collapsed to one."""
        out = [*scope, *window]
        if with_lens and lens_clause is not None:
            out.append(lens_clause)
        if exclude != "actor" and actor_c is not None:
            out.append(actor_c)
        if exclude != "surface" and surface_c is not None:
            out.append(surface_c)
        if exclude != "object":
            if target_c is not None:
                out.append(target_c)
            if target_id_c is not None:
                out.append(target_id_c)
        if with_bucket:
            out.extend(bucket_c)
        return out

    limit, offset = max(0, int(limit)), max(0, int(offset))
    row_conds = conds()
    total = int(db.scalar(select(func.count(Event.id)).where(*row_conds)) or 0)
    # Every event in the readable projects, ignoring every filter including the range.
    # The view needs it to tell "this project has never recorded anything" from "nothing
    # matches this selection" — both would otherwise render as the same empty list.
    ledger_total = int(db.scalar(select(func.count(Event.id)).where(
        Event.project_id.in_(project_ids))) or 0)
    rows = db.scalars(
        select(Event).where(*row_conds).order_by(Event.id.desc()).limit(limit).offset(offset)
    ).all()

    lens_conds = conds(with_lens=False)
    lenses = []
    for d in lens_definitions():
        clause = _lens_clause(d["id"])
        where = [*lens_conds] + ([clause] if clause is not None else [])
        lenses.append({**d, "count": int(db.scalar(
            select(func.count(Event.id)).where(*where)) or 0)})

    return {
        "results": [_event_dict(e) for e in rows],
        "total": total, "limit": limit, "offset": offset,
        "has_more": offset + limit < total,
        "ledger_total": ledger_total,
        "lenses": lenses,
        "histogram": _histogram(db, conds, since=since, bucket_seconds=bucket_seconds,
                                origin=origin, range_key=range_key),
        "facets": {
            "person": _person_facet(db, conds(exclude="actor")),
            "surface": _value_facet(db, Event.surface, conds(exclude="surface")),
            "object": _value_facet(db, Event.target_type, conds(exclude="object"),
                                   blank_label="untyped"),
        },
        "filters": {
            "lens": lens or "everything", "actor": actor, "surface": surface,
            "target_type": target_type, "target_id": target_id, "bucket": bucket,
            "range": range_key, "action": action,
        },
    }


def _histogram(db: Session, conds, *, since, bucket_seconds, origin, range_key) -> dict:
    if range_key is None or since is None or bucket_seconds is None or origin is None:
        return {"range": None, "bucket_seconds": None, "origin": None, "buckets": [],
                "coverage": "not_requested", "scanned": 0, "partial": False}

    # Lens and facets apply; the bucket filter does not, or clicking a bar would hide
    # every other bar and there would be no way to move to the next one.
    scanned = db.execute(
        select(Event.ts, Event.actor_type, Event.action)
        .where(*conds(with_bucket=False))
        .order_by(Event.ts.desc())
        .limit(HISTOGRAM_SCAN_CAP + 1)
    ).all()
    partial = len(scanned) > HISTOGRAM_SCAN_CAP
    counted = scanned[:HISTOGRAM_SCAN_CAP] if partial else scanned

    buckets = [{"index": i,
                "start": (since + timedelta(seconds=i * bucket_seconds)).isoformat(),
                "end": (since + timedelta(seconds=(i + 1) * bucket_seconds)).isoformat(),
                "agent": 0, "human": 0, "rejected": 0, "system": 0}
               for i in range(BUCKET_COUNT)]
    span = bucket_seconds * BUCKET_COUNT
    for ts, actor_type, action in counted:
        at = _aware(ts)
        if at is None:
            continue
        idx = int((at - since).total_seconds() // bucket_seconds)
        # `window` already bounds this; the guard keeps a clock-skewed row out of a bar
        # it does not belong to instead of wrapping it into one.
        if not 0 <= idx < BUCKET_COUNT or (at - since).total_seconds() >= span:
            continue
        buckets[idx][_series(actor_type, action)] += 1

    return {"range": range_key, "bucket_seconds": bucket_seconds,
            "origin": origin.isoformat(), "buckets": buckets,
            "coverage": "partial" if partial else "full",
            "scanned": len(counted), "partial": partial}


def _person_facet(db: Session, where: list) -> dict:
    rows = db.execute(
        select(Event.actor_type, Event.actor_id, Event.actor_label, func.count(Event.id))
        .where(*where)
        .group_by(Event.actor_type, Event.actor_id, Event.actor_label)
        .order_by(func.count(Event.id).desc())
        .limit(FACET_LIMIT + 1)
    ).all()
    truncated = len(rows) > FACET_LIMIT
    values = [{"value": actor_id, "label": actor_label or actor_id or "(unnamed)",
               "kind": actor_type, "count": int(n)}
              for actor_type, actor_id, actor_label, n in rows[:FACET_LIMIT]]
    return {"values": values, "truncated": truncated}


def _value_facet(db: Session, column, where: list, *, blank_label: str = "") -> dict:
    rows = db.execute(
        select(column, func.count(Event.id))
        .where(*where)
        .group_by(column)
        .order_by(func.count(Event.id).desc())
        .limit(FACET_LIMIT + 1)
    ).all()
    truncated = len(rows) > FACET_LIMIT
    values = []
    for value, n in rows[:FACET_LIMIT]:
        v = value or ""
        # An empty `target_type` is most of the ledger, not a missing row: label it
        # rather than dropping it, or the object facet looks tidier than the data is.
        values.append({"value": v, "label": v or blank_label or "(unset)", "count": int(n)})
    return {"values": values, "truncated": truncated}


def _principal_and_agent(e: Event) -> tuple[str, str]:
    """Normalize an event to (human principal, agent): the person on whose behalf it ran,
    and the agent that performed it — empty when none. AL-197.

    - API-key action: the key IS the agent; the human is its owner (meta.principal).
    - assistant action: the human is the actor; the agent is meta.origin (assistant:<provider>).
    - plain user action: the human is the actor; no agent.
    """
    meta = e.meta or {}
    if e.actor_type == "apikey":
        principal = (meta.get("principal") or {}).get("label") or ""
        # The AGENT behind the key when the dispatcher could name it (PRD-34 D3); several
        # agents share one credential by design, and the key label alone cannot say which.
        agent = str(meta.get("agent_id") or "") or (e.actor_label or e.actor_id)
        return principal, agent
    origin = str(meta.get("origin") or "")
    if origin.startswith("assistant:"):
        return (e.actor_label or e.actor_id), origin
    return (e.actor_label or e.actor_id), ""


def _event_dict(e: Event) -> dict:
    principal, agent = _principal_and_agent(e)
    return {
        "id": e.id,
        "ts": e.ts.isoformat() if e.ts else None,
        "actor_type": e.actor_type,
        "actor_id": e.actor_id,
        "actor_label": e.actor_label,
        "principal": principal,  # the human behind the action (AL-197)
        "agent": agent,          # the agent that performed it, if any
        "surface": e.surface,
        "action": e.action,
        "target_type": e.target_type,
        "target_id": e.target_id,
        "project_id": e.project_id,
        "meta": e.meta,
    }


# The complete set of actions that can be taken FROM the operator plane. It's an
# allowlist, not a "not project-scoped" filter: a project-less tenant event (a global
# memory write, say) is still tenant activity and must not surface cross-tenant.
PLATFORM_ACTIONS = (
    "create_platform_invite",
    "revoke_platform_invite",
    "decide_org_request",
    "set_org_plan",
)


def platform_ledger(db: Session, *, limit: int = 12) -> list[Event]:
    """Most-recent-first operator-plane actions, across every tenant.

    This is the operator's own ledger, not a platform activity feed. Nothing a tenant
    does appears here, so an empty result means "no operator has done anything", never
    "the platform is quiet" — the caller renders those as different sentences.
    """
    return list(
        db.scalars(
            select(Event)
            .where(Event.action.in_(PLATFORM_ACTIONS))
            .order_by(Event.id.desc())
            .limit(limit)
        )
    )
