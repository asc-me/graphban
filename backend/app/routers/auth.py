import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Membership, Project, User
from app.schemas import (LoginIn, PasswordChangeIn, RefreshIn, RegisterIn, TokenOut,
                         UserOut)
from app.security.deps import get_current_user
from app.security.jwt import create_access_token, create_refresh_token, decode_token
from app.security.net import client_ip
from app.security.passwords import hash_password, verify_password
from app.services import orgs as orgs_svc
from app.services import ratelimit

router = APIRouter(prefix="/auth", tags=["auth"])


def _guard_login_rate(request: Request, email: str) -> None:
    """Blunt credential stuffing / brute force (AL-72): cap attempts per-email and,
    more loosely, per source IP. Counts every attempt, so a wrong-password flood
    trips the limit and returns 429 instead of letting guessing run unbounded."""
    per_email = settings.login_rate_per_min
    per_ip = per_email * 3  # an IP may legitimately host several accounts
    ip = client_ip(request)
    if not ratelimit.allow(f"login:email:{email.lower()}", per_email) or not ratelimit.allow(
        f"login:ip:{ip}", per_ip
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts; try again shortly")


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    _guard_login_rate(request, body.email)
    user = db.scalar(select(User).where(User.email == body.email))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return TokenOut(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=create_refresh_token(user.id, user.token_version),
    )


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    # A valid org invite is its own authorization to sign up: it lets a user through
    # even when open self-serve registration is closed (invite-only hosted beta),
    # because someone already vouched for this specific email (AL-74b). We validate
    # the token up front so a bad/expired one is rejected before an account is made.
    # `closed` admits nobody, invite or not — the kill switch.
    if settings.signup_mode == "closed":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "registration is closed")
    invite = None
    if body.invite_token:
        invite = orgs_svc._validate_pending(orgs_svc.invite_by_token(db, body.invite_token))
        if invite.email.lower() != body.email.lower():
            raise HTTPException(status.HTTP_403_FORBIDDEN, "this invitation was sent to a different email address")
    elif settings.signup_mode != "open":
        # invite_only: no self-serve signup without a platform or org invite (AL-93).
        raise HTTPException(status.HTTP_403_FORBIDDEN, "registration is invite-only")
    exists = db.scalar(
        select(User).where((User.email == body.email) | (User.handle == body.handle))
    )
    if exists is not None:
        # Generic message — don't disclose which of email/handle is taken (AL-72).
        raise HTTPException(status.HTTP_409_CONFLICT, "could not create account with those details")
    initials = "".join(p[0] for p in body.name.split()[:2]).upper() or body.name[:2].upper()
    user = User(
        id="u_" + uuid.uuid4().hex[:8],
        name=body.name,
        email=body.email,
        handle=body.handle.lstrip("@"),
        initials=initials,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    if invite is not None:
        if invite.kind == "platform":
            # Nothing to join — the invite authorized the ACCOUNT. The user is routed
            # into the create-your-org onboarding, and any plan preset is applied there.
            orgs_svc.accept_platform_invite(db, body.invite_token, user)
        else:
            # Seat the new user in the org they were invited to (idempotent join).
            orgs_svc.accept_invite(db, body.invite_token, user)
    return TokenOut(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=create_refresh_token(user.id, user.token_version),
    )


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    try:
        payload = decode_token(body.refresh_token, expected_type="refresh")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")
    user = db.get(User, payload.get("sub"))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    # A refresh token from before the last logout/password-change is dead (AL-59).
    if payload.get("tv", 0) != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token revoked")
    return TokenOut(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=create_refresh_token(user.id, user.token_version),
    )


@router.post("/logout", status_code=204)
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Server-side logout (AL-59): bump the user's token_version so every access AND
    refresh token issued so far — on any device — stops validating immediately."""
    user.token_version += 1
    db.commit()


@router.post("/me/password", response_model=TokenOut)
def change_password(
    body: PasswordChangeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change your own password (GRPH-219 follow-up).

    **There was no way to do this at all.** `password_hash` was written at account creation
    — `register`, `seed`, `bootstrap` — and never again, in the API or the UI. So an
    operator provisioned by `bootstrap-hosted` was handed a generated password they could
    never rotate, and anyone who forgot theirs was locked out permanently. Found the moment
    the first hosted operator was told to change the password we had just printed for them.

    **Every existing session dies, including the one that made this call.** Bumping
    `token_version` is the AL-59 revocation machinery, and it is the point rather than a
    side effect: the reason to change a password is usually that the old one is not private
    any more, and leaving previously-issued tokens valid would keep whoever has it signed
    in. A fresh pair comes back so the caller stays logged in without re-entering anything.

    Not rate-limited, deliberately: it already requires a valid access token, so anyone in
    a position to brute-force the current password is someone who has already authenticated
    as this user. Sharing the login bucket would be actively harmful — a few fumbled
    attempts here would lock the account out of `login`, which is the one door still open.
    """
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return TokenOut(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=create_refresh_token(user.id, user.token_version),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.get("/me/memberships")
def my_memberships(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Membership).where(Membership.user_id == user.id)).all()
    out = []
    for m in rows:
        project = db.get(Project, m.project_id)
        out.append({
            "project_id": m.project_id,
            "project_name": project.name if project else m.project_id,
            "accent": project.accent if project else "#c6f24e",
            "role": m.role,
            "access": m.access,
        })
    return out
