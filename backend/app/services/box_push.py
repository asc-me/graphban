"""The box's org-view push (GBC-P8 S7 / GRPH-1454).

One request per sync, carrying every project on this box. The console
(`POST /api/sync/box-push`) accepts counts and rates only: no titles, no
prose, no id that is not a rule slug. A replay of the same payload reuses
its idempotency key. A refusal is logged and not retried until the payload
changes. A network failure is resent in full on the next sync.

GRPH-1675: each project entry may also carry `counts` (items by status, open claims,
active agents — integers only) and `publishing` (the project's `publish_to_org` flags).
Both are new fields, and a console that predates them refuses unknown fields, so they are
sent only when the console advertises `box_push_counts` on its `/api/config`. A console
that says nothing, or cannot be read, gets the push without them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from datetime import datetime, timedelta, timezone

from app.models import Agent, Item, Observation, Project, RuleDecisionPull, SyncProject
from app.services import rule_measure
from app.services import rule_pack
from app.services import rule_proposals
from app.services import rule_toggles

logger = logging.getLogger("graphban.box_push")

_TIMEOUT = 30.0
_RULE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_REASON_CODE = re.compile(r"^[a-z][a-z_]{0,47}$")
_STATES = ("on", "off", "advisory")
#: The feature a console advertises when it accepts `counts` and `publishing` (GBC-170).
COUNTS_FEATURE = "box_push_counts"
_ITEM_STATUSES = ("backlog", "next", "in_progress", "review", "done", "blocked")
_PROBE_TIMEOUT = 10.0

#: Payload hash the console last refused. Same process, same payload: do not post again.
_refused_key: str | None = None


#: GBC-279: the receipt capability that lets a push carry `build` and `failing`. Until a
#: receipt has named it (and a legacy sync key's never does) neither field is sent.
BUILD_CAPABILITY = "box_build"
_box_build_seen = False
#: Start of the current run of failed project pushes; None while nothing is failing.
_failing_since: datetime | None = None
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_STAGE = "project_push"
_REASON_MAX = 280
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def reset_refusal() -> None:
    """Tests start from a box that has not just been refused."""
    global _refused_key, _box_build_seen, _failing_since
    _refused_key = None
    _box_build_seen = False
    _failing_since = None


def _build() -> dict:
    from app.config import settings
    from app.version import __version__

    out = {"version": __version__}
    sha = settings.resolved_git_sha
    if _SHA.match(sha):
        out["git_sha"] = sha
    return out


def _trim_reason(reason: str | None) -> str:
    text = _CONTROL.sub(" ", reason or "").strip() or "unknown"
    return text[:_REASON_MAX]


def _iso(dt: datetime) -> str:
    return _aware(dt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failing(db: Session) -> dict | None:
    """The contract's `failing`: set while any synced project's latest push was refused or
    unknown, else None. Triage and spec-revision reports never touch these rows."""
    global _failing_since
    rows = list(db.scalars(select(SyncProject).where(
        SyncProject.last_push_result.in_(("refused", "unknown")))))
    if not rows:
        _failing_since = None
        return None
    rows.sort(key=lambda r: (_aware(r.last_push_at) or datetime.max.replace(tzinfo=timezone.utc),
                             r.local_project_id))
    first = _aware(rows[0].last_push_at) or datetime.now(timezone.utc)
    if _failing_since is None or first < _failing_since:
        _failing_since = first
    return {"since": _iso(_failing_since), "stage": _STAGE,
            "reason": _trim_reason(rows[-1].last_push_reason)}


def _state(on: bool, enforcement: str) -> str:
    if not on:
        return "off"
    if enforcement == "advisory":
        return "advisory"
    return "on"


def _reason_code(status: str, reason: object) -> str | None:
    """The console accepts a reason code or null, never the prose the measure layer stores."""
    if status == "measured":
        return None
    if isinstance(reason, str) and _REASON_CODE.match(reason):
        return reason
    if status == "insufficient":
        return "below_minimum_n"
    if isinstance(reason, str) and "adoption" in reason:
        return "no_adoption_time"
    return "no_measurement"


def _effect(cell: dict) -> dict:
    status = cell.get("status")
    if status not in ("measured", "insufficient", "unavailable"):
        status = "unavailable"
    value = cell.get("value")
    if status != "measured" or isinstance(value, bool) or not isinstance(value, (int, float)):
        value = None
    n = cell.get("n")
    min_n = cell.get("min_n")
    return {
        "status": status,
        "value": value,
        "n": n if isinstance(n, int) and n >= 0 else 0,
        "min_n": min_n if isinstance(min_n, int) and min_n >= 0 else 0,
        "reason": _reason_code(status, cell.get("reason")),
    }


def _profile_state(db: Session, project_id: str, rule_id: str) -> str | None:
    """The applied profile's pin for this rule, or null when no profile is applied."""
    profile_id = rule_measure.project_profile(db, project_id)
    if not profile_id:
        pull = db.get(RuleDecisionPull, "instance")
        profile_id = pull.profile if pull is not None else None
    pack = rule_pack.CURRENT
    prof = pack.profiles.get(profile_id) if pack is not None and profile_id else None
    if not isinstance(prof, dict):
        return None
    pin = (prof.get("rules") or {}).get(rule_id)
    if not isinstance(pin, dict):
        return None
    state = pin.get("state", "on")
    return state if state in _STATES else None


def _rule(db: Session, project_id: str, rule_id: str) -> dict:
    rule = rule_toggles.LIBRARY[rule_id]
    resolved = rule_toggles.resolve(db, rule_id, project_id)
    on = bool(resolved["on"]["value"])
    counts = rule_measure.project_counts(db, rule_id, project_id)
    rules = rule_measure.project_effect(db, project_id, rule_id).get("rules") or []
    summary = rules[0] if rules else {}
    after = (summary.get("escape_rate") or {}).get("after") or {}
    observations = db.scalar(select(func.count(Observation.id)).where(
        Observation.project_id == project_id, Observation.rule_id == rule_id)) or 0
    return {
        "rule_id": rule_id,
        "state": _state(on, rule.enforcement),
        "profile_state": _profile_state(db, project_id, rule_id),
        "overrides": len(counts["overrides"]),
        "adoption": 1.0 if on else 0.0,
        "observations": int(observations),
        "effect": _effect(after),
    }


def _advertises(data: object, feature: str) -> bool:
    """Does a console config body name `feature`? A list of feature names, a mapping of
    name to bool, under `features` or `capabilities`, or a top-level `true`."""
    if not isinstance(data, dict):
        return False
    if data.get(feature) is True:
        return True
    for field in ("features", "capabilities"):
        val = data.get(field)
        if isinstance(val, (list, tuple)) and feature in val:
            return True
        if isinstance(val, dict) and val.get(feature) is True:
            return True
    return False


def console_accepts_counts(cloud_url: str) -> bool:
    """Has the console advertised `box_push_counts`? Read from its public `/api/config`.

    Anything short of a 200 that names the feature is False: an older console, an error,
    a body that is not JSON. Sending the new fields to a console that refuses unknown fields
    would lose the whole push, so the default is the old shape.
    """
    try:
        resp = httpx.get(f"{cloud_url.rstrip('/')}/api/config", timeout=_PROBE_TIMEOUT)
        if getattr(resp, "status_code", None) != 200:
            return False
        return _advertises(resp.json(), COUNTS_FEATURE)
    except Exception:  # noqa: BLE001 — a probe failure means the old shape, never no push
        return False


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def project_counts(db: Session, project_id: str, *, now: datetime | None = None) -> dict:
    """The project's tracker in numbers: items by status, open claims, active agents.

    Integers only — no ids, titles or names leave the box. An open claim is a live build
    or review lease (an expired one frees the item and is not counted). An active agent is
    one whose presence has not lapsed and that is neither dismissed nor quarantined.
    """
    from app.services import fleet

    now = now or datetime.now(timezone.utc)
    by_status = dict(db.execute(
        select(Item.status, func.count(Item.id))
        .where(Item.project_id == project_id).group_by(Item.status)).all())
    items = {s: int(by_status.get(s, 0) or 0) for s in _ITEM_STATUSES}
    cutoff = now - timedelta(seconds=fleet.DEFAULT_LEASE_SECONDS)
    held = db.execute(select(Item.claimed_by, Item.claimed_at, Item.review_claimed_by,
                             Item.review_claimed_at).where(
        Item.project_id == project_id,
        (Item.claimed_by.is_not(None)) | (Item.review_claimed_by.is_not(None)))).all()
    open_claims = 0
    for build_by, build_at, review_by, review_at in held:
        if build_by and build_at is not None and _aware(build_at) >= cutoff:
            open_claims += 1
        if review_by and review_at is not None and _aware(review_at) >= cutoff:
            open_claims += 1
    agents = db.scalars(select(Agent).where(
        Agent.project_id == project_id, Agent.dismissed_at.is_(None))).all()
    active = sum(1 for a in agents
                 if fleet.presence_state(a, now=now) not in ("offline", "quarantined"))
    return {"items": items, "open_claims": open_claims, "active_agents": active}


def _publishing(project: Project) -> dict:
    return {"specs": bool(project.publish_specs),
            "item_digests": bool(project.publish_item_digests)}


def build_box_push(db: Session, *, with_counts: bool = False, with_build: bool = False) -> dict | None:
    """The console's `box_push` record, or None when this box has no projects.

    A project name that is shared by two projects on the box is left out and
    logged. Sending it twice, or sending both, is a 422 for the whole push.

    `with_counts` adds `counts` and `publishing` to each project (GRPH-1675). Only a console
    that advertised `box_push_counts` is sent them.
    """
    rows = list(db.scalars(select(Project).order_by(Project.name, Project.id)))
    seen: dict[str, str] = {}
    skipped: list[str] = []
    for row in rows:
        name = (row.name or "").strip()
        if not name:
            continue
        if name in seen:
            skipped.append(name)
            continue
        seen[name] = row.id
    if skipped:
        logger.warning("box push omitted projects whose names are not unique: %s",
                       ", ".join(sorted(set(skipped))))
        for name in set(skipped):
            seen.pop(name, None)
    rule_ids = [rid for rid in rule_toggles.LIBRARY if _RULE_ID.match(rid)]
    projects = []
    by_id = {row.id: row for row in rows}
    for name, project_id in seen.items():
        entry = {
            "project": name,
            "rules": [_rule(db, project_id, rid) for rid in rule_ids],
        }
        if with_counts:
            entry["counts"] = project_counts(db, project_id)
            # GRPH-1780: primitive gaps ride the same opt-in as the other counts, stage and
            # label only. Without it they stay on the box.
            gaps = rule_proposals.gap_counts(db, project_id)
            if gaps:
                entry["counts"]["primitive_gaps"] = gaps
            entry["publishing"] = _publishing(by_id[project_id])
        projects.append(entry)
    if not projects:
        return None
    out = {"record": "box_push", "projects": projects}
    if with_build:
        out["build"] = _build()
        out["failing"] = _failing(db)
    return out


def payload_key(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def push_box_view(db: Session, *, cloud_url: str, api_key: str) -> dict:
    """POST the org-view push. Never raises for a refusal; a network error does."""
    global _refused_key
    if db.scalar(select(func.count(Project.id))) == 0:
        return {"pushed": False, "reason": "no projects"}
    body = build_box_push(db, with_counts=console_accepts_counts(cloud_url),
                          with_build=_box_build_seen)
    if body is None:
        return {"pushed": False, "reason": "no projects"}
    key = payload_key(body)
    if key == _refused_key:
        return {"pushed": False, "refused": True, "repeated": True}
    resp = httpx.post(
        f"{cloud_url.rstrip('/')}/api/sync/box-push",
        json=body,
        headers={"X-API-Key": api_key, "Idempotency-Key": key},
        timeout=_TIMEOUT,
    )
    if resp.status_code == 422:
        try:
            failures = resp.json().get("failures") or []
        except ValueError:
            failures = []
        logger.warning("console refused the box push: %s", failures)
        _refused_key = key
        return {"pushed": False, "refused": True, "failures": failures}
    resp.raise_for_status()
    _refused_key = None
    _read_receipt(resp)
    return {"pushed": True, "projects": [p["project"] for p in body["projects"]],
            "idempotency_key": key}


def _read_receipt(resp: object) -> None:
    """Note whether the receipt's `capabilities` names `box_build`."""
    global _box_build_seen
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — a receipt we cannot read grants nothing
        return
    caps = data.get("capabilities") if isinstance(data, dict) else None
    _box_build_seen = isinstance(caps, (list, tuple)) and BUILD_CAPABILITY in caps


def publish_enabled(db: Session, project_id: str, flag: str) -> bool:
    """May this box publish `flag` ("specs" | "item_digests") for this project? (GRPH-1674)

    False on an unlinked box whatever the flags say, and False when the project opted out.
    Every spec or digest publish path asks this before it makes a call.
    """
    from app.services import code_sync, projects as projects_svc

    if code_sync.link_status(db)["linked"] is not True:
        return False
    project = db.get(Project, project_id)
    if project is None:
        return False
    return bool(getattr(project, projects_svc.PUBLISH_FLAGS[flag]))
