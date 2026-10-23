"""Upstream feedback — forward a "Report an issue with Graphban" report to the tool
maintainer's intake (another Graphban's public /requests endpoint).

This is the phone-home channel for tool feedback: a deployer runs Graphban for their own
project, but bugs/ideas about Graphban *itself* need to reach whoever develops it. It is
**always initiated** by a person (in-app action) or an agent (MCP tool) — never silent
telemetry — and is disabled by leaving `upstream_feedback_url` blank.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx

from app.config import settings
from app.services.requests import REQUEST_TYPES
from app.version import __version__


def report_enabled() -> bool:
    return bool(settings.upstream_feedback_url)


def target_host() -> str:
    """The host reports are sent to — surfaced to the user for transparency/consent."""
    return urlparse(settings.upstream_feedback_url).netloc if settings.upstream_feedback_url else ""


def explain_upstream_404() -> str:
    """Name the missing piece after a hosted intake 404 (GRPH-927).

    The intake returns one 404 for both "no share token" and "project not shared" on
    purpose, so the surface cannot be probed. The sender already knows whether it
    sent a token, so that half needs no upstream disclosure: unset → the token is
    the missing piece. Set → the token was sent and NOT accepted, and that is all the
    sender knows: the intake's `_public_project` raises the same 404 for a typo'd token,
    a rotated one, one belonging to another project, and a project with sharing or
    intake off. Stating any one of those as fact is one-message-many-causes with the
    wrong half asserted, so the set case names both. Does not skip the POST — a
    self-hosted intake still accepts a report with no token.
    """
    if not settings.upstream_feedback_token:
        return "UPSTREAM_FEEDBACK_TOKEN unset on this server"
    # No project is named: a hosted intake resolves the project FROM the token and ignores
    # `project_id`, so naming `upstream_feedback_project` could name the wrong one.
    return (
        "UPSTREAM_FEEDBACK_TOKEN was sent and not accepted: either it is not a current "
        "share token, or the project it belongs to has not enabled public sharing (or intake)"
    )


def submit_upstream(*, type_: str, title: str, detail: str = "", source: str = "in-app") -> dict:
    """POST the report to the configured upstream intake. Returns the upstream's response
    ({request, duplicates}). Raises ValueError on config/validation problems and lets
    httpx errors propagate for the caller to translate."""
    url = settings.upstream_feedback_url
    if not url:
        raise ValueError("upstream feedback is not configured")
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    if type_ not in REQUEST_TYPES:
        type_ = "feedback"
    payload = {
        # Both addressing modes, because the receiver decides which one it honours:
        # a hosted intake reads ONLY `token` (project_id is refused so one tenant cannot
        # name another's project, AL-73); a self-hosted one falls back to the raw id.
        # Sending both keeps a single payload correct against either.
        "project_id": settings.upstream_feedback_project,
        "type": type_,
        "title": title,
        "detail": detail or "",
        "source_url": f"graphban:{source}",
        "meta": {"reporter": source, "app_version": __version__},
    }
    if settings.upstream_feedback_token:
        payload["token"] = settings.upstream_feedback_token
    resp = httpx.post(url, json=payload, timeout=10.0)
    resp.raise_for_status()
    return resp.json()
