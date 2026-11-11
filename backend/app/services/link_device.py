"""Deployment sign-in through the console's device-code API (GRPH-1517, P67 §6 L2/L6).

The box relays `POST /api/sync/device/start` and `/poll` on the console. Only an
`approved` poll writes anything; refused and unknown store nothing, and a network error,
timeout or 5xx is `unknown`, never `refused`.
"""
from __future__ import annotations

import platform

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SyncLink, SyncProject, utcnow
from app.security import apikey, secrets
from app.services import code_sync

_TIMEOUT = 10.0
REFUSED_REASONS = ("declined", "expired", "not_member", "no_seat")


class ConsoleUnavailable(Exception):
    """The console could not be reached or answered 5xx — reported as unknown."""


def console_url(link: SyncLink | None, override: str = "") -> str:
    url = (override or (link.console_url if link else "") or (link.cloud_url if link else "")
           or settings.sync_cloud_url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def legacy_key_id(link: SyncLink) -> str | None:
    """The migrated key's public prefix — never the secret."""
    if not link.legacy_key:
        return None
    key = secrets.decrypt(link.api_key_enc) if link.api_key_enc else ""
    return key[: len(apikey.MINT_PREFIX) + 4] if key else None


def _call(method: str, url: str, **kw) -> httpx.Response:
    try:
        resp = httpx.request(method, url, timeout=_TIMEOUT, **kw)
    except httpx.HTTPError as e:
        raise ConsoleUnavailable(f"console unreachable: {e.__class__.__name__}")
    if resp.status_code >= 500:
        raise ConsoleUnavailable(f"console answered {resp.status_code}")
    return resp


def start(db: Session, *, label: str = "", console: str = "") -> dict:
    link = db.get(SyncLink, "instance")
    if link is None:
        link = SyncLink(id="instance")
        db.add(link)
    base = console_url(link, console)
    if not base:
        raise ValueError("no console URL configured")
    body = {"label": label or "graphban box", "host": platform.node()}
    key_id = legacy_key_id(link)
    if key_id:
        body["legacy_key_id"] = key_id
    resp = _call("POST", f"{base}/api/sync/device/start", json=body)
    if resp.status_code >= 400:
        raise ConsoleUnavailable(f"console refused to start: {resp.status_code}")
    data = resp.json()
    link.console_url = base
    link.device_code_enc = secrets.encrypt(data.get("device_code", ""))
    db.commit()
    return {"user_code": data.get("user_code", ""),
            "verification_url": data.get("verification_url", ""),
            "expires_at": data.get("expires_at")}


def poll(db: Session) -> dict:
    link = db.get(SyncLink, "instance")
    if link is None or not link.device_code_enc or not link.console_url:
        return {"status": "unknown", "reason": "no sign-in in progress"}
    try:
        resp = _call("GET", f"{link.console_url}/api/sync/device/poll",
                     params={"device_code": secrets.decrypt(link.device_code_enc)})
        data = resp.json() if resp.status_code < 400 else {}
    except (ConsoleUnavailable, ValueError) as e:
        return {"status": "unknown", "reason": str(e)}
    status = data.get("status")
    if status == "pending":
        return {"status": "pending"}
    if status == "refused":
        reason = data.get("reason")
        if reason in REFUSED_REASONS:
            return {"status": "refused", "reason": reason}
        return {"status": "unknown", "reason": "console refused without a known reason"}
    if status == "approved" and data.get("token"):
        link.token_enc = secrets.encrypt(data["token"])
        link.org = data.get("org", "") or ""
        link.linked_email = data.get("email", "") or ""
        link.legacy_key = ""
        link.device_code_enc = ""
        link.linked_at = utcnow()
        db.commit()
        code_sync._reset_org_cache()
        return {"status": "approved", "org": link.org, "email": link.linked_email}
    return {"status": "unknown", "reason": "unrecognised console answer"}


def status(db: Session) -> dict:
    link = db.get(SyncLink, "instance")
    projects = [{"local_project_id": p.local_project_id, "cloud_project_id": p.cloud_project_id,
                 "last_push_at": p.last_push_at, "last_push_result": p.last_push_result,
                 "last_push_reason": p.last_push_reason}
                for p in db.scalars(select(SyncProject).order_by(SyncProject.local_project_id))]
    results = {p["last_push_result"] for p in projects}
    state = "refused" if "refused" in results else ("ok" if results == {"ok"} else "unknown")
    return {"org": link.org if link else "", "linked_email": link.linked_email if link else "",
            "state": state, "legacy": bool(link and link.legacy_key),
            "mapped_projects": projects}
