import uuid
from typing import Literal

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import ApiKey, Membership, Organization, OrgMembership, Project, User
from app.schemas import (MeOut, ProfileIn, LoginIn, PasswordChangeIn, PasswordResetConfirmIn,
                         PasswordResetRequestIn, PreferencesIn, PreferencesOut,
                         RefreshIn, RegisterIn, TokenOut, UserOut)
from app.security.deps import get_current_user
from app.security.jwt import create_access_token, create_refresh_token, decode_token
from app.security.net import client_ip
from app.security.passwords import hash_password, verify_password
from app.services import handles as handles_svc
from app.services import orgs as orgs_svc
from app.services import password_reset as reset_svc
from app.services import preferences as preferences_svc
from app.services import ratelimit
from app.services import setup as setup_svc

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


#: Said for every request, registered address or not. The endpoint must not be an
#: account-enumeration oracle — "no account for that address" tells anyone who asks whether
#: you have an account here, which is worse than the missing feature was.
_RESET_SENT = {"detail": "If an account exists for that address, a reset link has been sent."}


def _guard_reset_rate(request: Request, email: str) -> None:
    """A SEPARATE bucket from login, and that is the point rather than tidiness.

    Sharing it would mean a few reset attempts lock the account out of `login` — the one door
    still open to someone who has just remembered their password. `change_password` avoided
    that same trade-off by not being limited at all, which it can afford because it already
    requires a token; this route is unauthenticated, so it needs a limit of its own.
    """
    per_email = settings.login_rate_per_min
    per_ip = per_email * 3
    ip = client_ip(request)
    if not ratelimit.allow(f"pwreset:email:{email.strip().lower()}", per_email) or not (
        ratelimit.allow(f"pwreset:ip:{ip}", per_ip)
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "too many reset requests; try again shortly")


@router.post("/password-reset", status_code=202)
def request_password_reset(body: PasswordResetRequestIn, request: Request,
                           db: Session = Depends(get_db)):
    """Ask for a reset link (GRPH-359). Always 202, always the same body.

    202 rather than 200: the request has been accepted and a mail may be on its way. It is
    the honest code, because `send_email` never raises and falls back to an in-process outbox
    with no SMTP_HOST — so this route CANNOT observe delivery, and returning 200 "sent" would
    promise something it does not know. The evidence of delivery is the `graphban.email` log
    line, not this response.
    """
    _guard_reset_rate(request, body.email)
    reset_svc.request_reset(db, email=body.email, base_url=settings.app_base_url,
                            ip=client_ip(request))
    # The token is deliberately NOT returned. `request_reset` hands it back for tests; a route
    # that echoed it would make the reset link readable by whoever could call the endpoint,
    # which is everyone.
    return _RESET_SENT


@router.post("/password-reset/confirm", response_model=TokenOut)
def confirm_password_reset(body: PasswordResetConfirmIn, db: Session = Depends(get_db)):
    """Follow the link once and set a new password.

    Returns a fresh token pair so the user lands signed in rather than at a login form they
    have just proved they can get past. Every OTHER session is dead by then — `token_version`
    moved, which is the point: a reset usually means the old password was not private.
    """
    try:
        user = reset_svc.consume_reset(db, token=body.token, new_password=body.new_password)
    except reset_svc.InvalidReset as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    return TokenOut(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=create_refresh_token(user.id, user.token_version),
    )


def _api_key_summary(db: Session, user: User) -> dict:
    keys = list(db.scalars(select(ApiKey).where(ApiKey.user_id == user.id)))
    pids = {k.project_id for k in keys if k.project_id}
    names = sorted(db.scalars(select(Project.name).where(Project.id.in_(pids)))) if pids else []
    return {"count": len(keys), "projects": names}


def _me_out(db: Session, user: User) -> MeOut:
    # Not model_validate(user): the ORM's `api_keys` is the relationship, not the summary.
    return MeOut(id=user.id, name=user.name, handle=user.handle, email=user.email,
                 avatar=user.avatar, avatar2=user.avatar2,
                 avatar_pattern=user.avatar_pattern, initials=user.initials,
                 api_keys=_api_key_summary(db, user))


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _me_out(db, user)


def _refuse(field: str, code: str, **extra) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                         {"field": field, "code": code, **extra})


def _my_orgs(db: Session, user: User) -> list[tuple[str, str]]:
    """(org_id, org_name) for each org the caller belongs to; hosted mode only."""
    if not settings.hosted_mode:
        return []
    rows = db.execute(
        select(Organization.id, Organization.name)
        .join(OrgMembership, OrgMembership.org_id == Organization.id)
        .where(OrgMembership.user_id == user.id).order_by(Organization.id)).all()
    return [(r[0], r[1]) for r in rows]


def _handle_problem(db: Session, user: User, handle: str) -> tuple[str, str | None] | None:
    """(code, org name) when `handle` cannot be used by `user`, else None.

    Reads only the caller's own orgs, so the answer never reveals another tenant."""
    try:
        handles_svc.validate(handle)
    except ValueError as exc:
        return ("handle_reserved" if str(exc) == "reserved" else "handle_invalid", None)
    orgs = _my_orgs(db, user)
    if orgs:
        hit = handles_svc.check_free(db, handle, [o[0] for o in orgs], exclude_user=user.id)
        if hit is not None:
            return ("handle_taken", dict(orgs)[hit])
    elif not settings.hosted_mode and handles_svc.box_handle_taken(db, handle, exclude_user=user.id):
        return ("handle_taken", None)
    return None


@router.patch("/me", response_model=MeOut)
def update_me(body: ProfileIn, user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    """Edit the caller's profile, all or nothing (GRPH-1788). Refusals are field-level:
    `{detail: {field, code, org?}}` with 422."""
    changed = body.model_dump(exclude_unset=True)
    if "name" in changed:
        name = (changed["name"] or "").strip()
        if not name:
            raise _refuse("name", "name_required")
        changed["name"] = name
    if "initials" in changed:
        ini = (changed["initials"] or "").strip()
        if not 1 <= len(ini) <= 3:
            raise _refuse("initials", "initials_invalid")
        changed["initials"] = ini
    old_handle = user.handle
    new_handle = None
    if "handle" in changed:
        new_handle = (changed["handle"] or "").strip().lstrip("@")
        # An unchanged handle is never re-judged: a legacy one that breaks the format
        # must not block saving the other fields.
        if new_handle == old_handle:
            new_handle = None
        else:
            problem = _handle_problem(db, user, new_handle)
            if problem is not None:
                code, org = problem
                extra = {"org": org} if code == "handle_taken" and org else {}
                raise _refuse("handle", code, **extra)
    for f in ("avatar", "avatar_pattern"):
        if f in changed and changed[f] is None:
            raise _refuse(f, f"{f}_invalid")
    try:
        handles_svc.apply_dot(user, {k: v for k, v in changed.items()
                                     if k in ("avatar", "avatar2", "avatar_pattern")})
    except ValueError as exc:
        db.rollback()
        msg = str(exc)
        field = ("avatar2" if msg.startswith("avatar2") else
                 "avatar_pattern" if msg.startswith("pattern") else "avatar")
        raise _refuse(field, f"{field}_invalid") from None
    if "name" in changed:
        user.name = changed["name"]
    if "initials" in changed:
        user.initials = changed["initials"]
    if new_handle is not None:
        org_ids = [o[0] for o in _my_orgs(db, user)]
        handles_svc.write_holds(db, user, old_handle, org_ids)
        if org_ids:
            for m in db.scalars(select(OrgMembership).where(
                    OrgMembership.user_id == user.id, OrgMembership.handle == old_handle)):
                m.handle = new_handle
        user.handle = new_handle
    db.commit()
    db.refresh(user)
    return _me_out(db, user)


@router.get("/me/handle-check")
def handle_check(handle: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """Is `handle` usable by the caller? Covers only the caller's own orgs (GRPH-1788)."""
    if not ratelimit.allow_handle_check(user.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many handle checks; try again shortly")
    handle = handle.strip().lstrip("@")
    problem = None if handle == user.handle else _handle_problem(db, user, handle)
    if problem is None:
        return {"handle": handle, "available": True, "code": None, "org": None}
    code, org = problem
    return {"handle": handle, "available": False, "code": code, "org": org}


class OnboardingMarkIn(BaseModel):
    """One dialog progress mark (GRPH-P67 D8). Unknown fields are refused."""
    model_config = ConfigDict(extra="forbid")
    mark: Literal["quickstart_finished", "quickstart_dismissed", "tour_completed",
                  "tour_offered"]
    # With `tour_completed` only (GRPH-1363): the stops this user's nav skipped.
    skipped_stops: list[str] | None = None


@router.get("/me/onboarding")
def my_onboarding(user: User = Depends(get_current_user)):
    """The caller's quick-start progress, readable before any project exists.

    `onboarding: null` is a new user who has not started (the dialog opens); existing users
    were backfilled as finished (migration 0186). UI progress only, never setup evidence."""
    return {"onboarding": user.onboarding}


@router.patch("/me/onboarding")
def mark_my_onboarding(body: OnboardingMarkIn, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """Stamp one mark: `quickstart_finished` (Finish; clears the top-bar chip) or
    `quickstart_dismissed` (Finish later; the dialog stops opening by itself), or
    `tour_completed` (the app tour's Finish, with `skipped_stops`), or
    `tour_offered` (the one-time "Take the tour" toast was shown; GRPH-1444)."""
    try:
        doc = setup_svc.mark_onboarding(db, user, body.mark, body.skipped_stops)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"onboarding": doc}


class OrgRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    email: str
    note: str = ""


@router.post("/me/org-request")
def request_my_org(body: OrgRequestIn, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """Request an org (GRPH-1364): relay to the operator, then store `org_requested_at`.
    A repeat overwrites the date. 503 when nothing could be sent — nothing is stored then."""
    try:
        doc = setup_svc.request_org(db, user, body.name, body.email, body.note)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except setup_svc.OrgRequestUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {"onboarding": doc}


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


@router.get("/me/preferences", response_model=PreferencesOut)
def my_preferences(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """This user's UI preference document (PRD-60 D1 / GRPH-1133).

    `{saved, preferences}`. `saved: false` means the server has never held a document for this
    user, and it is NOT the same answer as `saved: true` with `{}` — the client's localStorage
    migration uploads its local values once and only in the first case, so a second device
    reads the server copy instead of overwriting the first device's layout with its own.

    The document comes back as written. No defaults are filled in here, because a default the
    server invents is indistinguishable from a choice the user made.
    """
    return preferences_svc.read(db, user.id)


@router.put("/me/preferences", response_model=PreferencesOut)
def put_my_preferences(body: PreferencesIn, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """Replace this user's whole document. Last write wins, across devices and tabs alike.

    An unknown key or a value outside its vocabulary is a 422 naming the field
    (`PreferencesIn`, `extra="forbid"` at every level). A typo'd key stored silently would be
    a save that reports success and changes nothing.

    `exclude_unset` rather than a plain dump: a key the client did not send stays ABSENT in the
    stored document instead of materialising as a server-side default the UI never chose.

    Not recorded as an event, deliberately — this fires on every sidebar toggle and condense
    click, and an audit log that is mostly `nav.collapsed` is one nobody reads.
    """
    return preferences_svc.write(db, user.id, body.model_dump(exclude_unset=True))
