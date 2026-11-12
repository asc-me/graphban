"""Browser push for Response Needed alerts (GRPH-1528, P75 D4).

Standard Web Push with VAPID keys from settings. Best-effort by construction: nothing here
raises into the hand-off, and the in-app alert exists whether or not any push arrives.
"""
from __future__ import annotations

import json
import logging
import uuid

from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Alert, Item, PushSubscription, utcnow

log = logging.getLogger(__name__)


def configured() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def status() -> dict:
    return {"configured": configured(), "public_key": settings.vapid_public_key or None}


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
                       "url": f"/triage?item={key}", "alert": alert.id})


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
        webpush(subscription_info={"endpoint": sub.endpoint,
                                   "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=data, vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject}, timeout=10)
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
