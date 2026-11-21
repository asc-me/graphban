"""Org-scoped handles (GRPH-1787, P93 S1): the one service every handle path calls.

A handle is unique per org (`OrgMembership.handle`). On a self-hosted box with no orgs the
account handle (`User.handle`) is unique on the box, enforced here because the column is no
longer globally unique.
"""
from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import HandleHold, OrgMembership, Project, User, utcnow
from app.services.projects import normalize_hex, normalize_pattern, normalize_second_colour

HOLD_DAYS = 30
RESERVED = ("admin", "root", "support", "graphban")
_HANDLE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate(handle: str) -> str:
    """3-24 chars of lowercase letters, digits and single hyphens, none at either end.

    Raises ValueError("reserved") for a reserved word, ValueError("invalid") otherwise.
    """
    if not isinstance(handle, str) or not (3 <= len(handle) <= 24) or not _HANDLE.fullmatch(handle):
        raise ValueError("invalid")
    if handle in RESERVED:
        raise ValueError("reserved")
    return handle


def check_free(
    db: Session, handle: str, org_ids: list[str], exclude_user: str | None = None
) -> str | None:
    """The first org in `org_ids` where `handle` is taken or held, else None (free).

    Reads ONLY the given orgs. A live membership and an unexpired hold answer the same
    way, so the result never says which. `exclude_user` is the asker: their own seat and
    their own holds do not count against them.
    """
    if not org_ids:
        return None
    now = utcnow()
    taken = select(OrgMembership.org_id).where(
        OrgMembership.org_id.in_(org_ids), OrgMembership.handle == handle)
    held = select(HandleHold.org_id).where(
        HandleHold.org_id.in_(org_ids), HandleHold.handle == handle,
        HandleHold.expires_at > now)
    if exclude_user is not None:
        taken = taken.where(OrgMembership.user_id != exclude_user)
        held = held.where(HandleHold.user_id != exclude_user)
    busy = set(db.scalars(taken)) | set(db.scalars(held))
    # Order by the caller's list, but trust only what the org-filtered queries returned.
    return min(busy, key=org_ids.index, default=None)


def box_handle_taken(db: Session, handle: str, exclude_user: str | None = None) -> bool:
    """Self-hosted: is this account handle already used on the box?"""
    q = select(User.id).where(User.handle == handle)
    if exclude_user is not None:
        q = q.where(User.id != exclude_user)
    return db.scalar(q.limit(1)) is not None


def org_handle(db: Session, user: User, org_id: str | None) -> str:
    """The handle `user` goes by in `org_id`, falling back to the account handle."""
    if org_id:
        found = db.scalar(select(OrgMembership.handle).where(
            OrgMembership.org_id == org_id, OrgMembership.user_id == user.id))
        if found:
            return found
    return user.handle


def project_handle(db: Session, user: User, project_id: str | None) -> str:
    """`org_handle` for the org that owns `project_id` (account handle when it has none)."""
    if not project_id:
        return user.handle
    # One query: this runs on every authenticated request that names a project (deps.py).
    found = db.scalar(select(OrgMembership.handle).join(
        Project, Project.org_id == OrgMembership.org_id).where(
        Project.id == project_id, OrgMembership.user_id == user.id))
    return found or user.handle


def write_holds(
    db: Session, user: User, old_handle: str, org_ids: list[str]
) -> list[HandleHold]:
    """Reserve `old_handle` in each org for 30 days. Cloud (hosted) only: a self-hosted
    box writes none."""
    if not settings.hosted_mode:
        return []
    expires = utcnow() + timedelta(days=HOLD_DAYS)
    rows = [HandleHold(org_id=o, handle=old_handle, user_id=user.id, expires_at=expires)
            for o in org_ids]
    db.add_all(rows)
    db.flush()
    return rows


def apply_dot(user: User, changed: dict) -> None:
    """Write avatar / avatar2 / avatar_pattern with the project tile's validators.

    `avatar2` equal to `avatar` is stored null; a pattern outside `TILE_PATTERNS` raises.
    """
    if "avatar" in changed:
        user.avatar = normalize_hex(changed["avatar"], field="avatar")
    if "avatar2" in changed or "avatar" in changed:
        user.avatar2 = normalize_second_colour(
            user.avatar, changed.get("avatar2", user.avatar2))
    if "avatar_pattern" in changed:
        user.avatar_pattern = normalize_pattern(changed["avatar_pattern"])
