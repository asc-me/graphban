"""Device-code sign-in for the CLI (GRPH-1366).

Three steps, three actors. The CLI `start`s and gets a `device_code` (secret, kept by the CLI)
and a short `user_code` (shown to the human). A signed-in human `approve`s the user_code, which
binds the row to THEIR user id. The CLI `redeem`s with its device_code and receives that user —
the identity comes from the approving session, never from anything the CLI sent.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CliPair, User, utcnow

PAIR_TTL_MINUTES = 10
POLL_INTERVAL_SECONDS = 3
#: No 0/O/1/I/L — the code is read off a terminal and typed into a browser.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class InvalidPair(Exception):
    """Unknown, expired, or already-used. One exception so the three cannot be told apart."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_code(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


def _live(row: CliPair | None) -> CliPair:
    if row is None or row.used_at is not None or _aware(row.expires_at) <= utcnow():
        raise InvalidPair()
    return row


def start(db: Session) -> dict:
    now = utcnow()
    live_codes = {
        c for c in db.scalars(select(CliPair.user_code).where(CliPair.used_at.is_(None),
                                                              CliPair.expires_at > now)).all()
    }
    while True:
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        if raw not in live_codes:
            break
    device_code = secrets.token_urlsafe(32)
    db.add(CliPair(id="clp_" + uuid.uuid4().hex[:12], device_hash=_digest(device_code),
                   user_code=raw, expires_at=now + timedelta(minutes=PAIR_TTL_MINUTES)))
    db.commit()
    return {"device_code": device_code, "user_code": f"{raw[:4]}-{raw[4:]}",
            "expires_in": PAIR_TTL_MINUTES * 60, "interval": POLL_INTERVAL_SECONDS}


def approve(db: Session, *, user_code: str, user: User) -> None:
    """Bind a live code to `user`. Re-approving an already-approved code is refused, so a second
    person who learns the code cannot swap the identity the first one granted."""
    code = normalize_code(user_code)
    row = db.scalar(select(CliPair).where(CliPair.user_code == code,
                                          CliPair.used_at.is_(None),
                                          CliPair.expires_at > utcnow()))
    row = _live(row)
    if row.approved_user_id is not None:
        raise InvalidPair()
    row.approved_user_id = user.id
    db.commit()


def redeem(db: Session, *, device_code: str) -> User | None:
    """The approving user (and the code is spent), or None while still pending."""
    row = _live(db.scalar(select(CliPair).where(CliPair.device_hash == _digest(device_code))))
    if row.approved_user_id is None:
        return None
    user = db.get(User, row.approved_user_id)
    if user is None:
        raise InvalidPair()
    row.used_at = utcnow()
    db.commit()
    return user
