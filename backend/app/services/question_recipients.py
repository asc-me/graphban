"""Who is alerted about a question on an item (P75 D2, access rule from D6).

Order: the PRD owner, else the project owners. Only users with read access to the project
are ever returned. Pure: it reads, never writes, and never raises for an owner it cannot
resolve.
"""
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Membership, OrgMembership, Prd, Project, User
from app.security import authz


def question_recipients(db: Session, item) -> dict:
    project_id = getattr(item, "project_id", None)
    prd_id = getattr(item, "prd_id", None)
    prd = db.get(Prd, prd_id) if prd_id else None
    owner_id = prd.created_by if prd is not None else None
    if owner_id and db.get(User, owner_id) is not None and authz.can_read(db, owner_id, project_id):
        return {"users": [owner_id], "reason": "prd owner"}
    owners = []
    for m in db.query(Membership).filter(
        Membership.project_id == project_id, Membership.role == "owner"
    ).order_by(Membership.id):
        if m.user_id not in owners and db.get(User, m.user_id) is not None \
                and authz.can_read(db, m.user_id, project_id):
            owners.append(m.user_id)
    if owners:
        return {"users": owners, "reason": "project owners (prd has no owner with access)"}
    return {"users": [], "reason": "nobody with access could be found"}


def link_recipients(db: Session, prd: Prd | None, project_id: str) -> dict:
    """Who is alerted about an undecided PRD-link thing on the RECEIVING PRD (GRPH-P104 S5):
    the P75 rule for that PRD's project, and when it finds nobody, the org's owners."""
    found = question_recipients(db, SimpleNamespace(project_id=project_id,
                                                    prd_id=prd.id if prd is not None else None))
    if found["users"]:
        return found
    org_id = db.scalar(select(Project.org_id).where(Project.id == project_id))
    owners = []
    if org_id:
        for m in db.query(OrgMembership).filter(
                OrgMembership.org_id == org_id, OrgMembership.role == "owner").order_by(OrgMembership.id):
            if m.user_id not in owners and db.get(User, m.user_id) is not None:
                owners.append(m.user_id)
    return {"users": owners,
            "reason": "org owners (no owner of the PRD or project has access)" if owners
            else found["reason"]}
