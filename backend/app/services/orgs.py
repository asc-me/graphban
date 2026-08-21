"""Organization + invite domain logic (hosted-only, AL-74b).

One owner for the org lifecycle: create an org (creator becomes owner), invite a
teammate by email, and accept an emailed invite (join the org). The org router is
the only caller; authority checks (who may invite) live in ``security.authz`` and
are applied at the router boundary. This module owns the *rules* of an invite —
single-use, email-bound, time-bounded — and raises :class:`HTTPException` for the
ones a caller can trip, so the router stays thin.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Membership,
    OrgInvite,
    OrgMembership,
    OrgRequest,
    Organization,
    Project,
    Team,
    TeamGrant,
    TeamMember,
    User,
    utcnow,
)
from app.security import authz
from app.services import email as email_svc
from app.services import quotas


# ---- founding additional orgs (AL-92) -----------------------------------------
def _has_standing_org_entitlement(db: Session, user: User) -> bool:
    """True when the user OWNS an org whose plan carries the multi-org entitlement.

    Attached to the plan rather than the user because billing lives on the org — an
    enterprise licence is what buys the right to run more tenants."""
    owned = db.scalars(
        select(OrgMembership.org_id).where(
            OrgMembership.user_id == user.id, OrgMembership.role == "owner"
        )
    )
    for org_id in owned:
        org = db.get(Organization, org_id)
        if org is not None and quotas.plan_of(org).may_found_additional_orgs:
            return True
    return False


def approved_org_grant(db: Session, user: User) -> OrgRequest | None:
    """An approved, unspent request authorizing one additional org."""
    return db.scalar(
        select(OrgRequest)
        .where(
            OrgRequest.user_id == user.id,
            OrgRequest.status == "approved",
            OrgRequest.consumed.is_(False),
        )
        .order_by(OrgRequest.created_at)
    )


def require_may_found_org(db: Session, user: User) -> OrgRequest | None:
    """Gate on founding an organization (hosted only; self-host is unlimited).

    Allowed when the caller belongs to no org yet (their first), holds a standing plan
    entitlement, or has an approved one-time request — which is returned so the caller
    can consume it *after* the org is successfully created. Otherwise 403 with a
    pointer to the request flow."""
    if not settings.hosted_mode:
        return None
    if not authz.org_ids_for_user(db, user.id):
        return None  # first org is free
    if _has_standing_org_entitlement(db, user):
        return None
    grant = approved_org_grant(db, user)
    if grant is not None:
        return grant
    raise HTTPException(
        403,
        "founding an additional organization needs approval — submit a request and an "
        "operator will review it (an enterprise plan grants this standing)",
    )


def consume_org_grant(db: Session, grant: OrgRequest) -> None:
    """Spend a one-time approval so it can't found a second org. Commits."""
    grant.consumed = True
    db.commit()


def submit_org_request(db: Session, user: User, reason: str, company: str) -> OrgRequest:
    """Ask an operator for permission to found an additional org. Commits.

    Idempotent while one is pending — re-submitting updates the existing request
    rather than queueing duplicates for the operator."""
    if not authz.org_ids_for_user(db, user.id) or _has_standing_org_entitlement(db, user):
        raise HTTPException(400, "you can already create an organization; no request needed")
    existing = db.scalar(
        select(OrgRequest).where(OrgRequest.user_id == user.id, OrgRequest.status == "pending")
    )
    req = existing or OrgRequest(id="oreq_" + uuid.uuid4().hex[:10], user_id=user.id)
    req.reason = (reason or "").strip()
    req.company = (company or "").strip()
    if existing is None:
        db.add(req)
    db.commit()
    db.refresh(req)
    return req


def latest_org_request(db: Session, user: User) -> OrgRequest | None:
    return db.scalar(
        select(OrgRequest)
        .where(OrgRequest.user_id == user.id)
        .order_by(OrgRequest.created_at.desc())
    )


def pending_org_requests(db: Session) -> list[OrgRequest]:
    return list(
        db.scalars(
            select(OrgRequest)
            .where(OrgRequest.status == "pending")
            .order_by(OrgRequest.created_at)
        )
    )


def decide_org_request(
    db: Session, req: OrgRequest, admin: User, approve: bool, note: str = ""
) -> OrgRequest:
    """Operator decision. Commits."""
    req.status = "approved" if approve else "denied"
    req.decided_at = utcnow()
    req.decided_by = admin.id
    req.decision_note = (note or "").strip()
    db.commit()
    db.refresh(req)
    return req


# ---- membership mutations (PRD-21 D8) -----------------------------------------
# The governance gap §3.5 named: until this existed, members arrived by accepting an
# invite and stayed forever, at the role the invite carried. D5 (teams and grants) cannot
# exist on top of an API that cannot write access, and a role system on top of a role
# nobody can change is nothing.
ORG_ROLES = ("owner", "admin", "member")
ACCESS_LEVELS = ("write", "read", "none")


def _seat(db: Session, org_id: str, user_id: str) -> OrgMembership:
    seat = db.scalar(
        select(OrgMembership).where(
            OrgMembership.org_id == org_id, OrgMembership.user_id == user_id
        )
    )
    if seat is None:
        raise HTTPException(404, "that person is not a member of this organization")
    return seat


ADMIN_ROLES = ("owner", "admin")


def _administrators(db: Session, org_id: str, *, lock: bool = False) -> list[OrgMembership]:
    """Every seat that can administer this org. `lock` takes them FOR UPDATE."""
    q = select(OrgMembership).where(
        OrgMembership.org_id == org_id, OrgMembership.role.in_(ADMIN_ROLES)
    )
    if lock:
        q = q.with_for_update()
    return list(db.scalars(q))


def _refuse_if_last_administrator(db: Session, org_id: str, seat: OrgMembership) -> None:
    """The floor: an org must always keep at least one owner-or-admin (PRD-21 D8.1).

    This replaces three separate prohibitions on the `owner` seat. They protected a proxy
    — that one specific person exists — rather than the property anyone actually needs,
    which is that **somebody can still administer the org**. The proxy is also what
    stranded an org whose owner departed, since ownership was neither transferable nor
    removable.

    Taken FOR UPDATE, and that is not decoration. The self-action rules make zero
    administrators unreachable one step at a time — A may demote B but never A — so the
    only route to zero is two administrators demoting each other at the same instant.
    The lock is the whole difference between this check and a comment — and it is raced for
    real in tests/test_lock_concurrency.py on Postgres, because it was undefended until
    GRPH-432 went looking.
    """
    if seat.role not in ADMIN_ROLES:
        return
    remaining = [m for m in _administrators(db, org_id, lock=True) if m.user_id != seat.user_id]
    if not remaining:
        raise HTTPException(
            409,
            "this is the organization's last owner or admin — promote somebody else "
            "first, or the organization would be left with nobody who can administer it",
        )


def set_member_role(
    db: Session, org_id: str, user_id: str, role: str, *, actor: User
) -> OrgMembership:
    """Change someone's org role. Commits.

    **Owner and admin are equivalent in power** (PRD-21 D8.1) — `authz.require_org_admin`
    is the only org-administration gate and has always accepted either. So the owner seat
    is demotable and removable like any other, and what protects the org is the floor
    invariant rather than one immortal seat.

    The refusals that remain are about the rank ladder, not about ownership:

    - **Nobody may promote themselves.** The ladder means nothing if it can be climbed
      from below.
    - **Nobody may grant a rank above their own**, for the same reason. This is also what
      keeps `owner` grantable only by an owner.
    - **Nobody may vacate the last administrative seat** — see `_refuse_if_last_administrator`.
    """
    if role not in ORG_ROLES:
        raise HTTPException(422, f"unknown role {role!r}; expected one of {', '.join(ORG_ROLES)}")
    seat = _seat(db, org_id, user_id)
    actor_role = authz.require_org_admin(db, actor.id, org_id)

    if role not in ADMIN_ROLES:
        _refuse_if_last_administrator(db, org_id, seat)
    if user_id == actor.id:
        raise HTTPException(409, "you cannot change your own role")
    if authz._ORG_RANK[role] > authz._ORG_RANK[actor_role]:
        raise HTTPException(403, f"an {actor_role} cannot grant the {role} role")

    seat.role = role
    db.commit()
    db.refresh(seat)
    return seat


def remove_member(db: Session, org_id: str, user_id: str, *, actor: User) -> dict:
    """Remove someone from the org, cascading their project memberships. Commits.

    Returns what was actually removed, so the caller can say what was lost rather than
    reporting a bare success. A removal that silently left project access behind would be
    the worst kind of quiet: the seat is gone from the roster and the access is not.
    """
    seat = _seat(db, org_id, user_id)
    authz.require_org_admin(db, actor.id, org_id)

    _refuse_if_last_administrator(db, org_id, seat)
    if user_id == actor.id:
        raise HTTPException(409, "you cannot remove yourself from the organization")

    project_ids = [p.id for p in db.scalars(select(Project).where(Project.org_id == org_id))]
    dropped = []
    if project_ids:
        for m in db.scalars(
            select(Membership).where(
                Membership.user_id == user_id, Membership.project_id.in_(project_ids)
            )
        ):
            dropped.append(m.project_id)
            db.delete(m)
    db.delete(seat)
    db.commit()
    return {"removed_role": seat.role, "projects_revoked": dropped}


def set_project_access(
    db: Session, project_id: str, user_id: str, access: str, *, actor: User
) -> Membership:
    """Grant or change one person's access to one project. Commits.

    `none` is stored, not deleted: an explicit "not this project" is a decision somebody
    made, and it should not read the same as never having been considered.
    """
    if access not in ACCESS_LEVELS:
        raise HTTPException(
            422, f"unknown access {access!r}; expected one of {', '.join(ACCESS_LEVELS)}"
        )
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if not project.org_id:
        raise HTTPException(
            400, "this project belongs to no organization; access is not administered here"
        )
    authz.require_org_admin(db, actor.id, project.org_id)
    # A seat in the org is what makes someone grantable at all — access to a project
    # inside an org they do not belong to would be an access path with no roster entry.
    _seat(db, project.org_id, user_id)

    membership = db.scalar(
        select(Membership).where(
            Membership.project_id == project_id, Membership.user_id == user_id
        )
    )
    # The drift D5 accepts, made visible rather than silent. A derived row is owned by the
    # grant that wrote it, so editing it here would be undone by the next recompute — and
    # an admin who thought they had changed something would be wrong without being told.
    if membership is not None and membership.origin == "team":
        teams = ", ".join(
            name for (name,) in db.execute(
                select(Team.name)
                .join(TeamMember, TeamMember.team_id == Team.id)
                .join(TeamGrant, TeamGrant.team_id == Team.id)
                .where(TeamMember.user_id == user_id, TeamGrant.project_id == project_id)
            )
        )
        raise HTTPException(
            409,
            "this access comes from a team grant and cannot be edited here — change it on "
            + (f"the team ({teams})" if teams else "the team that grants it"),
        )
    if membership is None:
        membership = Membership(project_id=project_id, user_id=user_id, role="member")
        db.add(membership)
    membership.access = access
    db.commit()
    db.refresh(membership)
    return membership


def create_org(db: Session, user: User, name: str) -> Organization:
    """Create an org and seat its creator as owner. Commits."""
    name = name.strip()
    if not name:
        raise HTTPException(422, "organization name is required")
    org = Organization(id="org_" + uuid.uuid4().hex[:10], name=name)
    db.add(org)
    db.flush()
    org.created_by = user.id  # durable; the seat below is not (D8.2)
    db.add(OrgMembership(org_id=org.id, user_id=user.id, role="owner"))
    db.commit()
    db.refresh(org)
    return org


def create_invite(db: Session, org: Organization, email: str, role: str, inviter: User) -> OrgInvite:
    """Create a pending invite and email the accept link. Commits.

    An owner can grant admin or member; ``owner`` is never invitable (ownership is a
    creation/transfer concern, not an invite one). A still-pending invite to the same
    email is reused rather than duplicated, so re-inviting just re-sends the link."""
    email = email.strip().lower()
    if not email:
        raise HTTPException(422, "invitee email is required")
    role = role if role in ("admin", "member") else "member"

    invite = db.scalar(
        select(OrgInvite).where(
            OrgInvite.org_id == org.id,
            OrgInvite.email == email,
            OrgInvite.status == "pending",
        )
    )
    if invite is None:
        # A fresh invite reserves a seat; re-inviting the same pending email doesn't,
        # so only gate the new-invite path against the plan's seat cap.
        quotas.enforce_seat_quota(db, org.id)
        invite = OrgInvite(
            id="inv_" + uuid.uuid4().hex[:12],
            org_id=org.id,
            email=email,
            invited_by=inviter.id,
        )
        db.add(invite)
    invite.role = role
    invite.token = secrets.token_urlsafe(32)
    invite.expires_at = utcnow() + timedelta(days=settings.invite_expiry_days)
    db.commit()
    db.refresh(invite)

    _send_invite_email(invite, org, inviter)
    return invite


def _send_invite_email(invite: OrgInvite, org: Organization, inviter: User) -> None:
    link = f"{settings.app_base_url.rstrip('/')}/invite/{invite.token}"
    subject = f"You're invited to join {org.name} on Graphban"
    who = inviter.name or inviter.handle or "A teammate"
    text = (
        f"{who} invited you to join the “{org.name}” organization on Graphban "
        f"as {invite.role}.\n\n"
        f"Accept your invitation:\n{link}\n\n"
        f"This link expires in {settings.invite_expiry_days} days. If you didn't expect "
        f"this, you can ignore this email."
    )
    email_svc.send_email(invite.email, subject, text)


def create_platform_invite(db: Session, email: str, plan: str | None, inviter: User) -> OrgInvite:
    """Operator-issued invite authorizing a BRAND-NEW account to sign up and found its
    own org (AL-91). Commits.

    Refused when an account already exists for the email: platform invites are for
    net-new customers, and an existing user wanting another org goes through the
    additional-org request flow instead. Optionally carries a ``plan`` to stamp on the
    org they found. Re-inviting a still-pending email refreshes rather than duplicates."""
    email = email.strip().lower()
    if not email:
        raise HTTPException(422, "invitee email is required")
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(
            409,
            "an account already exists for that email — platform invites are for new "
            "customers; an existing user needs an additional-org request instead",
        )

    invite = db.scalar(
        select(OrgInvite).where(
            OrgInvite.kind == "platform",
            OrgInvite.email == email,
            OrgInvite.status == "pending",
        )
    )
    if invite is None:
        invite = OrgInvite(
            id="inv_" + uuid.uuid4().hex[:12],
            kind="platform",
            org_id=None,
            email=email,
            invited_by=inviter.id,
        )
        db.add(invite)
    invite.plan = plan
    invite.token = secrets.token_urlsafe(32)
    invite.expires_at = utcnow() + timedelta(days=settings.invite_expiry_days)
    db.commit()
    db.refresh(invite)

    _send_platform_invite_email(invite, inviter)
    return invite


def _send_platform_invite_email(invite: OrgInvite, inviter: User) -> None:
    link = f"{settings.app_base_url.rstrip('/')}/invite/{invite.token}"
    subject = "You're invited to Graphban"
    who = inviter.name or inviter.handle or "The Graphban team"
    text = (
        f"{who} invited you to Graphban.\n\n"
        f"Create your account and set up your organization:\n{link}\n\n"
        f"This link expires in {settings.invite_expiry_days} days. If you didn't expect "
        f"this, you can ignore this email."
    )
    email_svc.send_email(invite.email, subject, text)


def platform_invites(db: Session, *, include_history: bool = False) -> list[OrgInvite]:
    """Platform invites, newest first.

    ``include_history`` widens the read from "still outstanding" to "every one ever
    issued", which is what the Licensing screen shows: a redeemed invite is the only
    record of which org came from which invite and at what plan it was founded, so
    dropping it after acceptance would erase the provenance the row exists to carry.
    """
    stmt = select(OrgInvite).where(OrgInvite.kind == "platform")
    if not include_history:
        stmt = stmt.where(OrgInvite.status == "pending")
    return list(db.scalars(stmt.order_by(OrgInvite.created_at.desc())))


def invite_is_expired(invite: OrgInvite) -> bool:
    """Past its expiry while still pending. Expiry is evaluated on read — there is no
    sweeper — so a pending row and an expired row are the same row seen at different
    times, and the UI must not report the first as available."""
    if invite.status != "pending" or invite.expires_at is None:
        return False
    # SQLite hands datetimes back tz-naive; coerce to UTC before comparing (as the
    # api-key expiry check does) so aware/naive never collide.
    exp = invite.expires_at if invite.expires_at.tzinfo else invite.expires_at.replace(tzinfo=timezone.utc)
    return exp < utcnow()


def org_founded_from(db: Session, invite: OrgInvite) -> Organization | None:
    """The org an accepted platform invite actually produced, or None.

    Resolved through the account that redeemed it: a platform invite authorizes signup,
    and the org is founded afterwards, so acceptance and founding are two facts. An
    invite accepted by someone who has not founded anything yet returns None rather
    than the nearest plausible org.

    ``accepted_user_id`` alone is the signal: it is set only on redemption and nothing
    clears it, so pairing it with a status check would add a branch no state can reach.
    """
    if not invite.accepted_user_id:
        return None
    org_id = db.scalar(
        select(OrgMembership.org_id)
        .join(Organization, Organization.id == OrgMembership.org_id)
        .where(
            OrgMembership.user_id == invite.accepted_user_id,
            OrgMembership.role == "owner",
        )
        .order_by(Organization.created_at)
    )
    return db.get(Organization, org_id) if org_id else None


def platform_plan_for(db: Session, user: User) -> str | None:
    """The plan preset from the platform invite this user signed up with, if any.

    Only meaningful while founding their FIRST org — the caller checks that — so no
    separate consumed flag is needed and the invite keeps its provenance."""
    invite = db.scalar(
        select(OrgInvite).where(
            OrgInvite.kind == "platform",
            OrgInvite.accepted_user_id == user.id,
            OrgInvite.status == "accepted",
        )
    )
    return invite.plan if invite else None


def invite_by_token(db: Session, token: str) -> OrgInvite | None:
    return db.scalar(select(OrgInvite).where(OrgInvite.token == token))


def _validate_pending(invite: OrgInvite | None) -> OrgInvite:
    """Shared gate for reading/accepting an invite: it must exist, be pending, and
    not be expired. 404 (not 403) so a bad/used token can't be told apart from a
    non-existent one."""
    if invite is None or invite.status != "pending":
        raise HTTPException(404, "invitation not found or already used")
    if invite_is_expired(invite):
        raise HTTPException(410, "this invitation has expired")
    return invite


def accept_invite(db: Session, token: str, user: User) -> Organization:
    """Join the invite's org as the accepting user. Commits.

    The invite is email-bound: the logged-in user's email must match the address it
    was sent to, so a forwarded link can't be redeemed by a different account. Joining
    is idempotent — a user who is already a member just marks the invite accepted."""
    invite = _validate_pending(invite_by_token(db, token))
    if invite.kind == "platform":
        # A platform invite is redeemed by REGISTERING with it (which founds a new org),
        # not by joining an existing one. An already-signed-in user can't consume it.
        raise HTTPException(
            400,
            "this is a platform invitation — redeem it by creating a new account; an "
            "existing account needs an additional-org request instead",
        )
    if invite.email.lower() != (user.email or "").lower():
        raise HTTPException(403, "this invitation was sent to a different email address")

    org = db.get(Organization, invite.org_id)
    if org is None:  # org deleted out from under the invite
        raise HTTPException(404, "invitation not found or already used")

    existing = db.scalar(
        select(OrgMembership).where(
            OrgMembership.org_id == invite.org_id, OrgMembership.user_id == user.id
        )
    )
    if existing is None:
        db.add(OrgMembership(org_id=invite.org_id, user_id=user.id, role=invite.role))
    invite.status = "accepted"
    invite.accepted_at = utcnow()
    invite.accepted_user_id = user.id
    db.commit()
    return org


def accept_platform_invite(db: Session, token: str, user: User) -> OrgInvite:
    """Mark a platform invite redeemed by the account that just registered (AL-91).

    There is no org to join — the invite authorized the *account*; the user then founds
    their own org, and :func:`platform_plan_for` applies any plan preset at that point."""
    invite = _validate_pending(invite_by_token(db, token))
    if invite.kind != "platform":
        raise HTTPException(400, "not a platform invitation")
    if invite.email.lower() != (user.email or "").lower():
        raise HTTPException(403, "this invitation was sent to a different email address")
    invite.status = "accepted"
    invite.accepted_at = utcnow()
    invite.accepted_user_id = user.id
    db.commit()
    return invite


def revoke_invite(db: Session, invite: OrgInvite) -> None:
    """Cancel a pending invite so its link stops working. Commits."""
    invite.status = "revoked"
    db.commit()


def pending_invites(db: Session, org_id: str) -> list[OrgInvite]:
    """Pending invites for one org — org-kind only, so the operator's platform
    invites never surface inside a tenant's member list."""
    return list(
        db.scalars(
            select(OrgInvite)
            .where(
                OrgInvite.kind == "org",
                OrgInvite.org_id == org_id,
                OrgInvite.status == "pending",
            )
            .order_by(OrgInvite.created_at.desc())
        )
    )
