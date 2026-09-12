"""Linear integration REST (PRD-P10 §Linear integration adapter).

OAuth flow, webhook receiver, and integration management. The tracker is
authoritative: this surface reads issues, receives webhooks, and writes back
only the constrained set (status, comments, assignee).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.schemas import ORMModel
from app.security.deps import get_current_user
from app.services import linear

logger = logging.getLogger("graphban.linear")

router = APIRouter(prefix="/linear", tags=["linear"])


# ---- Schemas ----

class LinearStatusOut(ORMModel):
    linked: bool
    workspace_id: str
    workspace_name: str
    token_set: bool
    webhook_set: bool
    linked_at: str | None
    last_sync_at: str | None
    last_webhook_at: str | None


class LinearOAuthOut(BaseModel):
    url: str
    state: str


class LinearIssueOut(ORMModel):
    id: str
    identifier: str
    title: str
    assignee_id: str | None
    assignee_name: str | None
    state_name: str
    state_type: str
    canonical_status: str
    team_name: str
    labels: list[str]
    updated_at: str
    url: str


class LinearWriteStatusIn(BaseModel):
    status: str  # backlog | in_progress | review | done


class LinearCommentIn(BaseModel):
    body: str


class LinearAssigneeIn(BaseModel):
    assignee_id: str | None = None


# ---- Integration management ----

@router.get("/status", response_model=LinearStatusOut)
def status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Integration state — never leaks the token."""
    return linear.integration_status(db)


@router.delete("/link")
def unlink(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Unlink the Linear integration. Stops all sync; mirrored data is untouched."""
    removed = linear.unlink_integration(db)
    return {"removed": removed}


# ---- OAuth flow ----

@router.get("/oauth", response_model=LinearOAuthOut)
def start_oauth(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Build the Linear OAuth authorization URL. Returns the URL and the CSRF state token."""
    try:
        url, state = linear.oauth_url()
    except linear.LinearError as exc:
        raise HTTPException(400, str(exc))
    return {"url": url, "state": state}


@router.get("/callback")
def oauth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    """OAuth callback from Linear. Exchanges the code for a token, fetches workspace
    info, and stores the encrypted integration row."""
    if error:
        raise HTTPException(400, f"Linear OAuth error: {error}")
    if not code:
        raise HTTPException(400, "missing authorization code")

    try:
        token_data = linear.exchange_code(code)
    except linear.LinearError as exc:
        raise HTTPException(400, str(exc))

    access_token = token_data.get("access_token", "")
    if not access_token:
        raise HTTPException(400, "no access_token in Linear response")

    client = linear.LinearClient(access_token=access_token)
    try:
        info = linear.fetch_workspace_info(client)
    except linear.LinearError as exc:
        client.close()
        raise HTTPException(400, f"Failed to fetch workspace info: {exc}")
    finally:
        client.close()

    linear.link_integration(
        db,
        access_token=access_token,
        workspace_id=info["workspace_id"],
        workspace_name=info["workspace_name"],
    )

    from app.config import settings
    spa_base = (settings.linear_redirect_uri or settings.app_base_url).rsplit("/api/", 1)[0]
    return RedirectResponse(url=f"{spa_base}/settings/integrations?linear=linked", status_code=302)


# ---- Webhook receiver ----

@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    """Receive Linear webhooks. Verifies the HMAC-SHA256 signature against the stored
    webhook secret. Updates freshness timestamps; the sync engine handles the actual
    data reconciliation."""
    body = await request.body()
    signature = request.headers.get("linear-signature", "")

    integ = linear.get_integration(db)
    if integ is None:
        raise HTTPException(400, "no Linear integration linked")

    stored_secret = ""
    if integ.webhook_secret_enc:
        from app.security import secrets as sec
        stored_secret = sec.decrypt(integ.webhook_secret_enc)

    if stored_secret and not linear.verify_webhook(body, signature, stored_secret):
        raise HTTPException(401, "invalid webhook signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON payload")

    linear.touch_webhook(db)

    action = payload.get("action", "")
    data = payload.get("data", {})
    logger.info("linear webhook: action=%s issue=%s", action, data.get("id", "?"))

    return {"ok": True, "action": action}


# ---- Issue read (for preview / sync engine use) ----

@router.get("/issues")
def list_issues(
    team_id: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fetch issues from the linked Linear workspace. Requires a team_id filter."""
    if not team_id:
        raise HTTPException(422, "team_id is required")
    try:
        client = linear.get_client(db)
    except linear.LinearError as exc:
        raise HTTPException(400, str(exc))
    try:
        issues = linear.fetch_all_issues(client, team_id)
    except linear.LinearError as exc:
        raise HTTPException(502, str(exc))
    finally:
        client.close()
    return [
        {
            "id": i.id,
            "identifier": i.identifier,
            "title": i.title,
            "assignee_id": i.assignee_id,
            "assignee_name": i.assignee_name,
            "state_name": i.state_name,
            "state_type": i.state_type,
            "canonical_status": i.canonical_status,
            "team_name": i.team_name,
            "labels": i.labels,
            "updated_at": i.updated_at,
            "url": i.url,
        }
        for i in issues
    ]


# ---- Write-back ----

@router.post("/issues/{issue_id}/status")
def write_status(
    issue_id: str,
    body: LinearWriteStatusIn,
    team_id: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Write back a status change to Linear. The tracker is authoritative; this is the
    constrained write-back set (status only)."""
    if not team_id:
        raise HTTPException(422, "team_id is required")
    try:
        client = linear.get_client(db)
    except linear.LinearError as exc:
        raise HTTPException(400, str(exc))
    try:
        result = linear.update_issue_status(client, issue_id, body.status, team_id)
    except linear.LinearError as exc:
        raise HTTPException(502, str(exc))
    finally:
        client.close()
    return result


@router.post("/issues/{issue_id}/comment")
def write_comment(
    issue_id: str,
    body: LinearCommentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a comment to a Linear issue."""
    try:
        client = linear.get_client(db)
    except linear.LinearError as exc:
        raise HTTPException(400, str(exc))
    try:
        result = linear.add_comment(client, issue_id, body.body)
    except linear.LinearError as exc:
        raise HTTPException(502, str(exc))
    finally:
        client.close()
    return result


@router.post("/issues/{issue_id}/assignee")
def write_assignee(
    issue_id: str,
    body: LinearAssigneeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update the assignee on a Linear issue. Pass null assignee_id to unassign."""
    try:
        client = linear.get_client(db)
    except linear.LinearError as exc:
        raise HTTPException(400, str(exc))
    try:
        result = linear.update_assignee(client, issue_id, body.assignee_id)
    except linear.LinearError as exc:
        raise HTTPException(502, str(exc))
    finally:
        client.close()
    return result
