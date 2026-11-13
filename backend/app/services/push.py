"""Browser push for Response Needed alerts (GRPH-1528, P75 D4).

Standard Web Push with VAPID keys: from the environment when supplied, else the pair the
server generated and stored on first start (GRPH-1582). Best-effort by construction: nothing here
raises into the hand-off, and the in-app alert exists whether or not any push arrives.
"""
from __future__ import annotations

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


_DEFAULT_SUBJECT = "mailto:admin@localhost"
_ROW_ID = "vapid"
_stored: tuple[str, str] | None = None  # per-process cache of the stored pair


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _generate() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    return _b64(pub), _b64(key.private_numbers().private_value.to_bytes(32, "big"))


def _read(db: Session) -> tuple[str, str] | None:
    row = db.get(VapidKey, _ROW_ID)
    if row is None:
        return None
    priv = decrypt(row.private_key)
    return (row.public_key, priv) if priv else None


def ensure_keys(db: Session) -> None:
    """Startup step: resolve the VAPID pair. Env keys win and the table is untouched; else
    reuse the stored row, else generate and insert it (race-safe: on conflict re-read the
    winner). Never raises; a failure leaves push unconfigured."""
    global _stored
    _stored = None
    if settings.vapid_public_key and settings.vapid_private_key:
        return
    try:
        pair = _read(db)
        if pair is None:
            pub, priv = _generate()
            db.add(VapidKey(id=_ROW_ID, public_key=pub, private_key=encrypt(priv)))
            try:
                db.commit()
                pair = (pub, priv)
            except IntegrityError:
                db.rollback()
                pair = _read(db)
        _stored = pair
    except Exception:  # noqa: BLE001 - push stays off; the api still starts
        log.exception("push: could not ensure the VAPID key pair")
        db.rollback()
        _stored = None


def _resolved() -> tuple[str, str]:
    if settings.vapid_public_key and settings.vapid_private_key:
        return settings.vapid_public_key, settings.vapid_private_key
    return _stored or ("", "")


def configured() -> bool:
    return bool(all(_resolved()))


def status() -> dict:
    return {"configured": configured(), "public_key": _resolved()[0] or None}


def _subject(db: Session) -> str:
    if settings.vapid_subject != _DEFAULT_SUBJECT:
        return settings.vapid_subject
    email = db.scalar(select(User.email).order_by(User.created_at, User.id).limit(1))
    return f"mailto:{email}" if email else settings.vapid_subject


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
    if not alert_ids or not configured():
        return 0
    sent = 0
    try:
        for alert in db.scalars(select(Alert).where(Alert.id.in_(alert_ids))).all():
            if alert.project_id not in readable_project_ids(db, alert.user_id):
                continue  # D6: the user lost read access since the alert was written
            subs = db.scalars(select(PushSubscription).where(
                PushSubscription.user_id == alert.user_id)).all()
            if not subs:
                continue
            data = _payload(db, alert)
            for sub in subs:
                sent += _send_one(db, sub, data)
    except Exception:  # noqa: BLE001 - a push problem never fails the hand-off
        log.exception("push: sending failed")
        db.rollback()
    return sent


def _send_one(db: Session, sub: PushSubscription, data: str) -> int:
    try:
        private_key = _resolved()[1]
        subject = _subject(db)
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
