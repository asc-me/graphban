"""Deployment-wide OTLP log export (PRD-47 S15 / GRPH-966).

One config row for the box, one exporter, one queue. The collector is shared, so this module
takes no `project_id` anywhere: a per-project config would be N panels in front of one thing
(GRPH-625's argument, settled again by the item's 2026-09-29 decision). Records may span
projects, so **each record names its project** instead — `gb.project` is the tag a reader
recognises, `gb.project_id` the id it resolves to, and `gb.scope` says when a record belongs
to the deployment rather than to a project (a blank `gb.project` would not).

## The state floor

Every number the status strip shows is a count that looks reassuring when it is actually
unknown, so this module never lets an absence arrive as a zero:

- `sent_24h` / `dropped_24h` are `None` when the counter read failed, and `0` only when it ran
  and measured nothing. They are a SUM over `LogExportBatch` rows rather than a running
  integer, because a counter that was never incremented and one that counted nothing are the
  same integer.
- `queue_depth` is `None` when the exporter is not running. A backlog of zero beside a dead
  exporter reads as "all clear"; the honest answer is "no answer".
- `state` separates `paused` (off, deliberately) from `not_running` (on, but nothing will drain
  it) from `unknown` (the config could not be read). Off and unknown are different states.
- `last_batch_state` separates `never` from `unavailable`, and a batch that sent nothing is a
  real row, not an absent one.

## What this build actually sends

OTLP/HTTP with the **JSON** encoding (`Content-Type: application/json`,
`POST {endpoint}/v1/logs|traces|metrics`) — a wire format the OTLP spec defines and every
collector accepts. `http/protobuf` is the OTLP protocol identifier the config stores; the
protobuf encoding itself needs `opentelemetry-proto`, which is not a dependency here, and
`providers/llm_meter.py` records the same decision about OTel. `grpc` needs `grpcio` plus the
proto stubs, so it is refused with a SPECIFIC answer rather than silently downgraded —
`protocol_support` publishes that, so the panel can say it before anyone presses a button.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Agent,
    AgentCall,
    ApiKey,
    Event,
    LogExportBatch,
    LogExportConfig,
    Project,
)
from app.services import events as events_svc
from app.services.scrub import scrub

logger = logging.getLogger("graphban.logexport")

#: The only row `log_export_config` ever holds.
SINGLETON_ID = "deployment"

#: OTLP protocol identifiers, and the compression choices. The sets live here — the router and
#: the panel read them rather than carrying their own copies.
PROTOCOLS = ("http/protobuf", "grpc")
COMPRESSIONS = ("none", "gzip")

#: The item names this failure deliberately, so it gets its own code and its own sentence
#: rather than arriving as a generic "could not connect".
NO_PORT_ERROR = "no_port"
NO_PORT_DETAIL = "No port in endpoint — collector unreachable"

GRPC_UNSUPPORTED = (
    "gRPC export needs grpcio and the OTLP proto stubs, which this build does not carry. "
    "Use http/protobuf — the same collector listens on its HTTP port."
)

#: Records handed over per signal per pass. Bounded so a box that was down for a week catches
#: up in measured steps instead of one enormous POST.
DRAIN_BATCH = 200

#: After this many consecutive failed export batches the backlog is dropped AND COUNTED.
#: Unbounded retry is an unbounded queue; giving up silently is a zero that means nothing.
MAX_CONSECUTIVE_FAILURES = 5

#: The status strip's window.
WINDOW_SECONDS = 86_400

#: Header values are read back masked; sending a mask back for that name means "unchanged" —
#: platform.py's credential rule, so a redacted round-trip from the form never wipes a secret.
MASK_SENTINEL = "[masked]"

#: Attribute names carrying free text. `redact_summaries` REPLACES the value rather than
#: dropping the key, so a reader can still tell a summary was involved — scrub.py's rule about
#: placeholders, applied to a field rather than to a pattern.
FREE_TEXT_KEYS = ("summary", "title", "description", "text", "body", "reason", "note",
                  "detail", "message", "status_text")

#: Attribute names carrying a client IP. `redact_client_ips` removes these outright.
IP_KEYS = ("client_ip", "ip", "forwarded_for", "remote_addr")

#: Keys in an event's `meta` that are read onto a record. Nothing else in `meta` leaves the
#: box: an allowlist, because `meta` is whatever any writer put there.
META_TEXT_KEYS = ("summary", "title", "description", "text", "reason", "note", "detail",
                  "message", "status_text")
META_IP_KEYS = ("client_ip", "ip", "forwarded_for")

RETENTION_NOTE = (
    "Exporting never removes anything: activity events stay in this box's ledger whether or "
    "not export is on. MCP call records are swept after {days} day(s) "
    "(AGENT_CALL_RETENTION_DAYS), so an exporter off for longer than that loses traces it had "
    "not sent yet — and counts them as dropped, never as zero."
)

CATCH_UP_NOTE = (
    "Turning export on starts from now. The ledger this box already holds is not back-filled "
    "into a collector you have just pointed at it."
)

_exporter_running = False


class BadConfig(ValueError):
    """A config value this module does not define. Raised, never widened: an unrecognised
    protocol that fell back to a default would send on a wire format nobody chose and then
    report success."""


class SendFailed(Exception):
    """One batch could not be handed over. `code` is the machine-readable failure the panel
    says out loud; `detail` is the sentence a human reads."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ── exporter presence ────────────────────────────────────────────────────────
# A module flag, not a DB column: the exporter IS this process, and a column would outlive the
# crash that stopped it — the "reads healthy for a process killed an hour ago" failure the
# Agent model's docstring refuses to repeat.

def mark_exporter_running() -> None:
    global _exporter_running
    _exporter_running = True


def mark_exporter_stopped() -> None:
    global _exporter_running
    _exporter_running = False


def exporter_running() -> bool:
    return _exporter_running


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes, Postgres aware ones — same wall clock in UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── config ───────────────────────────────────────────────────────────────────

def get_config(db: Session) -> LogExportConfig:
    """The deployment's one config row, created on first read.

    There is no `project_id` argument to get wrong, and calling this from two projects returns
    the same row — which is the whole point, and is what
    `test_one_row_for_the_deployment_not_one_per_project` pins.
    """
    row = db.get(LogExportConfig, SINGLETON_ID)
    if row is None:
        row = LogExportConfig(id=SINGLETON_ID)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


#: What the panel may write. The cursors are deliberately absent, so saving the form can
#: neither rewind nor skip the export position.
WRITABLE = ("enabled", "endpoint", "protocol", "compression", "headers", "send_events",
            "send_tool_calls", "send_heartbeats", "event_types", "redact_summaries",
            "redact_client_ips", "mask_api_keys")


def _high_water(db: Session) -> dict:
    """Where the ledger is right now — the cursors a first enable starts from."""
    return {
        "event_id": int(db.scalar(select(func.max(Event.id))) or 0),
        "call_id": int(db.scalar(select(func.max(AgentCall.id))) or 0),
        "heartbeat_at": _aware(db.scalar(select(func.max(Agent.last_seen_at)))),
    }


def update_config(db: Session, **patch) -> tuple[LogExportConfig, dict]:
    """Patch the deployment config. Returns `(row, notes)`.

    Unknown fields are refused rather than ignored: a silently dropped key is a save that
    reports success and changes nothing, which is the same lie as a counter that never moved.
    """
    cfg = get_config(db)
    unknown = sorted(set(patch) - set(WRITABLE))
    if unknown:
        raise BadConfig(f"not a log-export setting: {', '.join(unknown)}")

    # `None` means "not provided" for the two collection fields, not "clear it": a JSON PATCH
    # body built from a form sends nulls for anything untouched, and nulling `headers` would
    # delete every secret the operator did not re-type. Clearing is an empty dict / list.
    for optional in ("headers", "event_types"):
        if optional in patch and patch[optional] is None:
            patch.pop(optional)

    if "protocol" in patch and patch["protocol"] not in PROTOCOLS:
        raise BadConfig(f"protocol must be one of {', '.join(PROTOCOLS)}")
    if "compression" in patch and patch["compression"] not in COMPRESSIONS:
        raise BadConfig(f"compression must be one of {', '.join(COMPRESSIONS)}")
    if "endpoint" in patch:
        endpoint = (patch["endpoint"] or "").strip()
        if len(endpoint) > 512:
            raise BadConfig("endpoint is longer than 512 characters")
        patch["endpoint"] = endpoint
    if "headers" in patch:
        patch["headers"] = merge_headers(cfg.headers or {}, patch["headers"])
    if "event_types" in patch:
        # De-duplicated with order kept: the panel is a checkbox list, and a repeated action
        # would read back as two filters on one thing.
        patch["event_types"] = list(dict.fromkeys(
            str(t).strip() for t in patch["event_types"] if str(t).strip()))

    notes: dict = {}
    virgin = (cfg.cursor_event_id == 0 and cfg.cursor_call_id == 0
              and cfg.cursor_heartbeat_at is None)
    turning_on = bool(patch.get("enabled")) and not cfg.enabled

    for field, value in patch.items():
        setattr(cfg, field, value)
    cfg.updated_at = _now()

    if turning_on and virgin:
        marks = _high_water(db)
        cfg.cursor_event_id = marks["event_id"]
        cfg.cursor_call_id = marks["call_id"]
        cfg.cursor_heartbeat_at = marks["heartbeat_at"]
        notes["catch_up"] = "from_now"
        notes["catch_up_note"] = CATCH_UP_NOTE
    db.commit()
    db.refresh(cfg)
    return cfg, notes


def mask_secret(value: str) -> str:
    """The read-back form of an operator-supplied header value.

    Short values are masked whole: `abc1…cdef` from an eight-character token leaves enough to
    guess the rest, and a mask that leaks proportionally is not a mask.
    """
    v = value or ""
    if not v:
        return ""
    if len(v) <= 8:
        return MASK_SENTINEL
    return f"{v[:4]}…{v[-4:]}"


def merge_headers(stored: dict, incoming: dict) -> dict:
    """Apply a header edit that arrived masked.

    A value equal to the mask this module returned for that name means "unchanged" and keeps
    the stored secret; anything else replaces it. A name absent from `incoming` is DELETED —
    the panel sends the full list, and treating absence as "keep" would make a header
    impossible to remove.
    """
    out: dict[str, str] = {}
    for name, value in (incoming or {}).items():
        name = str(name).strip()
        if not name:
            continue
        was = str((stored or {}).get(name, ""))
        value = "" if value is None else str(value)
        out[name] = was if value == mask_secret(was) else value
    return out


def protocol_support() -> list[dict]:
    """Which protocols this build can speak, and why one cannot.

    Published so the panel states the limitation up front. A greyed-out choice with no reason
    is a guess the operator has to make; a choice that looks available and fails on send is
    worse.
    """
    return [
        {"id": "http/protobuf", "label": "HTTP / protobuf", "supported": True,
         "note": "OTLP/HTTP to {endpoint}/v1/logs, /v1/traces, /v1/metrics. This build sends "
                 "the JSON encoding; the protobuf one needs opentelemetry-proto."},
        {"id": "grpc", "label": "gRPC", "supported": False, "note": GRPC_UNSUPPORTED},
    ]


def config_dict(cfg: LogExportConfig) -> dict:
    """The config as the panel reads it. Header VALUES are masked; names are not — a name is
    what the operator typed and is not the secret."""
    return {
        "enabled": bool(cfg.enabled),
        "endpoint": cfg.endpoint or "",
        "protocol": cfg.protocol,
        "compression": cfg.compression,
        "headers": [{"name": k, "value": mask_secret(str(v))}
                    for k, v in sorted((cfg.headers or {}).items())],
        "send_events": bool(cfg.send_events),
        "send_tool_calls": bool(cfg.send_tool_calls),
        "send_heartbeats": bool(cfg.send_heartbeats),
        "event_types": list(cfg.event_types or []),
        "redact_summaries": bool(cfg.redact_summaries),
        "redact_client_ips": bool(cfg.redact_client_ips),
        "mask_api_keys": bool(cfg.mask_api_keys),
        "updated_at": _aware(cfg.updated_at).isoformat() if cfg.updated_at else None,
    }


def retention_note() -> str:
    """The paused note, with the retention number READ rather than typed.

    The design copy says events are kept locally for 90 days. Nothing in this codebase sweeps
    `events` — they are kept indefinitely — and `agent_calls` are swept at
    `AGENT_CALL_RETENTION_DAYS`. Printing the design's 90 would be an invented number wearing
    a measured one's clothes.
    """
    return RETENTION_NOTE.format(days=settings.agent_call_retention_days)


def event_type_options(db: Session, limit: int = 40) -> list[str]:
    """The event actions this ledger actually records, busiest first.

    Measured rather than typed: a filter offering actions this deployment never records is a
    control that does nothing, and one missing an action it does record silently narrows the
    export.
    """
    rows = db.execute(
        select(Event.action).group_by(Event.action)
        .order_by(func.count(Event.id).desc()).limit(limit)
    ).scalars().all()
    return [a for a in rows if a]


# ── endpoint validation ──────────────────────────────────────────────────────

def endpoint_problem(endpoint: str) -> tuple[str, str] | None:
    """`(error, detail)` for an endpoint that cannot be a collector, else `None`.

    The portless case gets its own code because it is the mistake the item calls out: an
    operator pastes `http://collector.internal` from a vendor's docs and every other check
    passes — scheme, host, the lot — so a generic "could not connect" sends them off to look
    at their network instead of at the five characters they forgot.
    """
    raw = (endpoint or "").strip()
    if not raw:
        return ("no_endpoint", "No endpoint — there is nowhere to send a batch.")
    try:
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https"):
            return ("bad_endpoint", f"Endpoint must start http:// or https:// — got {raw!r}.")
        if not parsed.hostname:
            return ("bad_endpoint", f"No host in endpoint {raw!r}.")
        port = parsed.port
    except ValueError:
        # `urlparse` raises on a malformed port ("http://h:abc") and on a few bracket cases.
        return ("bad_endpoint", f"Not a URL: {raw!r}.")
    if port is None:
        return (NO_PORT_ERROR, NO_PORT_DETAIL)
    return None


# ── redaction and records ────────────────────────────────────────────────────

class Redaction:
    """The three redaction choices, resolved once.

    Carried as a value rather than read off the config row at each call site so a sample built
    from UNSAVED panel state and a record built by the exporter go through exactly the same
    code — one owner for what leaves the box.
    """

    __slots__ = ("summaries", "client_ips", "api_keys")

    def __init__(self, *, summaries: bool, client_ips: bool, api_keys: bool):
        self.summaries = bool(summaries)
        self.client_ips = bool(client_ips)
        self.api_keys = bool(api_keys)

    @classmethod
    def of(cls, cfg: LogExportConfig, *, summaries: bool | None = None,
           client_ips: bool | None = None, api_keys: bool | None = None) -> "Redaction":
        return cls(
            summaries=cfg.redact_summaries if summaries is None else summaries,
            client_ips=cfg.redact_client_ips if client_ips is None else client_ips,
            api_keys=cfg.mask_api_keys if api_keys is None else api_keys)

    def dict(self) -> dict:
        return {"summaries": self.summaries, "client_ips": self.client_ips,
                "api_keys": self.api_keys}


def apply_redaction(attrs: dict, red: Redaction) -> dict:
    """Apply the three choices to one record's attributes.

    `scrub` runs on everything that survives, always, and none of the toggles turn it off: a
    collector is off-box, a strictly wider audience than this deployment's own database, and
    `scrub` is the redactor every other write path already inherits. The toggles add to it.

    Order is fixed — drop IP-named attributes, replace free text, mask the credential, then
    scrub the rest — so moving one toggle changes exactly that toggle.
    """
    out: dict[str, object] = {}
    for key, value in attrs.items():
        if value is None:
            continue
        name = key.rsplit(".", 1)[-1].lower()
        if name in IP_KEYS:
            # The client-IP toggle owns these attributes outright, and `scrub` does not run on
            # them: with the toggle ON the field is absent, and with it OFF the operator has
            # asked for the address, so replacing it with `[redacted:ip]` would send a
            # placeholder and report the choice as honoured. An IP *inside* other free text is
            # a different thing and is still scrubbed below.
            if red.client_ips:
                continue
            out[key] = str(value)
            continue
        if isinstance(value, str):
            if red.summaries and name in FREE_TEXT_KEYS:
                out[key] = "[redacted]"
                continue
            if red.api_keys and name in ("api_key", "api_key_prefix", "key"):
                out[key] = MASK_SENTINEL
                continue
            cleaned, _changed = scrub(value)
            out[key] = cleaned
        elif isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = value
        else:
            cleaned, _changed = scrub(json.dumps(value, default=str, sort_keys=True))
            out[key] = cleaned
    return out


def _project_names(db: Session, ids: set[str]) -> dict[str, tuple[str, str]]:
    if not ids:
        return {}
    rows = db.execute(select(Project.id, Project.tag, Project.name)
                      .where(Project.id.in_(ids))).all()
    return {pid: (tag or pid, name or "") for pid, tag, name in rows}


def _key_prefix(db: Session, key_id: str, cache: dict[str, str]) -> str:
    """`ApiKey.prefix` is already the display form (`gb_sk_ab12`) — the secret is only ever
    `hashed_key`. So this is not about leaking a key; it is about whether an off-box collector
    learns WHICH credential acted, which is what the toggle is for."""
    if not key_id:
        return ""
    if key_id not in cache:
        row = db.get(ApiKey, key_id)
        cache[key_id] = row.prefix if row is not None else ""
    return cache[key_id]


def record_attributes(*, project_id: str | None, project: tuple[str, str] | None,
                      actor_type: str, actor_label: str, api_key_prefix: str, surface: str,
                      action: str, target_type: str, target_id: str,
                      meta: dict | None = None) -> dict:
    """The attribute set one exported record carries, BEFORE redaction.

    `gb.project` is why the config is not project-scoped: a batch spans projects, so the record
    names whose event it was rather than the export being split per project. `gb.scope` says
    when there is no project to name, so an absent `gb.project` is a statement and not a gap.
    """
    meta = meta or {}
    tag, name = project or ("", "")
    attrs: dict[str, object] = {
        "gb.scope": "project" if project_id else "deployment",
        "gb.actor.type": actor_type,
        "gb.actor": actor_label,
        "gb.surface": surface,
        "gb.action": action,
        "gb.target.type": target_type,
        "gb.target.id": target_id,
        "gb.api_key": api_key_prefix,
    }
    if project_id:
        attrs["gb.project"] = tag
        attrs["gb.project_id"] = project_id
        attrs["gb.project_name"] = name
    # A client IP is on a record only when whoever wrote the event put one in `meta`; nothing
    # in this codebase does today. The toggle is still real — it governs whatever arrives —
    # and the synthetic sample carries one so the choice is visible before your data does.
    for key in META_IP_KEYS:
        if meta.get(key):
            attrs[f"gb.{key}"] = str(meta[key])
    for key in META_TEXT_KEYS:
        if meta.get(key) is not None:
            attrs[f"gb.{key}"] = str(meta[key])
    if meta.get("principal"):
        attrs["gb.principal"] = str((meta.get("principal") or {}).get("label") or "")
    if meta.get("agent_id"):
        attrs["gb.agent"] = str(meta["agent_id"])
    return attrs


def _severity(action: str) -> str:
    """The refusals the ledger records are WARN; everything else is INFO. `REFUSAL_ACTIONS` is
    events.py's tuple, referenced not copied — a second list would drift and a refusal would
    then export as routine."""
    return "WARN" if action in events_svc.REFUSAL_ACTIONS else "INFO"


def event_record(db: Session, e: Event, red: Redaction, projects: dict[str, tuple[str, str]],
                 keys: dict[str, str]) -> dict:
    attrs = record_attributes(
        project_id=e.project_id, project=projects.get(e.project_id or ""),
        actor_type=e.actor_type, actor_label=e.actor_label or e.actor_id,
        api_key_prefix=(_key_prefix(db, e.actor_id, keys) if e.actor_type == "apikey" else ""),
        surface=e.surface, action=e.action, target_type=e.target_type,
        target_id=e.target_id, meta=e.meta)
    return {
        "kind": "log",
        "timestamp": (_aware(e.ts) or _now()).isoformat(),
        "severity": _severity(e.action),
        "body": e.action,
        "attributes": apply_redaction(attrs, red),
    }


def call_record(db: Session, c: AgentCall, red: Redaction,
                projects: dict[str, tuple[str, str]], keys: dict[str, str]) -> dict:
    """One MCP call as a span. `AgentCall.target` is already a single allowlisted string and
    never the arguments (PRD-34 D4), so a trace built from it carries no free text to leak."""
    started = _aware(c.ts) or _now()
    duration = max(0, int(c.duration_ms or 0))
    attrs = record_attributes(
        project_id=c.project_id, project=projects.get(c.project_id or ""),
        actor_type="apikey", actor_label=c.agent_id or "",
        api_key_prefix=_key_prefix(db, c.api_key_id, keys),
        surface="mcp", action=c.tool or "tool_call", target_type="tool",
        target_id=c.target or "")
    attrs["gb.ok"] = bool(c.ok)
    if c.error_code:
        attrs["gb.error_code"] = str(c.error_code)
    attrs["gb.duration_ms"] = duration
    return {
        "kind": "trace",
        "timestamp": started.isoformat(),
        "end_timestamp": (started + timedelta(milliseconds=duration)).isoformat(),
        "severity": "INFO" if c.ok else "WARN",
        "body": c.tool or "tool_call",
        "trace_id": _stable_id("agent_call", c.id, 32),
        "span_id": _stable_id("span", c.id, 16),
        "attributes": apply_redaction(attrs, red),
    }


def heartbeat_record(a: Agent, red: Redaction, projects: dict[str, tuple[str, str]]) -> dict:
    """One agent heartbeat as a gauge sample. Presence is derived from `last_seen_at` and never
    stored as a transition, so the exported metric is the same fact the roster reads."""
    attrs = record_attributes(
        project_id=a.project_id, project=projects.get(a.project_id or ""),
        actor_type="agent", actor_label=a.label or a.id, api_key_prefix="",
        surface="mcp", action="heartbeat", target_type="agent", target_id=a.id)
    attrs["gb.agent.state"] = a.state or ""
    attrs["gb.agent.role"] = a.active_role or ""
    return {
        "kind": "metric",
        "timestamp": (_aware(a.last_seen_at) or _now()).isoformat(),
        "severity": "INFO",
        "body": "gb.agent.heartbeat",
        "metric_name": "gb.agent.heartbeat",
        "metric_value": 1,
        "attributes": apply_redaction(attrs, red),
    }


def _stable_id(kind: str, ident, width: int) -> str:
    """A deterministic hex id, so re-exporting a row produces the same span.

    Random per attempt would make a retry look like a second call to whatever reads the
    collector, and a duplicated span is a count that is wrong in the reassuring direction.
    """
    return hashlib.sha256(f"{kind}:{ident}".encode()).hexdigest()[:width]


#: Attribute keys the synthetic sample carries, so an operator with an empty ledger still sees
#: what each redaction choice does.
_SYNTHETIC_ATTRS = {
    "gb.scope": "project", "gb.project": "GRPH", "gb.project_id": "prj_demo",
    "gb.project_name": "Graphban", "gb.actor.type": "apikey", "gb.actor": "loop @ macbook",
    "gb.surface": "mcp", "gb.action": "create_item", "gb.target.type": "item",
    "gb.target.id": "GRPH-1", "gb.api_key": "gb_sk_ab12", "gb.client_ip": "203.0.113.7",
    "gb.summary": "a free-text summary an agent wrote",
}


def sample_record(db: Session, red: Redaction) -> dict:
    """A live sample of what the next export would send, under the given redaction choices.

    Built from the newest REAL event, so it is a sample of this deployment. With an empty
    ledger it says so and marks itself synthetic, and uses the synthetic attribute set above so
    every toggle has something to act on — a made-up record presented as a real one would be
    the panel inventing data, which is the thing this PRD exists to stop.
    """
    e = db.scalars(select(Event).order_by(Event.id.desc()).limit(1)).first()
    if e is None:
        return {
            "kind": "log", "source": "synthetic",
            "note": "This ledger has no events yet, so this is the shape of a record, not one "
                    "of yours — and its values are invented so each redaction choice has "
                    "something to act on.",
            "timestamp": _now().isoformat(), "severity": "INFO", "body": "create_item",
            "attributes": apply_redaction(dict(_SYNTHETIC_ATTRS), red),
            "redaction": red.dict(),
        }

    keys: dict[str, str] = {}
    projects = _project_names(db, {e.project_id} if e.project_id else set())
    record = event_record(db, e, red, projects, keys)
    record["source"] = "event"
    record["note"] = ""
    record["event_id"] = e.id
    record["redaction"] = red.dict()
    return record


# ── OTLP wire format ─────────────────────────────────────────────────────────

def _otlp_value(v: object) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _otlp_attrs(attrs: dict) -> list[dict]:
    return [{"key": k, "value": _otlp_value(v)} for k, v in sorted(attrs.items())]


def _unix_nano(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1_000_000_000))


_RESOURCE = {"attributes": _otlp_attrs({"service.name": "graphban",
                                        "service.namespace": "agentledger"})}
_SCOPE = {"name": "graphban.logexport"}
_SIGNAL_PATH = {"log": "/v1/logs", "trace": "/v1/traces", "metric": "/v1/metrics"}


def otlp_payload(records: list[dict]) -> dict:
    """The OTLP/HTTP JSON body for one signal's records.

    Hand-built rather than pulled from `opentelemetry-proto`: the shape is small, the
    alternative is a dependency neither deployment has, and `providers/llm_meter.py` records
    the same call about OTel.
    """
    if not records:
        return {}
    kind = records[0]["kind"]
    if kind == "log":
        return {"resourceLogs": [{"resource": _RESOURCE, "scopeLogs": [{"scope": _SCOPE,
            "logRecords": [{
                "timeUnixNano": _unix_nano(r["timestamp"]),
                "severityText": r.get("severity", "INFO"),
                "body": {"stringValue": str(r.get("body", ""))},
                "attributes": _otlp_attrs(r.get("attributes") or {}),
            } for r in records]}]}]}
    if kind == "trace":
        return {"resourceSpans": [{"resource": _RESOURCE, "scopeSpans": [{"scope": _SCOPE,
            "spans": [{
                "traceId": r["trace_id"], "spanId": r["span_id"], "kind": 2,
                "name": str(r.get("body", "")),
                "startTimeUnixNano": _unix_nano(r["timestamp"]),
                "endTimeUnixNano": _unix_nano(r.get("end_timestamp") or r["timestamp"]),
                "attributes": _otlp_attrs(r.get("attributes") or {}),
                "status": {"code": 1 if (r.get("attributes") or {}).get("gb.ok", True) else 2},
            } for r in records]}]}]}
    return {"resourceMetrics": [{"resource": _RESOURCE, "scopeMetrics": [{"scope": _SCOPE,
        "metrics": [{
            "name": r.get("metric_name") or str(r.get("body", "")),
            "gauge": {"dataPoints": [{
                "asInt": str(int(r.get("metric_value", 1))),
                "timeUnixNano": _unix_nano(r["timestamp"]),
                "attributes": _otlp_attrs(r.get("attributes") or {}),
            }]},
        } for r in records]}]}]}


def signal_url(endpoint: str, kind: str) -> str:
    """`http://localhost:4318` + `log` → `http://localhost:4318/v1/logs`.

    The endpoint is the collector BASE, exactly as `OTEL_EXPORTER_OTLP_ENDPOINT` defines it —
    one rule, rather than guessing whether the operator typed a base or a signal URL.
    """
    return f"{(endpoint or '').strip().rstrip('/')}{_SIGNAL_PATH[kind]}"


def send_records(records: list[dict], *, endpoint: str, protocol: str, compression: str,
                 headers: dict | None = None, transport: httpx.BaseTransport | None = None,
                 timeout: float = 10.0) -> tuple[int, int]:
    """Hand `records` to the collector. Returns `(accepted, latency_ms)`; raises `SendFailed`.

    `transport` is the test seam: `httpx.MockTransport` answers without a socket, so the suite
    never depends on a collector being up. A test that needed a real one would pass on a
    machine that happens to have it and fail on CI — the worst kind of green.
    """
    if not records:
        return 0, 0
    problem = endpoint_problem(endpoint)
    if problem is not None:
        raise SendFailed(problem[0], problem[1])
    if protocol not in PROTOCOLS:
        raise BadConfig(f"protocol must be one of {', '.join(PROTOCOLS)}")
    if compression not in COMPRESSIONS:
        raise BadConfig(f"compression must be one of {', '.join(COMPRESSIONS)}")
    if protocol == "grpc":
        raise SendFailed("unsupported", GRPC_UNSUPPORTED)

    kind = records[0]["kind"]
    body = json.dumps(otlp_payload(records)).encode()
    sent_headers = {"Content-Type": "application/json"}
    if compression == "gzip":
        body = gzip.compress(body)
        sent_headers["Content-Encoding"] = "gzip"
    for name, value in (headers or {}).items():
        # httpx owns these; an operator-pasted one would corrupt the request or lie about its
        # length. Skipped rather than refused — a header list is copied from a vendor's docs
        # and this is not the operator's mistake to debug.
        if str(name).lower() in ("content-length", "host", "content-type"):
            continue
        sent_headers[str(name)] = str(value)

    url = signal_url(endpoint, kind)
    started = time.monotonic()
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            resp = client.post(url, content=body, headers=sent_headers)
    except httpx.TimeoutException as e:
        raise SendFailed("timeout",
                         f"Collector did not answer within {timeout:g}s: {e}") from None
    except httpx.HTTPError as e:
        raise SendFailed("unreachable",
                         f"Could not reach the collector at {url}: {e}") from None
    latency_ms = int((time.monotonic() - started) * 1000)

    if resp.status_code // 100 != 2:
        snippet = (resp.text or "")[:200]
        raise SendFailed("bad_status", f"Collector answered {resp.status_code} for {url}"
                         + (f": {snippet}" if snippet else ""))
    # `partialSuccess` is how OTLP reports "some records rejected". Counting those as sent
    # would make the strip's number higher than what the collector actually kept.
    rejected = 0
    try:
        data = resp.json() if resp.content else {}
        rejected = int((data.get("partialSuccess") or {}).get("rejectedRecords") or 0)
    except (ValueError, AttributeError, TypeError):
        rejected = 0
    return max(0, len(records) - rejected), latency_ms


# ── counters ─────────────────────────────────────────────────────────────────

def _batch_dict(b: LogExportBatch) -> dict:
    return {
        "ts": _aware(b.ts).isoformat() if b.ts else None,
        "kind": b.kind, "ok": bool(b.ok), "sent": int(b.sent or 0),
        "dropped": int(b.dropped or 0), "latency_ms": b.latency_ms,
        "error": b.error or "", "detail": b.detail or "", "endpoint": b.endpoint or "",
    }


def pending(db: Session, cfg: LogExportConfig | None = None) -> dict:
    """Records the exporter has not handed over yet, per signal.

    This is the queue. It is derived from the cursors and the source tables rather than held in
    memory, so it survives a restart and is a measurement — but see `status`: a depth is only
    REPORTED while the exporter is running, because the same number beside a dead exporter
    reads as an empty queue.
    """
    cfg = cfg or get_config(db)
    logs = traces = metrics = 0
    if cfg.send_events:
        conds = [Event.id > cfg.cursor_event_id]
        if cfg.event_types:
            conds.append(Event.action.in_(list(cfg.event_types)))
        logs = int(db.scalar(select(func.count(Event.id)).where(*conds)) or 0)
    if cfg.send_tool_calls:
        traces = int(db.scalar(select(func.count(AgentCall.id))
                               .where(AgentCall.id > cfg.cursor_call_id)) or 0)
    if cfg.send_heartbeats:
        conds = ([] if cfg.cursor_heartbeat_at is None
                 else [Agent.last_seen_at > cfg.cursor_heartbeat_at])
        metrics = int(db.scalar(select(func.count(Agent.id)).where(*conds)) or 0)
    return {"logs": logs, "traces": traces, "metrics": metrics,
            "total": logs + traces + metrics}


def failure_streak(db: Session) -> int:
    """Consecutive FAILED SENDS, newest first.

    Two kinds of row do not count, and both matter:

    - `kind="test"` is excluded by the query. A probe against a bad endpoint is the operator
      asking a question, not the exporter failing at its job.
    - a `backlog_dropped` row STOPS the count. It is the give-up marker, not another attempt —
      and counting it would leave the streak permanently at the ceiling, so every later drain
      would drop its backlog on sight and the exporter would never actually try the collector
      again. Breaking on it is what makes the next pass a fresh set of attempts.
    """
    streak = 0
    for ok, error in db.execute(
        select(LogExportBatch.ok, LogExportBatch.error)
        .where(LogExportBatch.kind == "export")
        .order_by(LogExportBatch.id.desc()).limit(MAX_CONSECUTIVE_FAILURES + 1)
    ).all():
        if ok or error == "backlog_dropped":
            break
        streak += 1
    return streak


def _unknown_status(note: str) -> dict:
    """The status when the CONFIG could not be read.

    Not `enabled: False` — that is the "off" the item warns about. A failed read has no idea
    whether export is on, and the reassuring default is the wrong one.
    """
    return {
        "enabled": None, "state": "unknown", "state_note": note, "exporter_running": False,
        "last_batch": None, "last_batch_state": "unavailable", "sent_24h": None,
        "dropped_24h": None, "queue_depth": None, "queue_state": "unavailable",
        "coverage": "unavailable", "note": note,
    }


def _last_batch(db: Session) -> LogExportBatch | None:
    """The newest batch row of either kind, or None when there has never been one."""
    return db.scalars(select(LogExportBatch).order_by(LogExportBatch.id.desc()).limit(1)).first()


def _window_totals(db: Session, since: datetime) -> tuple[int, int]:
    """`(sent, dropped)` over export batches in the window. Test batches are excluded: a probe
    is the operator asking a question, not this deployment's telemetry."""
    row = db.execute(
        select(func.coalesce(func.sum(LogExportBatch.sent), 0),
               func.coalesce(func.sum(LogExportBatch.dropped), 0))
        .where(LogExportBatch.kind == "export", LogExportBatch.ts >= since)
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


def status(db: Session, *, now: datetime | None = None) -> dict:
    """The status strip. Every field has a third answer besides a reassuring one.

    Each read is guarded SEPARATELY rather than the whole strip at once. One guard would mean a
    queue read that fails also blanks the 24h counters that read fine, and the panel would
    report less than it knows; three guards mean each number is independently measured or
    independently unknown, and each can be sabotaged on its own — which is what the item's
    "sabotage each" asks for. What must never happen is a guard turning into a zero, so a
    failure leaves `None`, names the read in `note`, and sets `coverage="unavailable"`.
    """
    now = _aware(now) or _now()
    try:
        cfg = get_config(db)
        enabled = bool(cfg.enabled)
    except Exception as e:  # noqa: BLE001 — unknown is not off
        logger.warning("log-export config read failed: %s", e)
        return _unknown_status(
            f"The export configuration could not be read ({e.__class__.__name__}), so this "
            "panel does not know whether export is on. That is not 'off'.")

    running = exporter_running()
    if not enabled:
        state = "paused"
        state_note = ("Export is off. Nothing is being sent, and the counters below are what "
                      "the last time it was on measured — not what is happening now.")
    elif running:
        state, state_note = "exporting", ""
    else:
        state = "not_running"
        state_note = ("Export is ON but no exporter is running in this process, so nothing is "
                      "draining. A queue depth here would be a backlog nobody is working down, "
                      "which is why it reads as unknown rather than as a number.")

    out: dict = {
        "enabled": enabled, "state": state, "state_note": state_note,
        "exporter_running": running,
        "last_batch": None, "last_batch_state": "never",
        "sent_24h": None, "dropped_24h": None, "queue_depth": None,
        "queue_state": "draining" if running else "exporter_not_running",
        "coverage": "full", "note": "",
    }
    failures: list[str] = []

    try:
        last = _last_batch(db)
        if last is not None:
            out["last_batch"] = _batch_dict(last)
            out["last_batch_state"] = "measured"
    except Exception as e:  # noqa: BLE001
        logger.warning("log-export last-batch read failed: %s", e)
        out["last_batch"] = None
        out["last_batch_state"] = "unavailable"
        failures.append(f"last batch ({e.__class__.__name__})")

    try:
        out["sent_24h"], out["dropped_24h"] = _window_totals(
            db, now - timedelta(seconds=WINDOW_SECONDS))
    except Exception as e:  # noqa: BLE001 — reported as unavailable, never as a zero
        logger.warning("log-export counter read failed: %s", e)
        out["sent_24h"] = None
        out["dropped_24h"] = None
        failures.append(f"24h counters ({e.__class__.__name__})")

    if not running:
        # Not an error and not a zero: there is no answer to give while nothing is draining.
        pass
    else:
        try:
            out["queue_depth"] = pending(db, cfg)["total"]
        except Exception as e:  # noqa: BLE001
            logger.warning("log-export queue read failed: %s", e)
            out["queue_depth"] = None
            out["queue_state"] = "unavailable"
            failures.append(f"queue depth ({e.__class__.__name__})")

    if failures:
        out["coverage"] = "unavailable"
        out["note"] = ("Could not read " + ", ".join(failures) + ", so those are shown as "
                       "unknown. That is not a zero, and it is not 'off'.")
    return out


# ── the exporter ─────────────────────────────────────────────────────────────

def _collect(db: Session, cfg: LogExportConfig, red: Redaction, *,
             limit: int = DRAIN_BATCH) -> tuple[dict[str, list], dict]:
    """Records past the cursors, grouped by signal kind, plus each group's high-water mark."""
    groups: dict[str, list] = {"log": [], "trace": [], "metric": []}
    project_ids: set[str] = set()
    events: list[Event] = []
    calls: list[AgentCall] = []
    agents: list[Agent] = []

    if cfg.send_events:
        conds = [Event.id > cfg.cursor_event_id]
        if cfg.event_types:
            conds.append(Event.action.in_(list(cfg.event_types)))
        events = list(db.scalars(select(Event).where(*conds).order_by(Event.id)
                                 .limit(limit)).all())
        project_ids |= {e.project_id for e in events if e.project_id}
    if cfg.send_tool_calls:
        calls = list(db.scalars(select(AgentCall).where(AgentCall.id > cfg.cursor_call_id)
                                .order_by(AgentCall.id).limit(limit)).all())
        project_ids |= {c.project_id for c in calls if c.project_id}
    if cfg.send_heartbeats:
        conds = ([] if cfg.cursor_heartbeat_at is None
                 else [Agent.last_seen_at > cfg.cursor_heartbeat_at])
        agents = list(db.scalars(select(Agent).where(*conds).order_by(Agent.last_seen_at)
                                 .limit(limit)).all())
        project_ids |= {a.project_id for a in agents if a.project_id}

    projects = _project_names(db, project_ids)
    keys: dict[str, str] = {}
    for e in events:
        groups["log"].append(event_record(db, e, red, projects, keys))
    for c in calls:
        groups["trace"].append(call_record(db, c, red, projects, keys))
    for a in agents:
        groups["metric"].append(heartbeat_record(a, red, projects))

    marks = {
        "event_id": max([e.id for e in events], default=cfg.cursor_event_id),
        "call_id": max([c.id for c in calls], default=cfg.cursor_call_id),
        "heartbeat_at": max([_aware(a.last_seen_at) for a in agents],
                            default=cfg.cursor_heartbeat_at),
    }
    return groups, marks


def _record_batch(db: Session, *, kind: str, ok: bool, sent: int, dropped: int,
                  latency_ms: int | None, error: str, detail: str, endpoint: str,
                  now: datetime | None = None) -> LogExportBatch:
    row = LogExportBatch(kind=kind, ok=ok, sent=sent, dropped=dropped, latency_ms=latency_ms,
                         error=(error or "")[:32], detail=(detail or "")[:400],
                         endpoint=(endpoint or "")[:512])
    if now is not None:
        row.ts = now
    db.add(row)
    return row


def _probe_records(db: Session, cfg: LogExportConfig, red: Redaction, limit: int = 3) -> list:
    """A few real records to probe with, so the collector is asked about the payload it will
    actually receive. An empty ledger still probes — one synthetic record — because "there was
    nothing to send" is not an answer to "can this box reach the collector"."""
    groups, _marks = _collect(db, cfg, red, limit=limit)
    records: list[dict] = []
    for kind in ("log", "trace", "metric"):
        records.extend(groups[kind])
    if records:
        return records[:limit]
    sample = sample_record(db, red)
    return [{k: v for k, v in sample.items()
             if k in ("kind", "timestamp", "severity", "body", "attributes")}]


def send_test_batch(db: Session, *, endpoint: str | None = None, protocol: str | None = None,
                    compression: str | None = None, headers: dict | None = None,
                    transport: httpx.BaseTransport | None = None,
                    now: datetime | None = None) -> dict:
    """`Send test batch` — the panel's sharpest absence case.

    A probe that has not been run must not look like one that passed, so this always returns a
    result object and never an empty one: `ok` is true only when the collector accepted
    records, and every failure carries a SPECIFIC `error` code and a sentence naming it. A
    portless endpoint is `no_port`, not a generic connection error, because the fix is five
    characters in the field above the button.

    Overrides let the operator test an endpoint they have not saved yet. The probe is written as
    a `kind="test"` batch row so `last_batch` shows it, and is excluded from `sent_24h` because
    a test record is not this deployment's telemetry.
    """
    cfg = get_config(db)
    red = Redaction.of(cfg)
    eff_endpoint = (cfg.endpoint if endpoint is None else endpoint).strip()
    eff_protocol = cfg.protocol if protocol is None else protocol
    eff_compression = cfg.compression if compression is None else compression
    eff_headers = ((cfg.headers or {}) if headers is None
                   else merge_headers(cfg.headers or {}, headers))
    now = _aware(now) or _now()

    problem = endpoint_problem(eff_endpoint)
    if problem is None and eff_protocol not in PROTOCOLS:
        problem = ("bad_protocol", f"protocol must be one of {', '.join(PROTOCOLS)}")
    if problem is None and eff_compression not in COMPRESSIONS:
        problem = ("bad_compression", f"compression must be one of {', '.join(COMPRESSIONS)}")
    if problem is None and eff_protocol == "grpc":
        problem = ("unsupported", GRPC_UNSUPPORTED)

    def fail(code: str, detail: str) -> dict:
        _record_batch(db, kind="test", ok=False, sent=0, dropped=0, latency_ms=None,
                      error=code, detail=detail, endpoint=eff_endpoint, now=now)
        db.commit()
        return {"ok": False, "ran": True, "error": code, "detail": detail, "records": None,
                "latency_ms": None, "endpoint": eff_endpoint}

    if problem is not None:
        return fail(problem[0], problem[1])

    probe = LogExportConfig(id=SINGLETON_ID, endpoint=eff_endpoint, protocol=eff_protocol,
                            compression=eff_compression, headers=eff_headers,
                            send_events=cfg.send_events, send_tool_calls=cfg.send_tool_calls,
                            send_heartbeats=cfg.send_heartbeats,
                            event_types=list(cfg.event_types or []),
                            cursor_event_id=0, cursor_call_id=0, cursor_heartbeat_at=None)
    records = _probe_records(db, probe, red)
    try:
        sent, latency_ms = send_records(records, endpoint=eff_endpoint, protocol=eff_protocol,
                                        compression=eff_compression, headers=eff_headers,
                                        transport=transport)
    except SendFailed as e:
        return fail(e.code, e.detail)
    except BadConfig as e:
        return fail("bad_config", str(e))

    detail = (f"{sent} of {len(records)} record(s) accepted in {latency_ms} ms "
              f"({eff_protocol}, {eff_compression} compression).")
    if sent < len(records):
        detail += " The collector rejected some of them."
    _record_batch(db, kind="test", ok=True, sent=sent, dropped=0, latency_ms=latency_ms,
                  error="", detail=detail, endpoint=eff_endpoint, now=now)
    db.commit()
    return {"ok": True, "ran": True, "error": "", "detail": detail, "records": sent,
            "attempted": len(records), "latency_ms": latency_ms, "endpoint": eff_endpoint}


def drain_once(db: Session, *, transport: httpx.BaseTransport | None = None,
               now: datetime | None = None, limit: int = DRAIN_BATCH) -> dict:
    """One exporter pass. The lifespan loop calls this; the tests call it directly, which is how
    `credential_retry` is tested too — a suite should not wait on a timer.

    Returns what it did, INCLUDING why it did nothing. `ran: False` with a `reason` is a
    different answer from `ran: True, sent: 0`: the first means the exporter is not exporting,
    the second means it exported and there was nothing to send.
    """
    now = _aware(now) or _now()
    cfg = get_config(db)
    if not cfg.enabled:
        return {"ran": False, "reason": "paused", "sent": 0, "dropped": 0}

    problem = endpoint_problem(cfg.endpoint)
    if problem is None and cfg.protocol == "grpc":
        problem = ("unsupported", GRPC_UNSUPPORTED)
    if problem is not None:
        _record_batch(db, kind="export", ok=False, sent=0, dropped=0, latency_ms=None,
                      error=problem[0], detail=problem[1], endpoint=cfg.endpoint, now=now)
        db.commit()
        return {"ran": True, "reason": problem[0], "detail": problem[1], "sent": 0,
                "dropped": 0}

    red = Redaction.of(cfg)
    groups, marks = _collect(db, cfg, red, limit=limit)
    groups = {k: v for k, v in groups.items() if v}
    total = sum(len(v) for v in groups.values())
    if total == 0:
        # No row. An empty pass every interval would bury `last_batch` under batches that sent
        # nothing, and "the last batch" would stop meaning anything.
        return {"ran": True, "reason": "empty", "sent": 0, "dropped": 0}

    if failure_streak(db) >= MAX_CONSECUTIVE_FAILURES:
        _record_batch(db, kind="export", ok=False, sent=0, dropped=total, latency_ms=None,
                      error="backlog_dropped",
                      detail=(f"{total} record(s) dropped after {MAX_CONSECUTIVE_FAILURES} "
                              "consecutive failed batches — counted, not silently kept."),
                      endpoint=cfg.endpoint, now=now)
        _advance(cfg, marks)
        db.commit()
        return {"ran": True, "reason": "backlog_dropped", "sent": 0, "dropped": total}

    sent = 0
    ok = True
    first_error = ""
    errors: list[str] = []
    started = time.monotonic()
    for kind, records in groups.items():
        try:
            accepted, _latency = send_records(records, endpoint=cfg.endpoint,
                                              protocol=cfg.protocol,
                                              compression=cfg.compression,
                                              headers=cfg.headers or {}, transport=transport)
            sent += accepted
            # Advance only the cursor whose signal landed, so a failure on one signal neither
            # loses the others' records nor re-sends them forever.
            if kind == "log":
                cfg.cursor_event_id = marks["event_id"]
            elif kind == "trace":
                cfg.cursor_call_id = marks["call_id"]
            else:
                cfg.cursor_heartbeat_at = marks["heartbeat_at"]
        except SendFailed as e:
            ok = False
            first_error = first_error or e.code
            errors.append(f"{kind}: {e.code} — {e.detail}")
    latency_ms = int((time.monotonic() - started) * 1000)

    _record_batch(db, kind="export", ok=ok, sent=sent, dropped=0, latency_ms=latency_ms,
                  error=first_error, detail="; ".join(errors)[:400],
                  endpoint=cfg.endpoint, now=now)
    db.commit()
    return {"ran": True, "reason": "" if ok else first_error, "sent": sent, "dropped": 0,
            "ok": ok, "latency_ms": latency_ms, "attempted": total}


def _advance(cfg: LogExportConfig, marks: dict) -> None:
    cfg.cursor_event_id = marks["event_id"]
    cfg.cursor_call_id = marks["call_id"]
    cfg.cursor_heartbeat_at = marks["heartbeat_at"]


def run_once(db: Session) -> int:
    """The loop's entry point: one pass, returning how many records moved.

    Never raises. A collector being down must not take the API process with it, and a task that
    dies silently leaves `enabled` saying something the box is not doing.
    """
    try:
        return int(drain_once(db).get("sent") or 0)
    except Exception:  # noqa: BLE001
        logger.warning("log export pass failed; the next interval will retry", exc_info=True)
        db.rollback()
        return 0


def view(db: Session) -> dict:
    """One response for the panel: config, status, sample, and the choices it offers.

    A single response so the panel cannot show a config from one moment beside counters from
    another. Two reads of one thing are free to disagree, and the disagreement lands on exactly
    the numbers an operator is trying to trust.
    """
    cfg = get_config(db)
    problem = endpoint_problem(cfg.endpoint)
    return {
        "config": config_dict(cfg),
        "status": status(db),
        "sample": sample_record(db, Redaction.of(cfg)),
        "protocols": protocol_support(),
        "compressions": list(COMPRESSIONS),
        "event_type_options": event_type_options(db),
        "event_types_all": not bool(cfg.event_types),
        "retention_note": retention_note(),
        "catch_up_note": CATCH_UP_NOTE,
        "endpoint_problem": ({"error": problem[0], "detail": problem[1]} if problem else None),
        "hosted": bool(settings.hosted_mode),
        "writable": not settings.hosted_mode,
    }
