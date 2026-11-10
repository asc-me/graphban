"""The box's org-view push (GBC-P8 S7 / GRPH-1454).

One request per sync, carrying every project on this box. The console
(`POST /api/sync/box-push`) accepts counts and rates only: no titles, no
prose, no id that is not a rule slug. A replay of the same payload reuses
its idempotency key. A refusal is logged and not retried until the payload
changes. A network failure is resent in full on the next sync.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Observation, Project, RuleDecisionPull
from app.services import rule_measure
from app.services import rule_pack
from app.services import rule_toggles

logger = logging.getLogger("graphban.box_push")

_TIMEOUT = 30.0
_RULE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_REASON_CODE = re.compile(r"^[a-z][a-z_]{0,47}$")
_STATES = ("on", "off", "advisory")

#: Payload hash the console last refused. Same process, same payload: do not post again.
_refused_key: str | None = None


def reset_refusal() -> None:
    """Tests start from a box that has not just been refused."""
    global _refused_key
    _refused_key = None


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


def build_box_push(db: Session) -> dict | None:
    """The console's `box_push` record, or None when this box has no projects.

    A project name that is shared by two projects on the box is left out and
    logged. Sending it twice, or sending both, is a 422 for the whole push.
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
    for name, project_id in seen.items():
        projects.append({
            "project": name,
            "rules": [_rule(db, project_id, rid) for rid in rule_ids],
        })
    if not projects:
        return None
    return {"record": "box_push", "projects": projects}


def payload_key(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def push_box_view(db: Session, *, cloud_url: str, api_key: str) -> dict:
    """POST the org-view push. Never raises for a refusal; a network error does."""
    global _refused_key
    body = build_box_push(db)
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
    return {"pushed": True, "projects": [p["project"] for p in body["projects"]],
            "idempotency_key": key}
