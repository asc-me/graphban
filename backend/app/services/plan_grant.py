"""Plan grants on the box (GRPH-1343 / PRD-P62 D8).

A self-hosted box must not read its capacity plan from `org.plan`: that column is the operator's
to edit. It reads a **plan grant** instead, a document the control plane signs.

**Format** (shared with graphban-control). `doc` is canonical JSON
`{org_id, deployment_id, plan, issued_at, expires_at}` (the times are unix seconds) and `sig` is a
raw 64-byte Ed25519 signature over `doc`'s exact bytes, checked against
`TRUSTED_PLAN_GRANT_KEYS` (raw public keys, hex, compiled into the release; the private half
never enters this repo). With no key configured every grant is refused.

**Verify on change, not per call.** The signature is checked at startup (`load`) and on `install`.
The verdict and `expires_at` stay in memory; `current` only compares `expires_at` with the clock.
The verdict is per process, so a grant installed on one worker reaches the others at their next
start.

**This box's org** is the cloud org the box is linked to (`RuleDecisionPull.org_id`), or, before it
is linked, the only local org when there is exactly one.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import InstalledPlanGrant, Organization, RoutingDecision, RuleDecisionPull, User

logger = logging.getLogger(__name__)

#: Raw Ed25519 public keys (hex) whose grants are trusted. Empty until the control plane's key
#: exists: an empty tuple refuses every grant (fail closed). Never put a placeholder here.
TRUSTED_PLAN_GRANT_KEYS: tuple[str, ...] = ()

REASONS = ("missing", "bad_signature", "unknown_key", "wrong_org", "expired",
           "older_than_installed")
EXPIRED_REASON = "plan grant expired"

#: In memory: the verdict of the installed grant, set by `load` / `install`.
_state: dict = {"reason": "missing", "plan": None, "issued_at": None, "expires_at": None}
_last_logged: str | None = None

#: The last attempt to fetch a grant from the control plane (GRPH-1344). A failed fetch only ever
#: writes here: the installed grant stays in force until its own `expires_at`.
NOT_ATTACHED = "not attached"
_FETCH_TIMEOUT = 15.0
_RETRY_SECONDS = 3600  # while no valid grant is installed, try at least hourly
_last_fetch: dict = {"at": None, "ok": None, "reason": NOT_ATTACHED}
_last_fetch_logged: str | None = None


def box_org_id(db: Session) -> str | None:
    pull = db.get(RuleDecisionPull, "instance")
    if pull is not None and pull.org_id:
        return pull.org_id
    ids = db.scalars(select(Organization.id).limit(2)).all()
    return ids[0] if len(ids) == 1 else None


def verify(db: Session, doc: bytes, sig: bytes, *, now: float | None = None,
           trusted_keys: tuple[str, ...] | None = None) -> dict:
    """Check, in order: the signature, the org, the expiry. Returns
    `{ok: True, plan, issued_at, expires_at}` or `{ok: False, reason, ...}`."""
    keys = TRUSTED_PLAN_GRANT_KEYS if trusted_keys is None else trusted_keys
    if not keys:
        return {"ok": False, "reason": "unknown_key"}
    for k in keys:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(k)).verify(sig, doc)
            break
        except (InvalidSignature, ValueError):
            continue
    else:
        return {"ok": False, "reason": "bad_signature"}
    try:
        g = json.loads(doc)
        plan, org_id = str(g["plan"]), str(g["org_id"])
        issued_at, expires_at = int(g["issued_at"]), int(g["expires_at"])
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "reason": "bad_signature"}
    out = {"plan": plan, "issued_at": issued_at, "expires_at": expires_at}
    if org_id != box_org_id(db):
        return {"ok": False, "reason": "wrong_org", **out}
    if (time.time() if now is None else now) >= expires_at:
        return {"ok": False, "reason": "expired", **out}
    return {"ok": True, **out}


def _set(verdict: dict, db: Session) -> None:
    """Swap the in-memory verdict. A valid→invalid change ends live reservations."""
    global _last_logged
    was_ok = _state.get("reason") is None
    _state.update(reason=None if verdict["ok"] else verdict["reason"],
                  plan=verdict.get("plan"), issued_at=verdict.get("issued_at"),
                  expires_at=verdict.get("expires_at"))
    if _state["reason"] != _last_logged:
        _last_logged = _state["reason"]
        logger.info("plan grant: %s", "valid" if verdict["ok"] else
                    f"not in force ({verdict['reason']})")
    if was_ok and not verdict["ok"]:
        _end_reservations(db)


def _end_reservations(db: Session) -> None:
    """Entitlement and ledger rows stay; only live reservations end."""
    now = datetime.now(timezone.utc)
    for r in db.scalars(select(RoutingDecision).where(
            RoutingDecision.kind == "reservation", RoutingDecision.ended_at.is_(None))).all():
        r.ended_at, r.ended_reason = now, EXPIRED_REASON
    db.commit()


def _newest(db: Session) -> InstalledPlanGrant | None:
    return db.scalars(select(InstalledPlanGrant).order_by(
        InstalledPlanGrant.issued_at.desc(), InstalledPlanGrant.id.desc()).limit(1)).first()


def load(db: Session) -> dict:
    """Startup: re-verify the persisted grant and keep the verdict. Never raises on a bad grant."""
    row = _newest(db)
    _set({"ok": False, "reason": "missing"} if row is None
         else verify(db, row.document, row.signature), db)
    return status()


def install(db: Session, user: User | None, doc: bytes, sig: bytes, *,
            now: float | None = None) -> dict:
    """Verify and persist a grant. Replaces the installed one only when `issued_at` is later."""
    v = verify(db, doc, sig, now=now)
    if not v["ok"]:
        return v
    cur = _newest(db)
    if cur is not None and v["issued_at"] <= cur.issued_at:
        return {**v, "ok": False, "reason": "older_than_installed"}
    g = json.loads(doc)
    db.add(InstalledPlanGrant(
        org_id=str(g["org_id"]), deployment_id=str(g.get("deployment_id", "")),
        plan=v["plan"], issued_at=v["issued_at"], expires_at=v["expires_at"],
        document=doc, signature=sig, installed_by=user.id if user else None))
    db.commit()
    _set(v, db)
    return v


def current(db: Session, *, now: float | None = None) -> dict:
    """The verdict in force. No signature work: it only compares `expires_at` with the clock."""
    if _state["reason"] is None and (time.time() if now is None else now) >= _state["expires_at"]:
        _set({"ok": False, "reason": "expired", "plan": _state["plan"],
              "issued_at": _state["issued_at"], "expires_at": _state["expires_at"]}, db)
    return status()


def status() -> dict:
    ok = _state["reason"] is None
    return {"ok": ok, "reason": _state["reason"], "plan": _state["plan"],
            "issued_at": _state["issued_at"], "expires_at": _state["expires_at"],
            "last_fetch": dict(_last_fetch)}


def _record_fetch(ok: bool, reason: str | None, now: float) -> None:
    """Keep `last_fetch` and log once per state change, not once per tick."""
    global _last_fetch_logged
    _last_fetch.update(at=int(now), ok=ok, reason=reason)
    key = "ok" if ok else str(reason)
    if key != _last_fetch_logged:
        _last_fetch_logged = key
        logger.info("plan grant fetch: %s", "ok" if ok else f"failed ({reason})")


def fetch_due(*, now: float | None = None) -> bool:
    """Whether the timer should fetch now: less than half the installed grant's lifetime
    remains, or (no valid grant) the last attempt was an hour ago or more."""
    t = time.time() if now is None else now
    if _state["reason"] is None:
        lifetime = _state["expires_at"] - _state["issued_at"]
        return _state["expires_at"] - t < lifetime / 2
    at = _last_fetch["at"]
    return at is None or t - at >= _RETRY_SECONDS


def fetch_once(db: Session, *, now: float | None = None) -> dict:
    """Ask the control plane for a grant with the box's link credential and install it through
    `install`. Never raises and never clears the installed grant. An unlinked box (and a hosted
    one, which reads `org.plan`) makes no request."""
    from app.services import code_sync

    t = time.time() if now is None else now
    creds = None if settings.hosted_mode else code_sync.cloud_credentials(db)
    if creds is None:
        _record_fetch(False, NOT_ATTACHED, t)
        return status()
    url, key = creds
    try:
        resp = httpx.post(f"{url.rstrip('/')}/api/sync/plan-grant",
                          headers={"X-API-Key": key}, timeout=_FETCH_TIMEOUT)
        if resp.status_code == 401:
            _record_fetch(False, "unauthorized", t)
            return status()
        resp.raise_for_status()
        body = resp.json()
        doc = base64.b64decode(body["grant_b64"], validate=True)
        sig = base64.b64decode(body["signature_b64"], validate=True)
    except httpx.HTTPStatusError as e:
        _record_fetch(False, f"http_{e.response.status_code}", t)
        return status()
    except httpx.HTTPError:
        _record_fetch(False, "unreachable", t)
        return status()
    except (ValueError, KeyError, TypeError):
        _record_fetch(False, "bad_response", t)
        return status()
    v = install(db, None, doc, sig, now=t)
    # A grant no newer than the one held is the control plane agreeing, not a failure.
    ok = v["ok"] or v.get("reason") == "older_than_installed"
    _record_fetch(ok, None if ok else v["reason"], t)
    return status()


def refresh_if_due(db: Session, *, now: float | None = None, startup: bool = False) -> bool:
    """One timer tick: fetch when due (always at startup). Returns whether it fetched."""
    if not startup and not fetch_due(now=now):
        return False
    fetch_once(db, now=now)
    return True
