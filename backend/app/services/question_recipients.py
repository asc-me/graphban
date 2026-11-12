"""Who is alerted about a question on an item (P75 D2, access rule from D6).

Order: the PRD owner, else the project owners. Only users with read access to the project
are ever returned. Pure: it reads, never writes, and never raises for an owner it cannot
resolve.
"""
from sqlalchemy.orm import Session

from app.models import Membership, Prd, User
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
