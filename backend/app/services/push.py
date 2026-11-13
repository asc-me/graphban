"""Browser push for Response Needed alerts (GRPH-1528, P75 D4).

Standard Web Push with VAPID. The key pair is the one in the environment when both
`VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` are set, else the pair the server generated on its
first start and keeps in `vapid_keys` (GRPH-1582) — so push works with no operator setup.
Best-effort by construction: nothing here raises into the hand-off, and the in-app alert exists
whether or not any push arrives.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid

from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Alert, Item, Project, PushSubscription, User, VapidKey, utcnow
from app.security.authz import readable_project_ids
from app.security.secrets import decrypt, encrypt

log = logging.getLogger(__name__)


_ROW_ID = "vapid"
_DEFAULT_SUBJECT = "mailto:admin@localhost"

# This process's resolved stored pair (public, private), set by `ensure_keys` at startup. Every
# worker reads the same row, so every worker holds the same pair.
_stored: tuple[str, str] | None = None


def _env_pair() -> tuple[str, str] | None:
    if settings.vapid_public_key and settings.vapid_private_key:
        return settings.vapid_public_key, settings.vapid_private_key
    return None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _generate() -> tuple[str, str]:
    """A fresh P-256 pair: (uncompressed public point, raw 32-byte private scalar), base64url."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    return _b64url(public), _b64url(key.private_numbers().private_value.to_bytes(32, "big"))


def _read_stored(db: Session) -> tuple[str, str] | None:
    row = db.get(VapidKey, _ROW_ID)
    if row is None:
        return None
    private = decrypt(row.private_key)
    if not private:  # stored under a different SECRET_ENCRYPTION_KEY: push stays off, no rotation
        log.error("push: the stored VAPID private key cannot be decrypted; browser push is off")
        return None
    return row.public_key, private


def ensure_keys(db: Session) -> tuple[str, str] | None:
    """Startup step: resolve the VAPID pair this process signs with.

    Env keys win and the table is not touched. Otherwise the stored row is reused; with no row
    a pair is generated and inserted under the fixed id. Workers starting together race on that
    id: the loser rolls back and re-reads the winner, so all end up with the same pair. Never
    raises: a failure is logged and leaves push unconfigured. Never rotates a stored pair."""
    global _stored
    _stored = None
    env = _env_pair()
    if env is not None:
        return env
    try:
        pair = _read_stored(db)
        if pair is None and db.get(VapidKey, _ROW_ID) is None:
            public, private = _generate()
            db.add(VapidKey(id=_ROW_ID, public_key=public, private_key=encrypt(private)))
            try:
                db.commit()
                pair = (public, private)
            except IntegrityError:
                db.rollback()
                pair = _read_stored(db)
        _stored = pair
    except Exception:  # noqa: BLE001 - push stays off; the api still starts
        log.error("push: could not ensure the VAPID key pair; browser push is off", exc_info=True)
        db.rollback()
        _stored = None
    return _stored


def _resolved() -> tuple[str, str] | None:
    return _env_pair() or _stored


def configured() -> bool:
    return _resolved() is not None


def status() -> dict:
    pair = _resolved()
    return {"configured": pair is not None, "public_key": pair[0] if pair else None}


def _subject(db: Session) -> str:
    """`VAPID_SUBJECT` when it is set; else `mailto:` the earliest user's email."""
    if settings.vapid_subject and settings.vapid_subject != _DEFAULT_SUBJECT:
        return settings.vapid_subject
    email = db.scalar(select(User.email).where(User.email != "")
                      .order_by(User.created_at, User.id).limit(1))
    return f"mailto:{email}" if email else _DEFAULT_SUBJECT


def subscribe(db: Session, user_id: str, endpoint: str, p256dh: str, auth: str) -> PushSubscription:
    """Register (or refresh) this browser. The same endpoint is one row, whoever holds it."""
    row = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if row is None:
        row = PushSubscription(id="ps_" + uuid.uuid4().hex[:12], user_id=user_id, endpoint=endpoint)
        db.add(row)
    row.user_id, row.p256dh, row.auth, row.failed_at = user_id, p256dh, auth, None
    db.commit()
    return row


def unsubscribe(db: Session, user_id: str, endpoint: str) -> int:
    rows = db.scalars(select(PushSubscription).where(
        PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint)).all()
    for r in rows:
        db.delete(r)
    db.commit()
    return len(rows)


def _payload(db: Session, alert: Alert) -> str:
    wait = db.get(Item, alert.item_id)
    ticket = None
    if wait is not None:
        from app.services.alerts import _asker
        ticket = _asker(db, wait)
    key = (ticket.key if ticket is not None else None) or (wait.key if wait is not None else alert.item_id)
    first = (alert.title or "").strip().splitlines()[0] if (alert.title or "").strip() else key
    return json.dumps({"title": f"{key} needs a response", "body": first, "ticket": key,
                       "project": alert.project_id,
                       "url": _triage_url(db, alert.project_id, key), "alert": alert.id})


def _triage_url(db: Session, project_id: str, key: str) -> str:
    """The same Triage row the bell links to: hosted routes by project tag, self-host's flat
    `/triage` resolves the project itself."""
    from urllib.parse import quote

    base = "/triage"
    if settings.hosted_mode:
        project = db.get(Project, project_id)
        if project is not None and project.tag:
            base = f"/p/{project.tag}/triage"
    return f"{base}?item={quote(key)}"


def _status_of(exc: WebPushException) -> int | None:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


def send_for_alerts(db: Session, alert_ids: list[str]) -> int:
    """One push per subscription of each alert's user. Call AFTER the alert commit. Returns the
    number delivered. Never raises."""
    pair = _resolved()
    if not alert_ids or pair is None:
        return 0
    sent = 0
    try:
        private_key, subject = pair[1], _subject(db)
        for alert in db.scalars(select(Alert).where(Alert.id.in_(alert_ids))).all():
            if alert.project_id not in readable_project_ids(db, alert.user_id):
                continue  # D6: the user lost read access since the alert was written
            subs = db.scalars(select(PushSubscription).where(
                PushSubscription.user_id == alert.user_id)).all()
            if not subs:
                continue
            data = _payload(db, alert)
            for sub in subs:
                sent += _send_one(db, sub, data, private_key, subject)
    except Exception:  # noqa: BLE001 - a push problem never fails the hand-off
        log.exception("push: sending failed")
        db.rollback()
    return sent


def _send_one(db: Session, sub: PushSubscription, data: str, private_key: str,
              subject: str) -> int:
    try:
        webpush(subscription_info={"endpoint": sub.endpoint,
                                   "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=data, vapid_private_key=private_key,
                vapid_claims={"sub": subject}, timeout=10)
    except WebPushException as exc:
        if _status_of(exc) in (404, 410):
            db.delete(sub)
        else:
            sub.failed_at = utcnow()
        db.commit()
        return 0
    except Exception:  # noqa: BLE001 - network error, bad key: keep the row, mark it failed
        log.warning("push: delivery error", exc_info=True)
        sub.failed_at = utcnow()
        db.commit()
        return 0
    sub.last_ok_at = utcnow()
    db.commit()
    return 1
