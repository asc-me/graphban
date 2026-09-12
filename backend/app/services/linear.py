"""Linear integration adapter (PRD-P10 §Linear integration adapter).

OAuth + GraphQL client for Linear: read issues (assignee, state, team, labels,
updatedAt), subscribe to webhooks, and write back the constrained set (status,
comments, assignee). Canonical status mapping: Linear workflow states → AgentLedger
backlog/in_progress/review/done. Rate-limit aware; token stored per-org encrypted.

The tracker is AUTHORITATIVE. This adapter reads heavily and writes back only the
constrained set. Description is READ-from-tracker only — never written back.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import LinearIntegration, utcnow
from app.security import secrets as sec

logger = logging.getLogger("graphban.linear")

LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_AUTH_URL = "https://linear.app/oauth/approve"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"

# Canonical status mapping: Linear workflow state type → AgentLedger status.
# Linear states have a `type` field: backlog, unstarted, started, completed, canceled, triage.
# Map: backlog→backlog, unstarted→backlog, started→in_progress, completed→done,
# canceled→done, triage→backlog.
LINEAR_STATE_TYPE_TO_STATUS: dict[str, str] = {
    "backlog": "backlog",
    "unstarted": "backlog",
    "triage": "backlog",
    "started": "in_progress",
    "completed": "done",
    "canceled": "done",
}

# Valid AgentLedger statuses for write-back.
VALID_STATUSES = {"backlog", "in_progress", "review", "done"}

# Reverse map: AgentLedger status → Linear state type preference order.
# "review" has no direct Linear equivalent; map to "started" (in-progress) as the
# closest semantic match — a ticket in review is still being worked.
STATUS_TO_LINEAR_STATE_TYPE: dict[str, str] = {
    "backlog": "unstarted",
    "in_progress": "started",
    "review": "started",
    "done": "completed",
}

# Linear GraphQL rate limit: 50 points/second, bucket of 1500. Queries cost 1-10 points.
# Track remaining budget and back off when low.
_RATE_LIMIT_THRESHOLD = 50  # pause when remaining drops below this


@dataclass
class RateLimitState:
    """Tracks Linear's GraphQL rate-limit budget from response headers."""
    remaining: int = 1500
    limit: int = 1500
    reset_at: float = 0.0

    @property
    def is_low(self) -> bool:
        return self.remaining < _RATE_LIMIT_THRESHOLD

    def update_from_headers(self, headers: httpx.Headers) -> None:
        """Extract rate-limit state from Linear's GraphQL response headers."""
        if "x-ratelimit-requests-remaining" in headers:
            self.remaining = int(headers["x-ratelimit-requests-remaining"])
        if "x-ratelimit-requests-limit" in headers:
            self.limit = int(headers["x-ratelimit-requests-limit"])


@dataclass
class LinearIssue:
    """Normalized issue from Linear."""
    id: str
    identifier: str  # e.g. "ENG-123"
    title: str
    description: str | None
    assignee_id: str | None
    assignee_name: str | None
    state_id: str
    state_name: str
    state_type: str  # backlog | unstarted | started | completed | canceled | triage
    team_id: str
    team_name: str
    labels: list[str]
    updated_at: str
    url: str

    @property
    def canonical_status(self) -> str:
        return LINEAR_STATE_TYPE_TO_STATUS.get(self.state_type, "backlog")


@dataclass
class LinearClient:
    """Authenticated Linear GraphQL client with rate-limit tracking."""
    access_token: str
    _rate: RateLimitState = field(default_factory=RateLimitState)
    _http: httpx.Client | None = field(default=None, repr=False)

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=LINEAR_API_URL,
                headers={
                    "Authorization": self.access_token,
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    @property
    def rate_limit(self) -> RateLimitState:
        return self._rate

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL request. Updates rate-limit state from response headers.
        Raises LinearError on GraphQL errors or transport failures."""
        if self._rate.is_low:
            wait = max(0, self._rate.reset_at - time.time())
            if wait > 0:
                logger.info("linear: rate-limit low (%d remaining), sleeping %.1fs",
                            self._rate.remaining, wait)
                time.sleep(min(wait, 5.0))

        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = self.http.post("", json=payload)
        self._rate.update_from_headers(resp.headers)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "5"))
            logger.warning("linear: rate-limited, retrying after %.1fs", retry_after)
            time.sleep(min(retry_after, 10.0))
            resp = self.http.post("", json=payload)
            self._rate.update_from_headers(resp.headers)

        if resp.status_code != 200:
            raise LinearError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if "errors" in data:
            msgs = [e.get("message", "?") for e in data["errors"]]
            raise LinearError(f"GraphQL: {'; '.join(msgs)}")
        return data.get("data", {})


class LinearError(Exception):
    """A Linear API call failed."""


# ---- OAuth flow ----

def oauth_url(state: str | None = None) -> str:
    """Build the Linear OAuth authorization URL. Raises if client_id is not configured."""
    if not settings.linear_client_id:
        raise LinearError("LINEAR_CLIENT_ID is not configured")
    state = state or secrets.token_urlsafe(24)
    redirect = settings.linear_redirect_uri or f"{settings.app_base_url}/api/linear/callback"
    params = {
        "client_id": settings.linear_client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "read,write,comment:create",
        "state": state,
        "actor": "application",
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{LINEAR_AUTH_URL}?{qs}", state


def exchange_code(code: str) -> dict:
    """Exchange an OAuth authorization code for an access token.
    Returns {"access_token": ..., "token_type": ...}."""
    if not settings.linear_client_id or not settings.linear_client_secret:
        raise LinearError("LINEAR_CLIENT_ID / LINEAR_CLIENT_SECRET not configured")
    redirect = settings.linear_redirect_uri or f"{settings.app_base_url}/api/linear/callback"
    resp = httpx.post(
        LINEAR_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": settings.linear_client_id,
            "client_secret": settings.linear_client_secret,
            "redirect_uri": redirect,
            "code": code,
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise LinearError(f"Token exchange failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


# ---- Webhook verification ----

def verify_webhook(payload: bytes, signature: str, secret: str) -> bool:
    """Verify a Linear webhook signature (HMAC-SHA256 of the raw body).
    Linear signs with the webhook secret using SHA256, sent as hex in the
    ``Linear-Signature`` header."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---- Client factory ----

def get_client(db: Session, *, org_id: str | None = None) -> LinearClient:
    """Build an authenticated Linear client for the given org.
    Raises LinearError if no integration is linked or the token cannot be decrypted."""
    integration = get_integration(db, org_id=org_id)
    if integration is None:
        raise LinearError("No Linear integration linked for this org")
    token = sec.decrypt(integration.access_token_enc)
    if not token:
        raise LinearError("Linear access token is empty or decryption failed")
    return LinearClient(access_token=token)


def get_integration(db: Session, *, org_id: str | None = None) -> LinearIntegration | None:
    """Fetch the Linear integration row for an org (or the singleton for self-host)."""
    if org_id:
        stmt = select(LinearIntegration).where(LinearIntegration.org_id == org_id)
    else:
        stmt = select(LinearIntegration).where(LinearIntegration.org_id.is_(None))
    return db.scalars(stmt).first()


# ---- Integration lifecycle ----

def link_integration(
    db: Session,
    *,
    access_token: str,
    workspace_id: str,
    workspace_name: str,
    webhook_secret: str = "",
    org_id: str | None = None,
) -> LinearIntegration:
    """Create or update the Linear integration for an org. Tokens are encrypted at rest."""
    integ = get_integration(db, org_id=org_id)
    if integ is None:
        integ = LinearIntegration(id=f"linteg_{uuid.uuid4().hex[:12]}", org_id=org_id)
        db.add(integ)
    integ.access_token_enc = sec.encrypt(access_token)
    integ.workspace_id = workspace_id
    integ.workspace_name = workspace_name
    if webhook_secret:
        integ.webhook_secret_enc = sec.encrypt(webhook_secret)
    integ.updated_at = utcnow()
    db.commit()
    db.refresh(integ)
    return integ


def unlink_integration(db: Session, *, org_id: str | None = None) -> bool:
    """Remove the Linear integration for an org. Returns True if a row was deleted."""
    integ = get_integration(db, org_id=org_id)
    if integ is None:
        return False
    db.delete(integ)
    db.commit()
    return True


def integration_status(db: Session, *, org_id: str | None = None) -> dict:
    """Link state for the UI. Never leaks the token."""
    integ = get_integration(db, org_id=org_id)
    if integ is None:
        return {
            "linked": False,
            "workspace_id": "",
            "workspace_name": "",
            "token_set": False,
            "webhook_set": False,
            "linked_at": None,
            "last_sync_at": None,
            "last_webhook_at": None,
        }
    return {
        "linked": True,
        "workspace_id": integ.workspace_id,
        "workspace_name": integ.workspace_name,
        "token_set": integ.token_set,
        "webhook_set": bool(integ.webhook_secret_enc),
        "linked_at": integ.linked_at.isoformat() if integ.linked_at else None,
        "last_sync_at": integ.last_sync_at.isoformat() if integ.last_sync_at else None,
        "last_webhook_at": integ.last_webhook_at.isoformat() if integ.last_webhook_at else None,
    }


def touch_webhook(db: Session, *, org_id: str | None = None) -> None:
    """Record that a webhook was received (freshness tracking)."""
    integ = get_integration(db, org_id=org_id)
    if integ is not None:
        integ.last_webhook_at = utcnow()
        db.commit()


def touch_sync(db: Session, *, org_id: str | None = None) -> None:
    """Record that a reconcile poll completed."""
    integ = get_integration(db, org_id=org_id)
    if integ is not None:
        integ.last_sync_at = utcnow()
        db.commit()


# ---- GraphQL queries ----

_ISSUES_QUERY = """
query Issues($teamId: String!, $after: String, $first: Int) {
  issues(
    filter: { team: { id: { eq: $teamId } } }
    orderBy: updatedAt
    after: $after
    first: $first
  ) {
    nodes {
      id
      identifier
      title
      description
      assignee { id name }
      state { id name type }
      team { id name }
      labels { nodes { id name } }
      updatedAt
      url
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_ISSUE_BY_ID_QUERY = """
query Issue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    assignee { id name }
    state { id name type }
    team { id name }
    labels { nodes { id name } }
    updatedAt
    url
  }
}
"""

_WORKSPACE_QUERY = """
query Viewer {
  viewer {
    id
    name
    organization { id name }
  }
}
"""

_TEAM_STATES_QUERY = """
query TeamStates($teamId: String!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name type }
  }
}
"""

_UPDATE_STATUS_MUTATION = """
mutation UpdateIssueStatus($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) {
    success
    issue { id state { id name type } updatedAt }
  }
}
"""

_ADD_COMMENT_MUTATION = """
mutation AddComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id body createdAt }
  }
}
"""

_UPDATE_ASSIGNEE_MUTATION = """
mutation UpdateAssignee($id: String!, $assigneeId: String) {
  issueUpdate(id: $id, input: { assigneeId: $assigneeId }) {
    success
    issue { id assignee { id name } updatedAt }
  }
}
"""


def _parse_issue(node: dict) -> LinearIssue:
    """Parse a GraphQL issue node into a LinearIssue."""
    assignee = node.get("assignee") or {}
    state = node.get("state") or {}
    team = node.get("team") or {}
    labels_data = node.get("labels", {}).get("nodes", [])
    return LinearIssue(
        id=node["id"],
        identifier=node.get("identifier", ""),
        title=node.get("title", ""),
        description=node.get("description"),
        assignee_id=assignee.get("id"),
        assignee_name=assignee.get("name"),
        state_id=state.get("id", ""),
        state_name=state.get("name", ""),
        state_type=state.get("type", "backlog"),
        team_id=team.get("id", ""),
        team_name=team.get("name", ""),
        labels=[lb["name"] for lb in labels_data],
        updated_at=node.get("updatedAt", ""),
        url=node.get("url", ""),
    )


def fetch_workspace_info(client: LinearClient) -> dict:
    """Fetch the authenticated user's workspace info."""
    data = client.graphql(_WORKSPACE_QUERY)
    viewer = data.get("viewer", {})
    org = viewer.get("organization", {})
    return {
        "viewer_id": viewer.get("id", ""),
        "viewer_name": viewer.get("name", ""),
        "workspace_id": org.get("id", ""),
        "workspace_name": org.get("name", ""),
    }


def fetch_issues(
    client: LinearClient,
    team_id: str,
    *,
    first: int = 50,
    after: str | None = None,
) -> tuple[list[LinearIssue], dict]:
    """Fetch issues for a team, paginated. Returns (issues, page_info)."""
    data = client.graphql(_ISSUES_QUERY, {"teamId": team_id, "first": first, "after": after})
    issues_data = data.get("issues", {})
    nodes = issues_data.get("nodes", [])
    page_info = issues_data.get("pageInfo", {})
    issues = [_parse_issue(n) for n in nodes]
    return issues, page_info


def fetch_all_issues(client: LinearClient, team_id: str) -> list[LinearIssue]:
    """Fetch all issues for a team, paginating through the full set."""
    all_issues: list[LinearIssue] = []
    after = None
    while True:
        issues, page_info = fetch_issues(client, team_id, after=after)
        all_issues.extend(issues)
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return all_issues


def fetch_issue(client: LinearClient, issue_id: str) -> LinearIssue:
    """Fetch a single issue by Linear ID."""
    data = client.graphql(_ISSUE_BY_ID_QUERY, {"id": issue_id})
    node = data.get("issue")
    if node is None:
        raise LinearError(f"Issue {issue_id} not found")
    return _parse_issue(node)


def fetch_team_states(client: LinearClient, team_id: str) -> list[dict]:
    """Fetch all workflow states for a team (for status mapping)."""
    data = client.graphql(_TEAM_STATES_QUERY, {"teamId": team_id})
    return data.get("workflowStates", {}).get("nodes", [])


def find_state_for_type(states: list[dict], target_type: str) -> str | None:
    """Find a state ID matching the target Linear state type. Returns None if no match."""
    for s in states:
        if s.get("type") == target_type:
            return s["id"]
    return None


# ---- Write-back operations ----

def update_issue_status(
    client: LinearClient,
    issue_id: str,
    target_status: str,
    team_id: str,
) -> dict:
    """Update an issue's status. Maps AgentLedger status → Linear workflow state.
    Returns the updated issue data."""
    if target_status not in VALID_STATUSES:
        raise LinearError(f"Invalid status: {target_status}")
    states = fetch_team_states(client, team_id)
    target_type = STATUS_TO_LINEAR_STATE_TYPE.get(target_status, "unstarted")
    state_id = find_state_for_type(states, target_type)
    if state_id is None:
        raise LinearError(f"No Linear workflow state of type '{target_type}' found for team")
    data = client.graphql(_UPDATE_STATUS_MUTATION, {"id": issue_id, "stateId": state_id})
    result = data.get("issueUpdate", {})
    if not result.get("success"):
        raise LinearError("Linear issueUpdate (status) returned success=false")
    return result.get("issue", {})


def add_comment(client: LinearClient, issue_id: str, body: str) -> dict:
    """Add a comment to a Linear issue."""
    data = client.graphql(_ADD_COMMENT_MUTATION, {"issueId": issue_id, "body": body})
    result = data.get("commentCreate", {})
    if not result.get("success"):
        raise LinearError("Linear commentCreate returned success=false")
    return result.get("comment", {})


def update_assignee(
    client: LinearClient,
    issue_id: str,
    assignee_id: str | None,
) -> dict:
    """Update an issue's assignee. Pass None to unassign."""
    data = client.graphql(_UPDATE_ASSIGNEE_MUTATION, {"id": issue_id, "assigneeId": assignee_id})
    result = data.get("issueUpdate", {})
    if not result.get("success"):
        raise LinearError("Linear issueUpdate (assignee) returned success=false")
    return result.get("issue", {})
