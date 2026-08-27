"""Authorization boundary — who may touch which project.

Authentication (security/deps.py) proves identity; this module decides
authority. Enforcement lives at the boundaries (the MCP dispatcher and REST
routers); services stay principal-free so domain logic keeps one owner.

Semantics (the Membership model the app always had, now enforced):

- ``Membership.access``: ``"write"`` | ``"read"`` | ``"none"`` (explicit denial).
  A user may read projects where access != "none" and write where access ==
  "write". No membership row means no access.
- An agent API key is bounded by BOTH its declared ``scopes`` (read/write/sync/gate) and
  its owner's memberships — a key can never out-rank the user who minted it.
  A project-scoped key (``project_id`` set) is further bounded to that project.

MCP maps :class:`Forbidden` to an ``unauthorized`` tool error; REST uses
:func:`require_readable` / :func:`require_writable`, which raise existence-hiding
404s so non-members can't probe which projects or resources exist.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ApiKey, Membership, OrgMembership, Project


class Forbidden(Exception):
    """Authenticated but not authorized for the attempted operation.

    `hint` is the machine-readable next step (AL-47), so a refused agent can branch without
    parsing prose — "your work moves to review; a reviewer takes it from there" is actionable
    in a way that a message alone is not. Optional, so every existing `raise Forbidden(msg)`
    is unchanged; PRD-17 D2 asked for the hint without a new error class or a new code, and
    this is the whole of that.
    """

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


# ---- Organization authority (hosted-only, AL-74b) ------------------------------
# The project gate above answers "which projects may this user touch". These answer
# "which orgs is this user in, and at what rank" — the authority behind the org
# router (create projects under an org, invite members, manage seats). Roles are
# ordered owner > admin > member; owner is the org creator and can never be demoted
# out of existence here (that's a billing/transfer concern, deferred to AL-75).

_ORG_RANK = {"member": 0, "admin": 1, "owner": 2}


def org_ids_for_user(db: Session, user_id: str) -> list[str]:
    """Every org the user holds a seat in."""
    return list(
        db.execute(select(OrgMembership.org_id).where(OrgMembership.user_id == user_id)).scalars()
    )


def org_role(db: Session, user_id: str, org_id: str) -> str | None:
    """The user's role in the org, or None if they hold no seat."""
    return db.scalar(
        select(OrgMembership.role).where(
            OrgMembership.org_id == org_id, OrgMembership.user_id == user_id
        )
    )


def require_org_member(db: Session, user_id: str, org_id: str) -> str:
    """Guard: the user must hold a seat in the org. 404 (not 403) hides existence so
    a non-member can't probe which orgs exist. Returns the caller's role."""
    role = org_role(db, user_id, org_id)
    if role is None:
        raise HTTPException(404, "organization not found")
    return role


def require_org_admin(db: Session, user_id: str, org_id: str) -> str:
    """Guard for org administration (invite/revoke/manage). Needs owner or admin;
    a plain member gets an honest 403 (they can already see the org)."""
    role = require_org_member(db, user_id, org_id)
    if _ORG_RANK.get(role, 0) < _ORG_RANK["admin"]:
        raise HTTPException(403, "requires an owner or admin role in this organization")
    return role


def _member_project_ids(db: Session, user_id: str, levels: tuple[str, ...]) -> list[str]:
    q = (
        select(Membership.project_id)
        .join(Project, Project.id == Membership.project_id)
        .where(Membership.user_id == user_id, Membership.access.in_(levels))
    )
    if settings.hosted_mode:
        # Hosted-only second gate (AL-74): the project must belong to an org the
        # caller is a member of. A per-project Membership alone is not enough, so a
        # project can never be reached from outside its tenant org. Self-host
        # (hosted_mode off) skips this entirely — behavior is unchanged.
        user_orgs = select(OrgMembership.org_id).where(OrgMembership.user_id == user_id)
        q = q.where(Project.org_id.in_(user_orgs))
    q = q.order_by(Project.name)  # deterministic default-project resolution
    return list(db.execute(q).scalars())


def readable_project_ids(db: Session, user_id: str) -> list[str]:
    return _member_project_ids(db, user_id, ("read", "write"))


def writable_project_ids(db: Session, user_id: str) -> list[str]:
    return _member_project_ids(db, user_id, ("write",))


def key_readable_ids(db: Session, key: ApiKey) -> list[str]:
    """Projects this key may read: its scope project (if the owner can read it),
    else all projects the owner can read."""
    readable = readable_project_ids(db, key.user_id)
    if key.project_id:
        return [key.project_id] if key.project_id in readable else []
    return readable


def key_writable_ids(db: Session, key: ApiKey) -> list[str]:
    """Projects this key may write: requires the 'write' scope on the key AND
    write access for the key's owner (a key never out-ranks its minter)."""
    if "write" not in (key.scopes or []):
        return []
    writable = writable_project_ids(db, key.user_id)
    if key.project_id:
        return [key.project_id] if key.project_id in writable else []
    return writable


def key_sync_ids(db: Session, key: ApiKey) -> list[str]:
    """Projects this key may SYNC a code graph into (AL-137): requires the 'sync' scope
    AND write access for the key's owner. This is the server-side resolution of the ingest
    target — the push lands in the key's OWN project, never a client-supplied one, so a sync
    can't cross into another tenant's workspace (the isolation invariant, AL-76/AL-95)."""
    if "sync" not in (key.scopes or []):
        return []
    writable = writable_project_ids(db, key.user_id)
    if key.project_id:
        return [key.project_id] if key.project_id in writable else []
    return writable


def key_gate_ids(db: Session, key: ApiKey) -> list[str]:
    """Projects this key may ATTEST completion in (GRPH-541): requires the 'gate' scope AND
    write access for the key's owner.

    The point of a separate scope is that an ordinary agent key does not carry it, so an
    agent cannot manufacture the proof that its own work is finished. Completion stops
    depending on agents behaving and starts depending on what their credential can express.

    Shaped exactly like `key_sync_ids` above rather than as a new pattern: same bounding by
    the owner's memberships, so a gate key still cannot out-rank the human who minted it,
    and same project pinning, so an attestation cannot be written into another tenant's
    workspace.
    """
    if "gate" not in (key.scopes or []):
        return []
    writable = writable_project_ids(db, key.user_id)
    if key.project_id:
        return [key.project_id] if key.project_id in writable else []
    return writable


def can_read(db: Session, user_id: str, project_id: str | None) -> bool:
    return project_id is not None and project_id in readable_project_ids(db, user_id)


def can_write(db: Session, user_id: str, project_id: str | None) -> bool:
    return project_id is not None and project_id in writable_project_ids(db, user_id)


def require_readable(db: Session, user_id: str, project_id: str | None, what: str = "project") -> None:
    """REST guard: 404 (not 403) so non-members can't probe resource existence.
    A None/omitted project_id fails closed (no cross-tenant "list everything")."""
    if not can_read(db, user_id, project_id):
        raise HTTPException(404, f"{what} not found")


def require_writable(db: Session, user_id: str, project_id: str | None, what: str = "project") -> None:
    """REST guard for mutations. 404 hides existence; a read-only member gets the
    honest 403 (they can already see the resource)."""
    if can_write(db, user_id, project_id):
        return
    if can_read(db, user_id, project_id):
        raise HTTPException(403, f"read-only access to this {what}'s project")
    raise HTTPException(404, f"{what} not found")
