"""A way back into an account whose password is forgotten (GRPH-359).

`POST /api/auth/me/password` covers changing a password you KNOW. Until this, a forgotten one
was permanent: `password_hash` was written at account creation and changeable only by someone
who could already authenticate. On a hosted product onboarding paying orgs, the first support
ticket is a customer who cannot get back in and an operator with no way to help.

Four properties, each of which is a way this feature is worse than not having it if missed:

**The response never distinguishes a registered address from an unregistered one.** Otherwise
the endpoint is an account-enumeration oracle — anyone can ask it whether you have an account
here. So `request_reset` returns None either way and the route says the same sentence.

**The token is stored hashed.** It is a credential for an existing account, not an offer of
one, so a database read, a backup, or a log line holding this row must not yield a working
link. Only the email carries the plaintext.

**Single use, and consumed on SUCCESS.** A link that still works after it has been used is a
credential sitting in an inbox forever. Marked on success rather than on first sight, so a
request that fails validation does not burn the link the user is holding.

**Every prior session dies.** Same `token_version` machinery the change-password path uses,
and for a stronger reason: a reset usually means the account was compromised or the mailbox
was, and leaving issued tokens valid keeps whoever has them signed in.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PasswordReset, User, utcnow
from app.security.passwords import hash_password
from app.services.email import send_email

#: How long a link lives. Short because it is a bearer credential in a mailbox, long enough to
#: survive a user reading mail on another device. The invite TTL is measured in days for the
#: opposite reason — an invite is an offer, and nobody is locked out while it sits unread.
RESET_TTL_MINUTES = 30


class InvalidReset(Exception):
    """The token does not resolve to a live reset. Deliberately one exception for expired,
    already-used, and never-existed: telling them apart tells an attacker holding a stolen
    link which of those it is, and tells a legitimate user nothing they can act on beyond
    "ask for another"."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt):
    """SQLite hands back a naive datetime for a `DateTime(timezone=True)` column, so an
    expiry read from the database cannot be compared with an aware `utcnow()` without this.

    The fifth copy of this idiom in the codebase — `services/orgs.py:477`,
    `security/apikey.py:91`, `services/galaxy.py:356` and `services/memory.py:52` all do it
    inline, and `services/fleet.py` has an `_aware` of its own. Written locally rather than
    imported from `fleet`, which would couple this to a module it has nothing to do with;
    consolidating all six is worth doing and is not this change.
    """
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def request_reset(db: Session, *, email: str, base_url: str, ip: str = "") -> str | None:
    """Mint a link and email it, or do nothing. Returns the token FOR TESTS ONLY.

    Returning the token is a deliberate compromise and worth naming: the alternative is a test
    that reaches into the outbox and parses a URL out of prose, which then fails whenever the
    wording changes and tests the copy rather than the flow. The ROUTE ignores this value — a
    test asserts that the response body cannot be used to reset anything.
    """
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if user is None:
        # No row, no mail, no tell. The caller returns the same sentence either way.
        return None

    # Any earlier outstanding link for this user is retired. Two live links means a stale one
    # in an older mail still works after the user has used the newest — which is the
    # single-use property defeated by a second request rather than by a second use.
    for stale in db.scalars(
        select(PasswordReset).where(PasswordReset.user_id == user.id,
                                    PasswordReset.used_at.is_(None))
    ).all():
        stale.used_at = utcnow()

    token = secrets.token_urlsafe(32)
    db.add(PasswordReset(
        id=f"pwr_{uuid.uuid4().hex[:12]}",
        user_id=user.id,
        token_hash=_digest(token),
        expires_at=utcnow() + timedelta(minutes=RESET_TTL_MINUTES),
        requested_ip=ip,
    ))
    db.commit()

    link = f"{base_url.rstrip('/')}/reset-password?token={token}"
    send_email(
        user.email,
        "Reset your Graphban password",
        f"Someone asked to reset the password for this account.\n\n{link}\n\n"
        f"The link works once and expires in {RESET_TTL_MINUTES} minutes. If this was not "
        "you, nothing has changed and you can ignore this message.",
    )
    return token


def consume_reset(db: Session, *, token: str, new_password: str) -> User:
    """Set the password and retire the link, or raise `InvalidReset`."""
    row = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == _digest(token)))
    if row is None or row.used_at is not None or _aware(row.expires_at) <= utcnow():
        raise InvalidReset("this reset link is no longer valid")

    user = db.get(User, row.user_id)
    if user is None:                      # the account went away between mint and use
        raise InvalidReset("this reset link is no longer valid")

    user.password_hash = hash_password(new_password)
    # AL-59 revocation. The reason is stronger here than on the change path: a reset usually
    # means the password or the mailbox was not private, so previously-issued tokens are
    # exactly what must stop working.
    user.token_version += 1
    row.used_at = utcnow()
    db.commit()
    db.refresh(user)
    return user
