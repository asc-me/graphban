"""`GET /api/playbooks` — the skills, loops and playbooks this deployment publishes (GRPH-1491)."""
from fastapi import APIRouter, Depends, HTTPException, status

from app.models import ApiKey, User
from app.security.deps import get_user_or_agent_key
from app.services import playbooks as svc

router = APIRouter(prefix="/playbooks", tags=["playbooks"])


@router.get("")
def get_pack(_who: User | ApiKey = Depends(get_user_or_agent_key)):
    try:
        return svc.manifest()
    except svc.PackUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
