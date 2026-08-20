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
    Team,
    TeamGrant,
    TeamMember,
    User,
)
from app.schemas import (
    BillingOut,
    InviteAcceptIn,
    InviteCreate,
    InviteOut,
    InvitePreviewOut,
    OrgCreate,
    GrantRevokedOut,
    MemberRemovedOut,
    MemberRoleIn,
    OrgMemberOut,
    OrgProjectAccessOut,
    ProjectAccessIn,
    TeamCreate,
    TeamGrantIn,
    TeamGrantOut,
    TeamOut,
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
from app.services import teams as teams_svc
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


@router.get("/orgs/{org_id}/deployments")
def org_deployments(org_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Which local boxes push into this tenant (PRD-21 D6).

    Everything here is cloud-held. A linked box forwards its claims, leases and heartbeats,
    so who is working on what is a query — nothing is framed, proxied, relayed or tunnelled,
    and the cloud never reaches into a deployment.

    The address a box reports is a **hint**: the same machine answers differently from
    different networks, and this endpoint makes no attempt to verify it.
    """
    authz.require_org_member(db, user.id, org_id)
    return galaxy_svc.deployments(db, org_id)


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
# ---- teams (PRD-21 D5) ---------------------------------------------------------
def _team_out(db: Session, team) -> TeamOut:
    members = [
        UserOut.model_validate(db.get(User, uid))
        for (uid,) in db.execute(
            select(TeamMember.user_id).where(TeamMember.team_id == team.id)
        )
        if db.get(User, uid) is not None
    ]
    grants = []
    for g in db.scalars(select(TeamGrant).where(TeamGrant.team_id == team.id)):
        project = db.get(Project, g.project_id)
        reach = teams_svc.materialized_by(db, team.id, g.project_id)
        grants.append(TeamGrantOut(
            project_id=g.project_id,
            tag=project.tag if project else "",
            name=project.name if project else "",
            access=g.access,
            derived_user_ids=reach["derived"],
            direct_user_ids=reach["direct"],
        ))
    return TeamOut(id=team.id, org_id=team.org_id, name=team.name,
                   description=team.description, members=members, grants=grants)


def _require_team_admin(db: Session, team_id: str, user: User):
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "team not found")
    authz.require_org_admin(db, user.id, team.org_id)
    return team


@router.get("/orgs/{org_id}/teams", response_model=list[TeamOut])
def list_teams(org_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authz.require_org_member(db, user.id, org_id)
    return [
        _team_out(db, t)
        for t in db.scalars(select(Team).where(Team.org_id == org_id).order_by(Team.name))
    ]


@router.post("/orgs/{org_id}/teams", response_model=TeamOut, status_code=201)
def create_team(org_id: str, body: TeamCreate, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    authz.require_org_admin(db, user.id, org_id)
    team = teams_svc.create_team(db, org_id, body.name, body.description)
    events_svc.record_user(db, user, action="create_team", target_type="team",
                           target_id=team.id, meta={"name": team.name})
    return _team_out(db, team)


@router.delete("/teams/{team_id}")
def delete_team(team_id: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """Disband a team and recompute. Access held directly, or through another team,
    survives — only what this team alone provided goes away."""
    _require_team_admin(db, team_id, user)
    result = teams_svc.delete_team(db, team_id)
    events_svc.record_user(db, user, action="delete_team", target_type="team",
                           target_id=team_id, meta=result)
    return result


@router.post("/teams/{team_id}/members/{user_id}", response_model=TeamOut, status_code=201)
def add_team_member(team_id: str, user_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    team = _require_team_admin(db, team_id, user)
    authz.require_org_member(db, user_id, team.org_id)  # a seat in the org comes first
    teams_svc.add_member(db, team_id, user_id)
    events_svc.record_user(db, user, action="add_team_member", target_type="team",
                           target_id=team_id, meta={"user_id": user_id})
    return _team_out(db, db.get(Team, team_id))


@router.delete("/teams/{team_id}/members/{user_id}", response_model=TeamOut)
def remove_team_member(team_id: str, user_id: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    _require_team_admin(db, team_id, user)
    teams_svc.remove_member(db, team_id, user_id)
    events_svc.record_user(db, user, action="remove_team_member", target_type="team",
                           target_id=team_id, meta={"user_id": user_id})
    return _team_out(db, db.get(Team, team_id))


@router.put("/teams/{team_id}/grants/{project_id}", response_model=TeamOut)
def set_team_grant(team_id: str, project_id: str, body: TeamGrantIn,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Grant or change a team's access to a project.

    The grant and every membership it materializes are one transaction: a grant that
    landed while its memberships did not would be a promise authorization never heard."""
    _require_team_admin(db, team_id, user)
    teams_svc.set_grant(db, team_id, project_id, body.access)
    events_svc.record_user(db, user, action="set_team_grant", target_type="team",
                           target_id=team_id, project_id=project_id,
                           meta={"access": body.access})
    return _team_out(db, db.get(Team, team_id))


@router.delete("/teams/{team_id}/grants/{project_id}", response_model=GrantRevokedOut)
def revoke_team_grant(team_id: str, project_id: str, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Revoke, and report what survived — access someone also holds directly or through
    another team is recomputed, not deleted."""
    _require_team_admin(db, team_id, user)
    result = teams_svc.revoke_grant(db, team_id, project_id)
    events_svc.record_user(db, user, action="revoke_team_grant", target_type="team",
                           target_id=team_id, project_id=project_id, meta=result)
    return GrantRevokedOut(**result)


# ---- membership mutations (PRD-21 D8) -----------------------------------------
# Authority actions, so every one lands in the ledger. `test_authority_gates.py` exists to
# assert that authority stays human-adjudicated and audited, and these are exactly that.
@router.patch("/orgs/{org_id}/members/{user_id}", response_model=OrgMemberOut)
def set_member_role(
    org_id: str,
    user_id: str,
    body: MemberRoleIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change a member's org role. The owner is immutable and nobody promotes themselves."""
    seat = orgs_svc.set_member_role(db, org_id, user_id, body.role, actor=user)
    events_svc.record_user(db, user, action="set_member_role", target_type="org_membership",
                           target_id=f"{org_id}:{user_id}", meta={"role": seat.role})
    member = db.get(User, user_id)
    return OrgMemberOut(user=UserOut.model_validate(member), role=seat.role)


@router.delete("/orgs/{org_id}/members/{user_id}", response_model=MemberRemovedOut)
def remove_member(
    org_id: str,
    user_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Remove a member, cascading their project memberships.

    Returns what was revoked. A removal that left project access behind would be the worst
    kind of quiet — gone from the roster, still able to reach the work.
    """
    result = orgs_svc.remove_member(db, org_id, user_id, actor=user)
    events_svc.record_user(db, user, action="remove_member", target_type="org_membership",
                           target_id=f"{org_id}:{user_id}", meta=result)
    return MemberRemovedOut(**result)


@router.put("/projects/{project_id}/members/{user_id}", response_model=OrgProjectAccessOut)
def set_project_access(
    project_id: str,
    user_id: str,
    body: ProjectAccessIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Grant or change one person's access to one project (`write` / `read` / `none`)."""
    membership = orgs_svc.set_project_access(db, project_id, user_id, body.access, actor=user)
    project = db.get(Project, project_id)
    events_svc.record_user(db, user, action="set_project_access", target_type="membership",
                           target_id=f"{project_id}:{user_id}", project_id=project_id,
                           meta={"access": membership.access})
    return OrgProjectAccessOut(
        project_id=project_id, tag=project.tag, name=project.name, level=membership.access,
    )


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
