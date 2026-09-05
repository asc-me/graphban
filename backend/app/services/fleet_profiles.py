"""Harness preferences and fleet policy (PRD-37 D3, D4, D9, D14).

The server STORES and SERVES. It never resolves: the matrix of what runs where is a file in
the supervisor's repository, and the supervisor holds the machine the harness must be
installed on. So this module validates shapes, answers "whose profile applies" (the API
key's owner, with a project override winning over the default) and hands the result to the
two payloads a supervisor already reads — `fleet_status` and `get_item_details.brief`.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FleetProfile, Project, utcnow

AXES: tuple[str, ...] = ("cost", "quality", "latency", "locality")
POLICY_KEYS: tuple[str, ...] = ("local_only", "reviewer_cross_vendor", "allowed_harnesses")
MAX_NAMES = 32


class ProfileInvalid(ValueError):
    """A profile or policy that says something it may not — refused before the write."""


# ---- validation --------------------------------------------------------------------------

def _names(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProfileInvalid(f"{what} must be a list of harness names")
    out: list[str] = []
    for v in value:
        if not isinstance(v, str) or not v.strip():
            raise ProfileInvalid(f"{what} entries must be non-empty strings")
        name = v.strip()
        if name in out:
            raise ProfileInvalid(f"{what} lists {name!r} twice")
        out.append(name)
    if len(out) > MAX_NAMES:
        raise ProfileInvalid(f"{what} lists more than {MAX_NAMES} names")
    return out


def _weights(value: Any) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileInvalid("weights must be an object of axis -> 0..1")
    out: dict[str, float] = {}
    for k, v in value.items():
        if k not in AXES:
            raise ProfileInvalid(f"unknown weight axis {k!r}; axes are {list(AXES)}")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ProfileInvalid(f"weight {k} must be a number between 0 and 1")
        if not 0.0 <= float(v) <= 1.0:
            raise ProfileInvalid(f"weight {k} must be between 0 and 1, got {v}")
        out[k] = float(v)
    return out


def normalise_policy(raw: Any) -> dict | None:
    """The stored shape, or None when nothing constrains. Unknown keys are refused rather
    than kept: a misspelt `local_onyl` that stored silently would read as no constraint."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProfileInvalid("policy must be an object")
    for k in raw:
        if k not in POLICY_KEYS:
            raise ProfileInvalid(f"unknown policy key {k!r}; keys are {list(POLICY_KEYS)}")
    local_only = raw.get("local_only", False)
    cross = raw.get("reviewer_cross_vendor", False)
    if not isinstance(local_only, bool) or not isinstance(cross, bool):
        raise ProfileInvalid("local_only and reviewer_cross_vendor must be booleans")
    allowed = _names(raw.get("allowed_harnesses"), "allowed_harnesses")
    if not local_only and not cross and not allowed:
        return None
    return {"local_only": local_only, "reviewer_cross_vendor": cross, "allowed_harnesses": allowed}


# ---- profiles ----------------------------------------------------------------------------

def _find(db: Session, user_id: str, project_id: str | None) -> FleetProfile | None:
    stmt = select(FleetProfile).where(FleetProfile.user_id == user_id)
    stmt = stmt.where(FleetProfile.project_id == project_id) if project_id else stmt.where(FleetProfile.project_id.is_(None))
    return db.scalars(stmt).first()


def summary(row: FleetProfile) -> dict:
    return {
        "user": row.user_id,
        "project_id": row.project_id,
        "scope": "project" if row.project_id else "default",
        "defaults": list(row.defaults or []),
        "weights": dict(row.weights or {}),
        "excludes": list(row.excludes or []),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def effective(db: Session, user_id: str | None, project_id: str | None) -> dict | None:
    """The profile that RESOLVES for this user in this project (D14): the project override
    when one exists, else the default, else None — and None is what the payload says, so a
    supervisor never mistakes "no taste recorded" for "prefers nothing"."""
    if not user_id:
        return None
    row = (_find(db, user_id, project_id) if project_id else None) or _find(db, user_id, None)
    return summary(row) if row is not None else None


def both(db: Session, user_id: str, project_id: str | None) -> dict:
    """What the Fleet view edits: the default and the override side by side, plus which one
    is in force."""
    default = _find(db, user_id, None)
    override = _find(db, user_id, project_id) if project_id else None
    return {
        "default": summary(default) if default else None,
        "override": summary(override) if override else None,
        "profile": effective(db, user_id, project_id),
    }


def set_profile(db: Session, *, user_id: str, project_id: str | None, defaults: Any,
                weights: Any, excludes: Any) -> FleetProfile:
    names = _names(defaults, "defaults")
    w = _weights(weights)
    ex = _names(excludes, "excludes")
    overlap = sorted(set(names) & set(ex))
    if overlap:
        raise ProfileInvalid(f"{overlap} are both in defaults and excludes")
    row = _find(db, user_id, project_id)
    if row is None:
        row = FleetProfile(id=str(uuid4()), user_id=user_id, project_id=project_id)
        db.add(row)
    row.defaults, row.weights, row.excludes, row.updated_at = names, w, ex, utcnow()
    db.commit()
    db.refresh(row)
    return row


def clear_profile(db: Session, *, user_id: str, project_id: str | None) -> bool:
    row = _find(db, user_id, project_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


# ---- policy ------------------------------------------------------------------------------

def policy_of(db: Session, project_id: str | None) -> dict | None:
    if not project_id:
        return None
    project = db.get(Project, project_id)
    if project is None:
        return None
    try:
        return normalise_policy(project.fleet_policy)
    except ProfileInvalid:
        # A row written by hand outside the API: still served, so the operator sees it.
        return project.fleet_policy


def set_policy(db: Session, project: Project, raw: Any) -> dict | None:
    project.fleet_policy = normalise_policy(raw)
    db.commit()
    db.refresh(project)
    return project.fleet_policy


def attach(db: Session, payload: dict, *, user_id: str | None, project_id: str | None) -> dict:
    """Put `profile` and `policy` on a result payload (D9). Both keys are always present so
    their absence can never be mistaken for a transport that dropped them."""
    payload["profile"] = effective(db, user_id, project_id)
    payload["policy"] = policy_of(db, project_id)
    return payload
