"""Upstream feedback — forward a "Report an issue with Graphban" report to the tool
maintainer's intake (another Graphban's public /requests endpoint).

This is the phone-home channel for tool feedback: a deployer runs Graphban for their own
project, but bugs/ideas about Graphban *itself* need to reach whoever develops it. It is
**always initiated** by a person (in-app action) or an agent (MCP tool) — never silent
telemetry — and is disabled by leaving `upstream_feedback_url` blank.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from app import errors
from app.config import settings
from app.services.requests import REQUEST_TYPES
from app.version import __version__

logger = logging.getLogger("graphban.upstream")


#: The intake `upstream_feedback_url` points at by default. It runs in hosted mode, which
#: resolves the project from the share token alone, so a report sent there without
#: UPSTREAM_FEEDBACK_TOKEN 404s and is lost (GRPH-1982).
HOSTED_INTAKE_HOSTS = frozenset({"feedback.asc-me.dev"})

#: The named reason a report is refused before it is sent. Stable: callers branch on it.
UNCONFIGURED = "upstream_feedback_unconfigured"


class UpstreamUnconfigured(errors.Unavailable, ValueError):
    """The report was NOT sent: this server lacks what the upstream needs to accept it.

    `Unavailable` so the MCP dispatcher reports it under its own code and an agent knows
    retrying will not help; `ValueError` so the REST route keeps answering 400 for it.
    """

    code = UNCONFIGURED


def token_required() -> bool:
    if settings.upstream_feedback_token_required is not None:
        return settings.upstream_feedback_token_required
    return target_host() in HOSTED_INTAKE_HOSTS


def unconfigured_reason() -> str | None:
    """Why a report cannot reach upstream from this server, or None when it can.

    Names the missing SETTING, never a value — this is read by `/health`, which is
    unauthenticated, and by the startup log.
    """
    if not settings.upstream_feedback_url:
        return "UPSTREAM_FEEDBACK_URL is blank (upstream reporting is disabled)"
    if token_required() and not settings.upstream_feedback_token:
        return (f"UPSTREAM_FEEDBACK_TOKEN is unset, and the hosted intake at {target_host()} "
                "accepts no report without it")
    return None


def warn_if_unconfigured() -> str | None:
    """Startup check: log why reports cannot be sent, so the gap shows at boot and not as
    the first lost report. A blank URL is a deliberate opt-out and stays quiet. Names the
    setting only — never a token value."""
    why = unconfigured_reason()
    if why and settings.upstream_feedback_url:
        logger.warning("upstream issue reporting is off: %s", why)
        return why
    return None


def report_enabled() -> bool:
    return unconfigured_reason() is None


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
    reason = unconfigured_reason()
    if reason:
        # Refuse before the POST: sending it would only collect the intake's 404, and the
        # caller must learn the report was not sent rather than that it "failed upstream".
        raise UpstreamUnconfigured(
            f"{UNCONFIGURED}: the report was not sent — {reason}",
            hint="configuration, not transient — retrying will not help; the server's "
                 "operator must set it",
        )
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
