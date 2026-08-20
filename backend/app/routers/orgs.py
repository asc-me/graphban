"""Organization REST — hosted-only (AL-74b).

Mounted in ``main.py`` ONLY when ``settings.hosted_mode`` is on, so self-host never
exposes an org surface. Everything here sits above the project layer: an org owner
creates the org, invites teammates by email, and manages seats; projects are then
created under the org (see ``routers/projects.create_project``) and the AL-74 authz
gate keeps them inside it.

The invite-accept and invite-preview routes address an invite by its unguessable
token: preview is intentionally unauthenticated (so the accept page can render "join
{org}" before the invitee logs in), while accept requires a logged-in user whose
email matches the invitation.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import (
    Event,
    Membership,
    OrgInvite,
    OrgMembership,
    Organization,
    Project,
    User,
)
from app.schemas import (
    BillingOut,
    InviteAcceptIn,
    InviteCreate,
    InviteOut,
    InvitePreviewOut,
    OrgCreate,
    OrgMemberOut,
    OrgProjectAccessOut,
    OrgOut,
    OrgRequestCreate,
    OrgRequestOut,
    PlanLimitsOut,
    SetPlanIn,
    UsageOut,
    UserOut,
)
from app.security import authz
from app.security.deps import get_current_user
from app.services import events as events_svc
from app.services import galaxy as galaxy_svc
from app.services import orgs as orgs_svc
from app.services import quotas


def require_hosted() -> None:
    """Gate the whole org surface behind HOSTED_MODE. With it off (self-host), every
    org/invite route 404s — the feature is effectively absent, matching the "orgs are
    SaaS-only" constraint — while a hosted deploy gets the full router."""
    if not settings.hosted_mode:
        raise HTTPException(404, "Not Found")


router = APIRouter(tags=["orgs"], dependencies=[Depends(require_hosted)])


def _invite_out(invite: OrgInvite) -> InviteOut:
    out = InviteOut.model_validate(invite)
    out.accept_url = f"{settings.app_base_url.rstrip('/')}/invite/{invite.token}"
    return out


@router.get("/orgs", response_model=list[OrgOut])
def list_orgs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The orgs the caller holds a seat in, each with their role."""
    seats = db.query(OrgMembership).filter(OrgMembership.user_id == user.id).all()
    out = []
    for seat in seats:
        org = db.get(Organization, seat.org_id)
        if org is not None:
            out.append(OrgOut(id=org.id, name=org.name, plan=org.plan, role=seat.role))
    return out


@router.post("/orgs", response_model=OrgOut, status_code=201)
def create_org(
    body: OrgCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Every account gets one org; founding another needs a standing plan entitlement or
    # an approved one-time request, which is spent only after the org actually lands (AL-92).
    grant = orgs_svc.require_may_found_org(db, user)
    first_org = not authz.org_ids_for_user(db, user.id)
    org = orgs_svc.create_org(db, user, body.name)
    if grant is not None:
        orgs_svc.consume_org_grant(db, grant)
    # A platform invite may have pre-assigned a plan (e.g. a design partner seeded onto
    # `team`). Apply it only to the FIRST org they found, which is what the invite
    # authorized — no consumed-flag needed, and the invite keeps its provenance (AL-91).
    if first_org:
        preset = orgs_svc.platform_plan_for(db, user)
        if preset in quotas.PLANS:
            org.plan = preset
            db.commit()
            db.refresh(org)
    events_svc.record_user(db, user, action="create_org", target_type="org",
                           target_id=org.id, meta={"name": org.name, "plan": org.plan})
    return OrgOut(id=org.id, name=org.name, plan=org.plan, role="owner")


@router.post("/orgs/requests", response_model=OrgRequestOut, status_code=201)
def request_additional_org(
    body: OrgRequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Ask an operator for permission to found an additional organization (AL-92).
    Refused as unnecessary if the caller can already create one."""
    req = orgs_svc.submit_org_request(db, user, body.reason, body.company)
    events_svc.record_user(db, user, action="request_additional_org", target_type="org_request",
                           target_id=req.id, meta={"company": req.company})
    return req


@router.get("/orgs/requests/mine", response_model=OrgRequestOut | None)
def my_org_request(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The caller's most recent additional-org request, so the UI can show its status."""
    return orgs_svc.latest_org_request(db, user)


@router.get("/orgs/{org_id}/billing", response_model=BillingOut)
def org_billing(org_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The org's plan, its limits, and current usage — visible to any member so the
    team can see how close they are to each cap."""
    authz.require_org_member(db, user.id, org_id)
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "organization not found")
    plan = quotas.plan_of(org)
    return BillingOut(
        plan=org.plan,
        limits=PlanLimitsOut(
            max_projects=plan.max_projects,
            max_seats=plan.max_seats,
            max_shards=plan.max_shards,
            max_calls_per_month=plan.max_calls_per_month,
        ),
        usage=UsageOut(**quotas.usage(db, org_id)),
    )


@router.put("/orgs/{org_id}/plan", response_model=OrgOut)
def set_org_plan(
    org_id: str,
    body: SetPlanIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Assign an org's plan. Private-beta billing is MANUAL (AL-75): only a platform
    operator (config allowlist) may do this — an org owner can't upgrade themselves
    for free. 404 (not 403) for non-admins so the endpoint's existence stays hidden."""
    if not quotas.is_platform_admin(user):
        raise HTTPException(404, "Not Found")
    if body.plan not in quotas.PLANS:
        raise HTTPException(422, f"unknown plan {body.plan!r}; expected one of {', '.join(quotas.PLANS)}")
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "organization not found")
    org.plan = body.plan
    db.commit()
    events_svc.record_user(db, user, action="set_org_plan", target_type="org",
                           target_id=org_id, meta={"plan": body.plan})
    role = authz.org_role(db, user.id, org_id) or "admin"
    return OrgOut(id=org.id, name=org.name, plan=org.plan, role=role)


@router.get("/orgs/{org_id}/galaxy")
def org_galaxy(org_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The org's repo-level dependency graph (PRD-21 D3).

    Every edge carries the evidence that proves it — the file and the fact found there —
    because that is the whole difference between this graph and a guess. Stale edges are
    included and flagged rather than filtered, so "this dependency went away" and "there
    was never a dependency" do not arrive looking the same.
    """
    authz.require_org_member(db, user.id, org_id)
    return galaxy_svc.galaxy(db, org_id)


@router.get("/orgs/{org_id}/members", response_model=list[OrgMemberOut])
def list_members(org_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_org_member(db, user.id, org_id)
    seats = db.query(OrgMembership).filter(OrgMembership.org_id == org_id).all()
    org_projects = {
        p.id: p for p in db.scalars(select(Project).where(Project.org_id == org_id))
    }
    out = []
    for seat in seats:
        member = db.get(User, seat.user_id)
        if member is None:
            continue
        # Project access is per-membership and lives on `Membership.access`. `none` is
        # an access level meaning "explicitly not this one", so it is filtered out here
        # rather than rendered as a grant.
        access = [
            OrgProjectAccessOut(
                project_id=m.project_id,
                tag=org_projects[m.project_id].tag,
                name=org_projects[m.project_id].name,
                level=m.access,
            )
            for m in db.scalars(select(Membership).where(Membership.user_id == member.id))
            if m.project_id in org_projects and m.access in ("write", "read")
        ]
        last_write = db.scalar(
            select(func.max(Event.ts)).where(
                Event.actor_type == "user", Event.actor_id == member.id
            )
        )
        out.append(OrgMemberOut(
            user=UserOut.model_validate(member), role=seat.role,
            access=access, last_write_at=last_write,
        ))
    return out


@router.get("/orgs/{org_id}/invites", response_model=list[InviteOut])
def list_invites(org_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_org_admin(db, user.id, org_id)
    return [_invite_out(i) for i in orgs_svc.pending_invites(db, org_id)]


@router.post("/orgs/{org_id}/invites", response_model=InviteOut, status_code=201)
def create_invite(
    org_id: str,
    body: InviteCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_org_admin(db, user.id, org_id)
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "organization not found")
    invite = orgs_svc.create_invite(db, org, body.email, body.role, user)
    events_svc.record_user(db, user, action="create_org_invite", target_type="org_invite",
                           target_id=invite.id, meta={"org_id": org_id, "email": invite.email,
                                                      "role": invite.role})
    return _invite_out(invite)


@router.delete("/orgs/{org_id}/invites/{invite_id}", status_code=204)
def revoke_invite(
    org_id: str,
    invite_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    authz.require_org_admin(db, user.id, org_id)
    invite = db.get(OrgInvite, invite_id)
    if invite is None or invite.org_id != org_id or invite.status != "pending":
        raise HTTPException(404, "invitation not found")
    orgs_svc.revoke_invite(db, invite)
    events_svc.record_user(db, user, action="revoke_org_invite", target_type="org_invite",
                           target_id=invite.id, meta={"org_id": org_id})


@router.get("/invites/{token}/preview", response_model=InvitePreviewOut)
def preview_invite(token: str, db: Session = Depends(get_db)):
    """Unauthenticated: what org/email a token invites. Used by the accept page before
    the invitee has logged in. A used/expired/unknown token 404s identically."""
    invite = orgs_svc._validate_pending(orgs_svc.invite_by_token(db, token))
    inviter = db.get(User, invite.invited_by)
    invited_by = (inviter.name or inviter.handle) if inviter else ""
    if invite.kind == "platform":
        # No org yet — the accept page renders the "create your account, then found
        # your organization" flow instead of a join-this-org prompt (AL-91).
        return InvitePreviewOut(
            kind="platform", org_name="", email=invite.email,
            role=invite.role, invited_by=invited_by,
        )
    org = db.get(Organization, invite.org_id)
    if org is None:
        raise HTTPException(404, "invitation not found or already used")
    return InvitePreviewOut(
        kind="org",
        org_name=org.name,
        email=invite.email,
        role=invite.role,
        invited_by=invited_by,
    )


@router.post("/invites/accept", response_model=OrgOut)
def accept_invite(
    body: InviteAcceptIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    org = orgs_svc.accept_invite(db, body.token, user)
    role = authz.org_role(db, user.id, org.id) or "member"
    events_svc.record_user(db, user, action="accept_org_invite", target_type="org",
                           target_id=org.id, meta={"role": role})
    return OrgOut(id=org.id, name=org.name, plan=org.plan, role=role)
