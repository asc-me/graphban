"""Instance-level guided-setup reads (GRPH-1365). Project-level setup lives in routers/projects.py."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import Refused
from app.models import User
from app.security.deps import get_current_user
from app.services import setup as setup_svc

router = APIRouter(prefix="/setup", tags=["setup"])

POSTURE_RULE = "setup.posture_needs_instance_admin"

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@router.get("/instance")
def instance_posture(request: Request, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Is this box safe to expose? Booleans only: a secret's value never leaves the server, and
    a non-admin learns nothing about how the deployment is configured. Warnings, not blockers."""
    if not setup_svc.is_instance_admin(db, user):
        raise Refused(403, "requires an instance admin", rule_id=POSTURE_RULE,
                      hint="ask the box's operator, or an owner of one of its projects",
                      target=("instance", "setup_posture"))
    host = request.client.host if request.client else ""
    return setup_svc.instance_posture(db, loopback=host in _LOOPBACK)
