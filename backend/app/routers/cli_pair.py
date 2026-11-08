"""`/api/cli/pair/*` — device-code sign-in for `gban login --pair` (GRPH-1366)."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User
from app.security.deps import get_current_user
from app.security.jwt import create_access_token, create_refresh_token
from app.security.net import client_ip
from app.services import cli_pair as svc
from app.services import ratelimit

router = APIRouter(prefix="/cli/pair", tags=["cli"])


class ApproveIn(BaseModel):
    user_code: str


class RedeemIn(BaseModel):
    device_code: str


def _guard(request: Request, scope: str) -> None:
    limit = settings.login_rate_per_min * 3
    if not ratelimit.allow(f"clipair:{scope}:{client_ip(request)}", limit):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts; try again shortly")


@router.post("", status_code=201)
def start(request: Request, db: Session = Depends(get_db)):
    _guard(request, "start")
    return svc.start(db)


@router.post("/approve", status_code=204)
def approve(body: ApproveIn, request: Request, user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    _guard(request, "approve")
    try:
        svc.approve(db, user_code=body.user_code, user=user)
    except svc.InvalidPair:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that code is not valid, or has expired")


@router.post("/token")
def token(body: RedeemIn, request: Request, db: Session = Depends(get_db)):
    _guard(request, "token")
    try:
        user = svc.redeem(db, device_code=body.device_code)
    except svc.InvalidPair:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this sign-in code is invalid or has expired")
    if user is None:
        return {"status": "pending"}
    return {"status": "approved",
            "access_token": create_access_token(user.id, user.token_version),
            "refresh_token": create_refresh_token(user.id, user.token_version),
            "email": user.email}
