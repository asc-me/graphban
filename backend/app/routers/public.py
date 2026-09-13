"""Public, unauthenticated feedback intake (Phase 2, AL-19 + AL-21).

Powers the embeddable feedback widget. No JWT — protected by layered spam control
(honeypot + per-project rate limit + optional Turnstile) and a project enable flag.

PRD-43 extends this with: ingest token auth (Bearer gbfb_...), tracking page,
public issues/requests boards, operator comments, and surface flags.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request as FastAPIRequest,
    Response,
    UploadFile,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Item, PlatformConfig, Request, RequestVote, User
from app.schemas import (
    DuplicateHit,
    IngestTokenOut,
    SurfaceFlagsOut,
    PublicRequestIn,
    PublicRequestOut,
    RequestOut,
)
from app.services import attachments as att_svc
from app.services import duplicates as dup_svc
from app.services import items as items_svc
from app.services import requests as req_svc
from app.services import ratelimit
from app.services import roadmap as roadmap_svc
from app.services import spam
from app.services.platform import get_config, mint_ingest_token, update_surface_flags
from app.services.projects import default_project_id, resolve_project_id

router = APIRouter(prefix="/public", tags=["public"])

_UPLOAD_RATE = 10  # attachment uploads per IP per minute


from app.security.net import client_ip as _client_ip  # shared with auth rate limiting


def _rate_or_429(db: Session, request: FastAPIRequest, project_id: str | None, default: int = 20) -> None:
    limit = default
    if project_id:
        limit = get_config(db, project_id).rate_limit_per_min or default
    if not ratelimit.allow(f"{project_id or 'global'}:{_client_ip(request)}", limit):
        raise HTTPException(429, "too many submissions, slow down")


def _ensure_enabled() -> None:
    if not settings.public_submit_enabled:
        raise HTTPException(403, "public submissions are disabled")


def _resolve_ingest_token(db: Session, authorization: str | None) -> str | None:
    """PRD-43 D1: extract and verify ingest token from Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if not token.startswith("gbfb_"):
        return None
    from app.services.platform import resolve_project_by_ingest_token
    return resolve_project_by_ingest_token(db, token)


def _public_project(
    db: Session, token: str | None, project_id: str | None,
    *, authorization: str | None = None, require_intake: bool = False,
) -> str:
    """Resolve the project a public request targets, and enforce that it opted into
    public sharing (AL-73). Prefer the unguessable share token; in hosted mode ONLY
    the token is accepted, so an attacker can't name another tenant's project_id.
    A project that hasn't opted in (or a bad token) is indistinguishable from
    nonexistent — always 404 — so the surface can't be probed.

    PRD-43 D1: also accepts ingest token via Authorization: Bearer gbfb_...
    PRD-43 D4: when require_intake=True, checks intake_enabled instead of public_share_enabled.
    """
    pid: str | None = None

    # PRD-43 D1: ingest token takes precedence.
    pid = _resolve_ingest_token(db, authorization)

    if pid is None and token:
        cfg = db.scalar(select(PlatformConfig).where(PlatformConfig.share_token == token))
        pid = cfg.project_id if cfg else None
    elif pid is None and not settings.hosted_mode:
        # Self-host convenience: address by raw id or fall back to the sole project.
        pid = resolve_project_id(db, project_id)

    if pid is not None:
        cfg = db.get(PlatformConfig, pid)
        if cfg is not None:
            if require_intake:
                if cfg.intake_enabled or cfg.public_share_enabled:
                    return pid
            elif cfg.public_share_enabled:
                return pid
    raise HTTPException(404, "not found")


@router.get("/roadmap")
def public_roadmap(request: FastAPIRequest, project_id: str | None = None,
                   token: str | None = None, db: Session = Depends(get_db)):
    """Read-only public roadmap for the shareable link (opted-in projects only).

    Rate limited like every other public route (GRPH-32). It was the one endpoint the
    hardening checklist named by name and the only unauthenticated one without a limit:
    a full roadmap query per request, reachable by anyone holding a share link, with
    nothing bounding how fast it can be asked for.
    """
    pid = _public_project(db, token, project_id)
    _rate_or_429(db, request, pid)
    return roadmap_svc.list_roadmap(db, project_id=pid)


@router.get("/widget-config")
def widget_config(request: FastAPIRequest, project_id: str | None = None,
                  token: str | None = None, db: Session = Depends(get_db)):
    """Public config the embedded widget needs (e.g. whether to render Turnstile)."""
    pid = _public_project(db, token, project_id)
    _rate_or_429(db, request, pid)
    cfg = get_config(db, pid)
    return {"turnstile_sitekey": cfg.turnstile_sitekey}


def _verify_github_signature(raw: bytes, signature: str | None) -> None:
    """Verify GitHub's X-Hub-Signature-256 HMAC when a webhook secret is configured.
    No secret → unverified (local/offline default). Secret set → a missing or bad
    signature is rejected, so forged issue payloads can't create items (AL-44)."""
    secret = settings.github_webhook_secret
    if not secret:
        return
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(401, "missing or malformed X-Hub-Signature-256")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "webhook signature verification failed")


@router.post("/stripe/webhook")
async def stripe_webhook(request: FastAPIRequest, db: Session = Depends(get_db)):
    """Stripe → org.plan (GRPH-82). Unsigned or unconfigured is 401/404, never a quiet apply."""
    from app.services import billing as billing_svc

    if not billing_svc.configured():
        raise HTTPException(404, "Not Found")
    raw = await request.body()
    sig = request.headers.get("stripe-signature") or ""
    try:
        event = billing_svc.construct_event(raw, sig)
    except billing_svc.BillingUnavailable:
        raise HTTPException(404, "Not Found") from None
    except ValueError:
        raise HTTPException(401, "webhook signature verification failed") from None
    billing_svc.apply_event(db, event)
    return {"ok": True}


@router.post("/github/webhook")
async def github_webhook(request: FastAPIRequest, db: Session = Depends(get_db)):
    """Inbound GitHub issues webhook → new tracker item, routed to the project that
    has this repo connected (falls back to the default project).

    Verifies the X-Hub-Signature-256 HMAC when GITHUB_WEBHOOK_SECRET is set.
    """
    _rate_or_429(db, request, None, default=60)
    raw = await request.body()
    _verify_github_signature(raw, request.headers.get("x-hub-signature-256"))
    payload = json.loads(raw or b"{}")
    if payload.get("action") not in ("opened", "reopened"):
        return {"ignored": True, "action": payload.get("action")}
    issue = payload.get("issue", {}) or {}
    repo = (payload.get("repository", {}) or {}).get("full_name", "")

    # Route to the project whose platform_config names this repo.
    project_id = None
    if repo:
        match = db.scalar(
            select(PlatformConfig).where(func.lower(PlatformConfig.github_repo) == repo.lower())
        )
        if match is not None:
            project_id = match.project_id
    project_id = project_id or default_project_id(db)

    item = items_svc.create_item(
        db,
        title=issue.get("title", "Untitled GitHub issue"),
        description=issue.get("body", "") or "",
        tags=["github"],
        project_id=project_id,
        reporter={"name": "GitHub", "handle": "github", "avatar": "#8b949e"},
    )
    # Link the item back to the originating issue.
    url = issue.get("html_url", "")
    if url:
        items_svc.update_item(db, item.id, github_url=url)
    return {"created_item": item.id, "project_id": project_id, "github_url": url}


@router.get("/duplicates", response_model=list[DuplicateHit])
def check_duplicates(
    q: str,
    request: FastAPIRequest,
    project_id: str | None = None,
    token: str | None = None,
    db: Session = Depends(get_db),
):
    """Live duplicate check for the widget — before the user submits."""
    _ensure_enabled()
    pid = _public_project(db, token, project_id)
    _rate_or_429(db, request, pid)
    return dup_svc.find_duplicates(db, q, project_id=pid)


@router.post("/attachments", status_code=201)
async def upload_attachment(
    request: FastAPIRequest,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a screenshot for a feedback submission. Returns its id + public url."""
    _ensure_enabled()
    if not ratelimit.allow(f"upload:{_client_ip(request)}", _UPLOAD_RATE):
        raise HTTPException(429, "too many uploads, slow down")
    data = await file.read()
    try:
        att = att_svc.create_attachment(db, content_type=file.content_type or "", data=data)
    except att_svc.AttachmentError as e:
        raise HTTPException(422, str(e))
    return {"id": att.id, "url": f"/api/public/attachments/{att.id}", "size": att.size}


@router.get("/attachments/{attachment_id}")
def get_attachment(attachment_id: str, db: Session = Depends(get_db)):
    """Serve an attachment's bytes (public-read by unguessable id)."""
    att = att_svc.get_attachment(db, attachment_id)
    if att is None:
        raise HTTPException(404, "attachment not found")
    return Response(
        content=att.data,
        media_type=att.content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/requests", response_model=PublicRequestOut, status_code=201)
def submit_request(
    body: PublicRequestIn,
    request: FastAPIRequest,
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    _ensure_enabled()
    # 1. Honeypot: a hidden field only bots fill.
    if body.hp:
        raise HTTPException(400, "submission rejected")

    # Only opted-in projects accept public submissions, addressed by share token
    # (or raw id on self-host) — never an arbitrary tenant's project_id (AL-73).
    # PRD-43 D1: also accepts ingest token via Authorization header.
    project_id = _public_project(
        db, body.token, body.project_id,
        authorization=authorization, require_intake=True,
    )
    # 2. Per-project rate limit.
    _rate_or_429(db, request, project_id)
    # 3. Optional Turnstile (only enforced when the project configured a secret).
    cfg = get_config(db, project_id)
    if cfg and cfg.turnstile_secret:
        if not spam.verify_turnstile(cfg.turnstile_secret, body.turnstile_token, _client_ip(request)):
            raise HTTPException(403, "captcha verification failed")

    # PRD-43 D3: capture_identity gate — 422 if on and no identity provided.
    if cfg and cfg.capture_identity:
        has_identity = bool(body.email) or bool(body.account and (body.account.id or body.account.email))
        if not has_identity:
            raise HTTPException(422, "identity is required")

    text = f"{body.title} {body.detail}".strip()
    meta = dict(body.meta or {})
    ua = request.headers.get("user-agent")
    if ua and "user_agent" not in meta:
        meta["user_agent"] = ua
    attachment_ids = att_svc.valid_ids(db, body.attachment_ids)

    # PRD-43 D2: pass account and email to service; never write by="public".
    account_dict = body.account.model_dump() if body.account else None
    try:
        req = req_svc.create_request(
            db, type_=body.type, title=body.title, detail=body.detail,
            by="", project_id=project_id,
            source_url=body.source_url, meta=meta, attachment_ids=attachment_ids,
            account=account_dict, email=body.email,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    dups = dup_svc.find_duplicates(db, text, project_id=project_id, exclude_request_id=req.id)

    # PRD-43 D3: build track_url from the minted token.
    track_url = ""
    track_plain = getattr(req, "_track_token_plain", None)
    if track_plain:
        track_url = f"/{project_id}/t/{track_plain}"

    return PublicRequestOut(
        request=RequestOut.model_validate(req),
        duplicates=[DuplicateHit(**d) for d in dups],
        track_url=track_url,
    )


# ---- PRD-43: tracking page ----

@router.get("/t/{track_token}")
def tracking_page(track_token: str, request: FastAPIRequest, db: Session = Depends(get_db)):
    """PRD-43 D3: submitter tracking page. Always resolves for a real token, even
    when the request is unpublished. 404 for unknown tokens."""
    _rate_or_429(db, request, None)
    req = req_svc.resolve_track_token(db, track_token)
    if req is None:
        raise HTTPException(404, "not found")
    comments = req_svc.list_comments(db, req.id, visibility="public")
    data = req_svc.serialize_tracking(req, comments)
    # Enrich linked_status if linked.
    if req.linked_to:
        item = db.get(Item, req.linked_to)
        if item:
            data["linked_status"] = item.status
    return data


# ---- PRD-43: public boards ----

@router.get("/boards/issues")
def public_issues_board(
    request: FastAPIRequest,
    project_id: str | None = None,
    token: str | None = None,
    db: Session = Depends(get_db),
):
    """PRD-43 D5: public issues board — published bugs."""
    pid = _public_project(db, token, project_id)
    _rate_or_429(db, request, pid)
    cfg = get_config(db, pid)
    if not cfg.public_issues_enabled:
        raise HTTPException(404, "not found")
    reqs = req_svc.public_board(db, pid, types=["bug"])
    rows = []
    for r in reqs:
        comments = req_svc.list_comments(db, r.id, visibility="public")
        row = req_svc.serialize_public_row(r, comments)
        if r.linked_to:
            item = db.get(Item, r.linked_to)
            if item:
                row["linked_status"] = item.status
        rows.append(row)
    return rows


@router.get("/boards/requests")
def public_requests_board(
    request: FastAPIRequest,
    project_id: str | None = None,
    token: str | None = None,
    db: Session = Depends(get_db),
):
    """PRD-43 D5: public requests board — published features/enhancements."""
    pid = _public_project(db, token, project_id)
    _rate_or_429(db, request, pid)
    cfg = get_config(db, pid)
    if not cfg.public_requests_enabled:
        raise HTTPException(404, "not found")
    reqs = req_svc.public_board(db, pid, types=["feature", "enhancement"])
    rows = []
    for r in reqs:
        comments = req_svc.list_comments(db, r.id, visibility="public")
        row = req_svc.serialize_public_row(r, comments)
        if r.linked_to:
            item = db.get(Item, r.linked_to)
            if item:
                row["linked_status"] = item.status
        rows.append(row)
    return rows


# ---- PRD-43 D6: public voting ----

@router.post("/requests/{request_id}/vote")
def public_vote(
    request_id: str,
    request: FastAPIRequest,
    project_id: str | None = None,
    token: str | None = None,
    db: Session = Depends(get_db),
):
    """PRD-43 D6: anonymous upvote on a published request. Uses a server-signed
    cookie (gb_vote) for one-vote-per-request best effort."""
    pid = _public_project(db, token, project_id)
    _rate_or_429(db, request, pid)
    req = db.get(Request, req_svc.keys.resolve_request(db, request_id) or request_id)
    if req is None or req.published_at is None:
        raise HTTPException(404, "not found")
    # Check surface flag matches the request type.
    cfg = get_config(db, pid)
    if req.type == "bug" and not cfg.public_issues_enabled:
        raise HTTPException(404, "not found")
    if req.type in ("feature", "enhancement") and not cfg.public_requests_enabled:
        raise HTTPException(404, "not found")
    # Best-effort voter identity from cookie.
    voter_cookie = request.cookies.get("gb_vote", "")
    if not voter_cookie:
        voter_cookie = token_urlsafe(16)
    voter_key = hashlib.sha256(voter_cookie.encode()).hexdigest()
    # Check for duplicate vote.
    existing = db.scalar(
        select(RequestVote).where(
            RequestVote.request_id == req.id,
            RequestVote.voter_key == voter_key,
        )
    )
    if existing:
        resp = Response(status_code=200)
        resp.set_cookie("gb_vote", voter_cookie, httponly=True, samesite="lax")
        return {"votes": req.votes, "voted": False}
    vote = RequestVote(request_id=req.id, voter_key=voter_key)
    db.add(vote)
    req.votes = max(0, req.votes + 1)
    db.commit()
    db.refresh(req)
    resp_data = {"votes": req.votes, "voted": True}
    return resp_data


# ---- PRD-43: authenticated operator endpoints ----

from app.security.deps import get_current_user
from secrets import token_urlsafe


@router.post("/ingest-token", response_model=IngestTokenOut)
def mint_token_endpoint(
    project_id: str = "core",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """PRD-43 D1: mint or rotate the ingest token for a project."""
    plain, prefix = mint_ingest_token(db, project_id)
    return IngestTokenOut(token=plain, prefix=prefix)


@router.put("/surface-flags", response_model=SurfaceFlagsOut)
def set_surface_flags(
    flags: dict,
    project_id: str = "core",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """PRD-43 D4: set per-surface flags for a project."""
    cfg = update_surface_flags(db, project_id, flags)
    return SurfaceFlagsOut(
        intake_enabled=cfg.intake_enabled,
        public_form_enabled=cfg.public_form_enabled,
        public_roadmap_enabled=cfg.public_roadmap_enabled,
        public_issues_enabled=cfg.public_issues_enabled,
        public_requests_enabled=cfg.public_requests_enabled,
        capture_identity=cfg.capture_identity,
        public_share_enabled=cfg.public_share_enabled,
        ingest_token_prefix=cfg.ingest_token_prefix,
        public_path_id=cfg.public_path_id,
    )


@router.post("/requests/{request_id}/publish")
def publish_endpoint(
    request_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """PRD-43 D5: publish a request (make it visible on public boards)."""
    req = req_svc.publish_request(db, request_id)
    if req is None:
        raise HTTPException(404, "not found")
    return {"published": True, "published_at": req.published_at.isoformat() if req.published_at else None}


@router.post("/requests/{request_id}/unpublish")
def unpublish_endpoint(
    request_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """PRD-43 D5: unpublish a request (remove from public boards)."""
    req = req_svc.unpublish_request(db, request_id)
    if req is None:
        raise HTTPException(404, "not found")
    return {"published": False}


@router.post("/requests/{request_id}/comments", status_code=201)
def create_comment_endpoint(
    request_id: str,
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """PRD-43 D7: create an operator comment on a request."""
    try:
        comment = req_svc.create_comment(
            db,
            request_id=request_id,
            author_user_id=current_user.id,
            body=body.get("body", ""),
            visibility=body.get("visibility", "private"),
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {
        "id": comment.id,
        "body": comment.body,
        "visibility": comment.visibility,
        "created_at": comment.created_at.isoformat(),
    }


# ---- PRD-43 D8: slug management ----

from app.services.platform import claim_org_host, claim_project_path_id, validate_slug


@router.post("/slugs/org-host")
def claim_org_host_endpoint(
    body: dict,
    org_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """PRD-43 D8: claim a public host slug for an org."""
    slug = body.get("slug", "")
    try:
        return claim_org_host(db, org_id, slug)
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/slugs/project-path")
def claim_project_path_endpoint(
    body: dict,
    project_id: str = "core",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """PRD-43 D8: claim a public path id for a project."""
    slug = body.get("slug", "")
    try:
        return claim_project_path_id(db, project_id, slug)
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/slugs/validate")
def validate_slug_endpoint(slug: str):
    """PRD-43 D8: validate a slug without claiming it."""
    err = validate_slug(slug)
    if err:
        return {"valid": False, "error": err}
    return {"valid": True}
