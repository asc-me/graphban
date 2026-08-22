"""Public, unauthenticated feedback intake (Phase 2, AL-19 + AL-21).

Powers the embeddable feedback widget. No JWT — protected by layered spam control
(honeypot + per-project rate limit + optional Turnstile) and a project enable flag.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request as FastAPIRequest,
    Response,
    UploadFile,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import PlatformConfig
from app.schemas import DuplicateHit, PublicRequestIn, PublicRequestOut, RequestOut
from app.services import attachments as att_svc
from app.services import duplicates as dup_svc
from app.services import items as items_svc
from app.services import requests as req_svc
from app.services import ratelimit
from app.services import roadmap as roadmap_svc
from app.services import spam
from app.services.platform import get_config
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


def _public_project(db: Session, token: str | None, project_id: str | None) -> str:
    """Resolve the project a public request targets, and enforce that it opted into
    public sharing (AL-73). Prefer the unguessable share token; in hosted mode ONLY
    the token is accepted, so an attacker can't name another tenant's project_id.
    A project that hasn't opted in (or a bad token) is indistinguishable from
    nonexistent — always 404 — so the surface can't be probed."""
    pid: str | None = None
    if token:
        cfg = db.scalar(select(PlatformConfig).where(PlatformConfig.share_token == token))
        pid = cfg.project_id if cfg else None
    elif not settings.hosted_mode:
        # Self-host convenience: address by raw id or fall back to the sole project.
        pid = resolve_project_id(db, project_id)

    if pid is not None:
        cfg = db.get(PlatformConfig, pid)
        if cfg is not None and cfg.public_share_enabled:
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
    db: Session = Depends(get_db),
):
    _ensure_enabled()
    # 1. Honeypot: a hidden field only bots fill.
    if body.hp:
        raise HTTPException(400, "submission rejected")

    # Only opted-in projects accept public submissions, addressed by share token
    # (or raw id on self-host) — never an arbitrary tenant's project_id (AL-73).
    project_id = _public_project(db, body.token, body.project_id)
    # 2. Per-project rate limit.
    _rate_or_429(db, request, project_id)
    # 3. Optional Turnstile (only enforced when the project configured a secret).
    cfg = get_config(db, project_id)
    if cfg and cfg.turnstile_secret:
        if not spam.verify_turnstile(cfg.turnstile_secret, body.turnstile_token, _client_ip(request)):
            raise HTTPException(403, "captcha verification failed")

    text = f"{body.title} {body.detail}".strip()
    meta = dict(body.meta or {})
    ua = request.headers.get("user-agent")
    if ua and "user_agent" not in meta:
        meta["user_agent"] = ua
    attachment_ids = att_svc.valid_ids(db, body.attachment_ids)
    try:
        req = req_svc.create_request(
            db, type_=body.type, title=body.title, detail=body.detail,
            by=body.email or "public", project_id=project_id,
            source_url=body.source_url, meta=meta, attachment_ids=attachment_ids,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    dups = dup_svc.find_duplicates(db, text, project_id=project_id, exclude_request_id=req.id)
    return PublicRequestOut(
        request=RequestOut.model_validate(req),
        duplicates=[DuplicateHit(**d) for d in dups],
    )
