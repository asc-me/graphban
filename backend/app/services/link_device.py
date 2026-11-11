"""Signing a deployment in through the console's device code (GRPH-1517, P67 §6 L2/L6).

The box relays the console's device-code API (GBC-77, graphban-control `docs/contract.md`):

- `POST {console}/api/sync/device/start {label, host, legacy_key_id?}` →
  `{device_code, user_code, verification_url, expires_at, interval}`.
- `POST {console}/api/sync/device/poll {device_code}` → `pending`, `approved {org:{id,name},
  email, token}` (the token only on the poll that first sees approval), or `refused {reason}`.
  An unknown `device_code` is 404.

Three states, never two. **Only an approved poll writes the link**; refused and unknown store
nothing. A network error, timeout or 5xx is `unknown`, never `refused`, because no answer is
not a no. The token and the legacy key never leave this module in a return value.
"""
from __future__ import annotations

import json
import platform

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SyncLink, SyncProject, utcnow
from app.security import apikey, secrets

_LINK_ID = "instance"
_TIMEOUT = 10.0

#: The console's definite noes on a poll. Anything else it says is not a refusal we know.
REFUSED_REASONS = ("declined", "expired", "not_member", "no_seat")
#: Push refusals that are about the credential, not one project (L5): they refuse the link.
LINK_REFUSALS = ("seat_removed", "token_revoked")
#: Key prefixes whose public part the console can match, and its length past the prefix.
_KEY_PREFIXES = ("gb_sync_",) + apikey.ACCEPTED_PREFIXES
_PUBLIC_CHARS = 4


class NoConsole(Exception):
    """No console URL to sign in to."""


class NoSignIn(Exception):
    """A poll with no sign-in in flight: `link/start` was never called, or already approved."""


class _Unknown(Exception):
    """The console gave no answer we can act on."""


def _link(db: Session) -> SyncLink | None:
    return db.get(SyncLink, _LINK_ID)


def _normal_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def console_url(db: Session, requested: str = "") -> str:
    """Where to sign in. An env-baked `SYNC_CLOUD_URL` is read-only and wins (the wizard's
    step 1 shows it read-only); otherwise the URL the wizard sent, else the stored one."""
    link = _link(db)
    stored = (link.console_url or link.cloud_url) if link else ""
    return _normal_url(settings.sync_cloud_url or requested or stored)


def legacy_key_id(link: SyncLink | None) -> str | None:
    """The migrated key's PUBLIC prefix (`gb_sync_ab12`), never the secret. None when the
    link carries no legacy key, or the key has no prefix the console could match."""
    if link is None or not link.legacy_key:
        return None
    raw = secrets.decrypt(link.legacy_key) or ""
    for prefix in _KEY_PREFIXES:
        if raw.startswith(prefix) and len(raw) > len(prefix) + _PUBLIC_CHARS:
            return raw[: len(prefix) + _PUBLIC_CHARS]
    return None


def _post(url: str, body: dict) -> httpx.Response:
    """POST to the console. No answer (network error, timeout, 5xx) raises `_Unknown`."""
    try:
        resp = httpx.post(url, json=body, timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        raise _Unknown(f"could not reach {url.split('/api/')[0]}: {e.__class__.__name__}")
    if resp.status_code >= 500:
        raise _Unknown(f"the console answered {resp.status_code}")
    return resp


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise _Unknown(f"the console answered {resp.status_code} without JSON")
    if not isinstance(data, dict):
        raise _Unknown("the console answered with an unexpected shape")
    return data


def _detail(data: dict) -> str:
    detail = data.get("detail") or data.get("error") or data.get("reason") or ""
    return detail if isinstance(detail, str) else json.dumps(detail)


def start(db: Session, *, label: str = "", requested_console: str = "") -> dict:
    """Ask the console for a device code. Returns `started {user_code, verification_url,
    expires_at, interval}`, `refused {reason}` (the console said no to the request) or
    `unknown {reason}`. Stores only the sign-in in flight; the link itself is untouched."""
    base = console_url(db, requested_console)
    if not base:
        raise NoConsole("no console URL: enter the console to sign in to")
    link = _link(db)
    body = {"label": (label or "").strip() or "Graphban box", "host": platform.node() or ""}
    key_id = legacy_key_id(link)
    if key_id:
        body["legacy_key_id"] = key_id
    try:
        resp = _post(f"{base}/api/sync/device/start", body)
        data = _json(resp)
        if resp.status_code >= 400:
            return {"status": "refused",
                    "reason": _detail(data) or f"the console answered {resp.status_code}"}
        code = data.get("device_code")
        if not code or not data.get("user_code"):
            raise _Unknown("the console answered without a device code")
    except _Unknown as e:
        return {"status": "unknown", "reason": str(e)}
    if link is None:
        link = SyncLink(id=_LINK_ID)
        db.add(link)
    link.pending_device_enc = secrets.encrypt(json.dumps(
        {"console_url": base, "device_code": code, "expires_at": data.get("expires_at")}))
    db.commit()
    return {"status": "started", "console_url": base, "user_code": data["user_code"],
            "verification_url": data.get("verification_url") or f"{base}/link",
            "expires_at": data.get("expires_at"), "interval": data.get("interval")}


def _pending(link: SyncLink | None) -> dict | None:
    if link is None or not link.pending_device_enc:
        return None
    try:
        pending = json.loads(secrets.decrypt(link.pending_device_enc) or "")
    except ValueError:
        return None
    return pending if isinstance(pending, dict) and pending.get("device_code") else None


def _ask(pending: dict) -> dict:
    """One poll of the console, mapped to exactly one of the four answers."""
    resp = _post(f"{pending['console_url']}/api/sync/device/poll",
                 {"device_code": pending["device_code"]})
    if resp.status_code == 404:
        # The console does not know the code (contract: unknown device_code is 404). It can
        # never be approved, which is a definite no: get a new code.
        return {"status": "refused", "reason": "expired"}
    if resp.status_code >= 400:
        raise _Unknown(f"the console answered {resp.status_code}")
    data = _json(resp)
    status = data.get("status")
    if status == "pending":
        return {"status": "pending"}
    if status == "refused":
        reason = data.get("reason")
        if reason in REFUSED_REASONS:
            return {"status": "refused", "reason": reason}
        raise _Unknown(f"the console refused with a reason this box does not know: {reason!r}")
    if status == "approved":
        if not data.get("token"):
            raise _Unknown("the console says approved but sent no token (it is sent once); "
                           "get a new code")
        org = data.get("org")
        org_name = (org.get("name") if isinstance(org, dict) else org) or ""
        return {"status": "approved", "org": str(org_name), "email": str(data.get("email") or ""),
                "token": data["token"]}
    raise _Unknown(f"the console answered an unknown status: {status!r}")


def poll(db: Session) -> dict:
    """Poll the sign-in in flight. Only `approved` writes: it stores the token (encrypted),
    `org` and `linked_email`, points the link at that console, and clears `legacy_key` (the
    console revoked the key in the same transaction that approved the code, L6). Mappings
    are kept, so a takeover by another member re-checks them on the next push (L5)."""
    link = _link(db)
    pending = _pending(link)
    if pending is None:
        raise NoSignIn("no sign-in in progress: start one with POST /api/sync/link/start")
    try:
        answer = _ask(pending)
    except _Unknown as e:
        return {"status": "unknown", "reason": str(e)}
    if answer["status"] != "approved":
        return answer
    link.token_enc = secrets.encrypt(answer["token"])
    link.org = answer["org"]
    link.linked_email = answer["email"]
    link.console_url = link.cloud_url = pending["console_url"]
    link.legacy_key = ""
    link.pending_device_enc = ""
    link.linked_at = utcnow()
    db.commit()
    return {"status": "approved", "org": answer["org"], "email": answer["email"]}


def clear(db: Session) -> None:
    """Unlink's half of the deployment link: delete the token, the sign-in in flight, the
    legacy key and every mapping, so each project is local only again."""
    link = _link(db)
    if link is not None:
        link.token_enc = link.pending_device_enc = link.legacy_key = ""
        link.linked_email = link.console_url = ""
    db.execute(delete(SyncProject))
    db.commit()


def mappings(db: Session) -> dict[str, SyncProject]:
    return {m.local_project_id: m for m in db.scalars(select(SyncProject))}


def link_view(db: Session, maps: dict[str, SyncProject] | None = None) -> dict:
    """The deployment link for `GET /api/sync/status`, never the token or the key.

    `state` is the link's own answer: `refused` when the latest push was refused for the
    credential itself (seat removed, token revoked), `unknown` when it got no answer, `ok`
    otherwise. A refusal of ONE project is that project's result, not the link's (L5).
    `state` is None when the box holds no credential to answer for."""
    link = _link(db)
    maps = mappings(db) if maps is None else maps
    signed_in = bool(link and link.token_enc)
    legacy = bool(link and link.legacy_key)
    has_key = bool(link and link.cloud_url and link.api_key_enc)
    state = None
    if signed_in or legacy or has_key:
        pushed = [m for m in maps.values() if m.last_push_at and m.last_push_result]
        latest = max(pushed, key=lambda m: m.last_push_at, default=None)
        state = "ok"
        if latest is not None and latest.last_push_result == "unknown":
            state = "unknown"
        elif latest is not None and latest.last_push_result == "refused" and (
                latest.last_push_reason in LINK_REFUSALS):
            state = "refused"
    return {"linked_email": link.linked_email if link else "", "signed_in": signed_in,
            "legacy": legacy, "state": state,
            "sign_in_pending": bool(link and link.pending_device_enc)}


def project_view(mapping: SyncProject | None) -> dict:
    """One local project's mapping and last push result; unmapped is local only."""
    if mapping is None:
        return {"mapped": False, "cloud_project_id": None, "last_push_at": None,
                "last_push_result": None, "last_push_reason": None}
    return {"mapped": True, "cloud_project_id": mapping.cloud_project_id,
            "last_push_at": mapping.last_push_at, "last_push_result": mapping.last_push_result,
            "last_push_reason": mapping.last_push_reason}
