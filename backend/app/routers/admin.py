"""Platform (operator) plane — hosted-only, platform-admin-only (AL-91).

This is the one deliberate cross-tenant surface in the product. Two hard rules keep it
from becoming the isolation hole that Phase 6 (AL-76) exists to prevent:

1. **Gated twice** — HOSTED_MODE must be on AND the caller must be a platform operator
   (``PLATFORM_ADMIN_EMAILS``). Failure is a 404, not a 403, so the surface's very
   existence stays hidden from tenants.
2. **Metadata only** — nothing here returns tenant *content* (items, memory shards,
   PRDs, requests, code graph). Operators see orgs, plans, usage, and invites; never
   what a customer wrote.

Every mutation is recorded to the event ledger, attributed to the acting operator.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from sqlalchemy import func, select

from app.models import Event, OrgInvite, OrgMembership, OrgRequest, Organization, User
from app.schemas import (
    AdminActivityOut,
    AdminInviteOut,
    AdminOrgMemberOut,
    AdminOrgOut,
    AdminUserOrgOut,
    AdminUserOut,
    AdminWhoamiOut,
    OrgRequestDecision,
    OrgRequestOut,
    PlanLimitsOut,
    PlatformInviteCreate,
    UsageOut,
)
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import orgs as orgs_svc
from app.services import quotas


def require_platform_admin(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> User:
    """Hosted + operator-allowlist gate. 404 (not 403) hides the plane from tenants."""
    if not settings.hosted_mode or not quotas.is_platform_admin(user):
        raise HTTPException(404, "Not Found")
    return user


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])


def _invite_out(db: Session, invite: OrgInvite) -> AdminInviteOut:
    """An invite plus the provenance the Licensing screen is built on: who issued it,
    whether it has aged out, and which org it actually produced."""
    out = AdminInviteOut.model_validate(invite)
    out.accept_url = f"{settings.app_base_url.rstrip('/')}/invite/{invite.token}"
    out.expired = orgs_svc.invite_is_expired(invite)
    inviter = db.get(User, invite.invited_by) if invite.invited_by else None
    out.invited_by_handle = (inviter.handle or inviter.name) if inviter else ""
    org = orgs_svc.org_founded_from(db, invite)
    if org is not None:
        out.redeemed_org_id, out.redeemed_org_name = org.id, org.name
    return out


@router.get("/forwarded-chain")
def forwarded_chain(request: Request):
    """What this instance actually received from the proxies in front of it (GRPH-517).

    `security.net.client_ip` picks the caller's rate-limit bucket out of the
    `X-Forwarded-For` chain, and which POSITION is the real caller depends on how many
    proxies append to that header. On this deployment there are two — Railway's edge, then
    nginx — and Railway does not document whether its edge overwrites the header or appends
    to it. That single fact decides whether taking `[0]` reads the true client or a value
    the caller wrote, and it cannot be settled by reading code on either side.

    It also cannot responsibly be settled by experiment against the rate limiter, which is
    the only other observable: that means sending forged-header traffic until a bucket
    breaks, which is DoS-shaped whatever the intent. So the missing thing was never
    analysis, it was **a way to look**. This is the way to look.

    Send one authenticated request from a real client and read `xff_hops`. Its length and
    the position of your own address give the hop count that `client_ip` should be counting
    from the right.

    Also answers GRPH-477 in the same response: `scheme` is what the app believes it is
    serving and `header_x_forwarded_proto` is what nginx said. If they disagree, uvicorn is
    discarding the forwarded scheme — `--forwarded-allow-ips` does not cover the peer — and
    any URL built from the request would carry `http` on an HTTPS-only origin.

    Reflects only the caller's OWN request headers, so it discloses nothing about anyone
    else. Platform-admin gated like every route on this router, which 404s for everyone
    else so the console's existence is never disclosed.
    """
    from app.security.net import client_ip

    raw = request.headers.get("x-forwarded-for")
    hops = [h.strip() for h in raw.split(",")] if raw else []
    resolved = client_ip(request)

    return {
        "resolved_client_ip": resolved,
        # Which entry `client_ip` returned. `from_left` 0 is the position a caller can
        # write; the useful number is `from_right`, because that is what a hop count has
        # to be expressed in to survive a chain of a different length.
        "resolved_position": (
            {"from_left": hops.index(resolved), "from_right": len(hops) - hops.index(resolved)}
            if resolved in hops else None
        ),
        "xff_raw": raw,
        "xff_hops": hops,
        "xff_hop_count": len(hops),
        # The socket peer. Behind a proxy this is the proxy, never the caller — it is here
        # as the fail-closed fallback `client_ip` uses when nothing is asserted.
        "socket_peer": request.client.host if request.client else None,
        "header_x_real_ip": request.headers.get("x-real-ip"),
        "header_x_forwarded_proto": request.headers.get("x-forwarded-proto"),
        "header_x_forwarded_host": request.headers.get("x-forwarded-host"),
        "header_host": request.headers.get("host"),
        "scheme": request.url.scheme,
        "scheme_matches_forwarded": (
            request.headers.get("x-forwarded-proto") is None
            or request.url.scheme == request.headers.get("x-forwarded-proto")
        ),
        "trusted_proxy": settings.trusted_proxy,
    }


@router.get("/me", response_model=AdminWhoamiOut)
def admin_whoami(admin: User = Depends(require_platform_admin)):
    """Cheap probe the SPA uses to decide whether to render the operator nav at all.
    Non-admins get the same 404 as every other route here, so the console's existence
    is never disclosed to a tenant.

    Carries the two pieces of deployment policy the console has to state rather than
    imply: how a stranger may sign up, and how long an issued invite stays good."""
    return AdminWhoamiOut(
        email=admin.email,
        signup_mode=settings.signup_mode,
        invite_expiry_days=settings.invite_expiry_days,
    )


@router.get("/orgs", response_model=list[AdminOrgOut])
def list_orgs(db: Session = Depends(get_db)):
    """Every tenant, with plan + usage against its limits. Metadata only — no tenant
    content. Usage is computed per-org (an N+1 that's fine at beta scale; batch later)."""
    out: list[AdminOrgOut] = []
    for org in db.scalars(select(Organization).order_by(Organization.created_at)):
        members = [
            AdminOrgMemberOut(
                handle=user.handle, name=user.name, role=membership.role,
                joined_at=membership.created_at,
            )
            for membership, user in db.execute(
                select(OrgMembership, User)
                .join(User, User.id == OrgMembership.user_id)
                .where(OrgMembership.org_id == org.id)
                .order_by(OrgMembership.created_at)
            )
        ]
        # Owner first, then admins, then the rest — the console reads the head of this
        # list as "who to contact", so the order is meaning, not presentation.
        rank = {"owner": 0, "admin": 1}
        members.sort(key=lambda m: rank.get(m.role, 2))
        # The founder, not "whoever holds the owner seat" — that seat is demotable now
        # (PRD-21 D8.2), and an org whose founder was demoted must still say who made it.
        # Falls back to the seat for orgs created before `created_by` existed.
        owner_id = org.created_by or db.scalar(
            select(OrgMembership.user_id).where(
                OrgMembership.org_id == org.id, OrgMembership.role == "owner"
            )
        )
        owner = db.get(User, owner_id) if owner_id else None
        plan = quotas.plan_of(org)
        out.append(AdminOrgOut(
            id=org.id, name=org.name, plan=org.plan, created_at=org.created_at,
            owner_email=owner.email if owner else None,
            owner_name=(owner.name or owner.handle) if owner else "",
            owner_handle=owner.handle if owner else "",
            usage=UsageOut(**quotas.usage(db, org.id)),
            limits=PlanLimitsOut(
                max_projects=plan.max_projects, max_seats=plan.max_seats,
                max_shards=plan.max_shards, max_calls_per_month=plan.max_calls_per_month,
            ),
            members=members,
        ))
    return out


@router.get("/users", response_model=list[AdminUserOut])
def list_users(db: Session = Depends(get_db)):
    """Read-only identity lookup for support. Identity + org count only — never a
    user's data."""
    out: list[AdminUserOut] = []
    for user in db.scalars(select(User).order_by(User.created_at)):
        orgs = [
            AdminUserOrgOut(id=org.id, name=org.name, role=membership.role)
            for membership, org in db.execute(
                select(OrgMembership, Organization)
                .join(Organization, Organization.id == OrgMembership.org_id)
                .where(OrgMembership.user_id == user.id)
                .order_by(OrgMembership.created_at)
            )
        ]
        # Newest event this account is the actor of. A user with no row has written
        # nothing we recorded — which is NOT the same as being idle, since reads are
        # never evented, so it stays NULL and the console names it separately.
        last_write = db.scalar(
            select(func.max(Event.ts)).where(
                Event.actor_type == "user", Event.actor_id == user.id
            )
        )
        out.append(AdminUserOut(
            id=user.id, name=user.name, handle=user.handle, email=user.email,
            created_at=user.created_at, org_count=len(orgs), orgs=orgs,
            last_write_at=last_write,
        ))
    return out


@router.get("/invites", response_model=list[AdminInviteOut])
def list_platform_invites(history: bool = False, db: Session = Depends(get_db)):
    """Platform invites — the beta's onboarding links.

    Defaults to the outstanding ones. ``history=true`` widens it to every invite ever
    issued, because a redeemed row is the only record of which org came from which
    invite: dropping it once accepted would make "never invited" and "invited, and it
    worked" render identically.
    """
    return [_invite_out(db, i) for i in orgs_svc.platform_invites(db, include_history=history)]


@router.get("/activity", response_model=list[AdminActivityOut])
def list_platform_activity(limit: int = 12, db: Session = Depends(get_db)):
    """The operator ledger: actions taken FROM this plane, newest first.

    Deliberately NOT a platform activity feed. Tenant events are project-scoped and
    stay inside their tenant, so an empty list here means no operator has done
    anything — never that the platform is idle. The console says which of the two it
    is showing, in those words."""
    return [
        AdminActivityOut(
            ts=e.ts, action=e.action, actor_label=e.actor_label,
            target_type=e.target_type, target_id=e.target_id, meta=e.meta,
        )
        for e in events_svc.platform_ledger(db, limit=max(1, min(limit, 100)))
    ]


@router.post("/invites", response_model=AdminInviteOut, status_code=201)
def create_platform_invite(
    body: PlatformInviteCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_platform_admin),
):
    """Invite a NEW customer: they sign up and found their own org. Refused if an
    account already exists for the email (that's an additional-org request instead)."""
    if body.plan is not None and body.plan not in quotas.PLANS:
        raise HTTPException(422, f"unknown plan {body.plan!r}; expected one of {', '.join(quotas.PLANS)}")
    invite = orgs_svc.create_platform_invite(db, body.email, body.plan, admin)
    events_svc.record_user(db, admin, action="create_platform_invite", target_type="org_invite",
                           target_id=invite.id, meta={"email": invite.email, "plan": invite.plan})
    return _invite_out(db, invite)


@router.delete("/invites/{invite_id}", status_code=204)
def revoke_platform_invite(
    invite_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_platform_admin),
):
    invite = db.get(OrgInvite, invite_id)
    if invite is None or invite.kind != "platform" or invite.status != "pending":
        raise HTTPException(404, "invitation not found")
    orgs_svc.revoke_invite(db, invite)
    events_svc.record_user(db, admin, action="revoke_platform_invite", target_type="org_invite",
                           target_id=invite.id, meta={"email": invite.email})


@router.get("/org-requests", response_model=list[OrgRequestOut])
def list_org_requests(db: Session = Depends(get_db)):
    """Pending requests to found an additional organization (AL-92)."""
    return orgs_svc.pending_org_requests(db)


@router.post("/org-requests/{request_id}", response_model=OrgRequestOut)
def decide_org_request(
    request_id: str,
    body: OrgRequestDecision,
    db: Session = Depends(get_db),
    admin: User = Depends(require_platform_admin),
):
    """Approve or deny. An approval grants exactly ONE additional org — it's consumed
    when spent, so it can't be replayed. Standing multi-org access comes from an
    enterprise plan instead."""
    req = db.get(OrgRequest, request_id)
    if req is None or req.status != "pending":
        raise HTTPException(404, "request not found")
    req = orgs_svc.decide_org_request(db, req, admin, body.approve, body.note)
    events_svc.record_user(db, admin, action="decide_org_request", target_type="org_request",
                           target_id=req.id, meta={"status": req.status, "user_id": req.user_id})
    return req
