"""Scoped API keys for agent / MCP authentication.

Keys look like `<prefix>_sk_<40 hex>`. Only the SHA-256 hash is stored; the plaintext is
shown to the user exactly once at creation.

The product rename (AgentLedger → Graphban) moves the prefix, so verification accepts
**every prefix this product has ever minted** while creation uses exactly one. A key is
matched by the hash of its full plaintext, so the prefix is only a routing hint — but the
`startswith` gate is what stops an unrelated bearer token from being hashed and looked up,
so it has to know the full set.

Order matters for the cut-over: this accept-both change must be deployed to every
instance BEFORE `MINT_PREFIX` moves, or a credential minted by one instance is rejected
by another. The self-host and the hosted tenant sync to each other with exactly such a
key (AL-262 / AL-263).
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from datetime import timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import ApiKey, utcnow

# Every prefix this product has ever issued. NOTHING is ever removed from this tuple:
# keys are long-lived, stored only as a hash, and cannot be rewritten in place.
ACCEPTED_PREFIXES = ("gb_sk_", "al_sk_")

# The one used for new keys. Moved to gb_sk_ in AL-263, once AL-262 (accept-both) was
# confirmed live on both the self-host and the hosted tenant. Existing al_sk_ keys keep
# working forever — only the hash is stored, so nothing needs re-issuing.
MINT_PREFIX = "gb_sk_"

# Back-compat alias: `KEY_PREFIX` was the single source of truth before the rename.
KEY_PREFIX = MINT_PREFIX


def is_api_key(raw: str) -> bool:
    """Does this look like one of our keys? Used by the Bearer sniff in security/deps."""
    return bool(raw) and raw.startswith(ACCEPTED_PREFIXES)


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_api_key(
    db: Session,
    user_id: str,
    name: str,
    scopes: list[str] | None = None,
    project_id: str | None = None,
    expires_in_days: int | None = None,
    tool_tiers: list[str] | None = None,
    origin: str | None = None,
    roles: list[str] | None = None,
    posture: str | None = None,
    commit: bool = True,
) -> tuple[ApiKey, str]:
    """Create a key row and return (row, plaintext). Plaintext is not persisted.

    `project_id` scopes the key to one project (agent writes target it by default);
    None makes a global key. `expires_in_days` sets an optional lifetime; None =
    non-expiring. `origin` records which surface minted it (NULL = unknown). `commit=False`
    flushes instead, so a caller (the rotate route) can finish a larger transaction.
    """
    raw = MINT_PREFIX + secrets.token_hex(20)
    row = ApiKey(
        id=str(uuid.uuid4()),
        user_id=user_id,
        project_id=project_id,
        name=name,
        prefix=raw[: len(MINT_PREFIX) + 4],  # display fragment, e.g. gb_sk_ab12
        hashed_key=_hash_key(raw),
        scopes=scopes or ["read", "write"],
        # `or None` rather than `or []`: an empty list and NULL both mean core-only, and
        # storing one shape keeps a hand-minted key indistinguishable from a migrated one.
        tool_tiers=list(tool_tiers) if tool_tiers else None,
        expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None,
        origin=origin,
        posture=posture,
    )
    if roles is not None:
        row.roles = list(roles)
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row, raw


def verify_api_key(db: Session, raw: str) -> ApiKey | None:
    if not is_api_key(raw):
        return None
    row = db.query(ApiKey).filter(ApiKey.hashed_key == _hash_key(raw)).one_or_none()
    if row is None:
        return None
    # Lifecycle gate (AL-72): a revoked or expired key authenticates no one.
    if row.revoked:
        return None
    if row.expires_at is not None:
        expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= utcnow():
            return None
    _touch_last_used(db, row)
    return row


# `last_used` is the first-call proof for quick-start step ③ (GRPH-1362, G13), so it has two
# rules: the FIRST authenticated call stamps it immediately (a person is watching for it), and
# every later stamp is throttled to once a minute per key from this in-process cache. Both
# writes go through their own short session, never the request's transaction: the old
# `db.commit()` here committed whatever the caller had pending and put a write on every call.
LAST_USED_THROTTLE_SECONDS = 60
_last_stamped: dict[str, float] = {}
_stamp_lock = threading.Lock()


def _touch_last_used(db: Session, row: ApiKey) -> None:
    now = time.monotonic()
    first = row.last_used is None
    with _stamp_lock:
        prev = _last_stamped.get(row.id)
        if not first and prev is not None and now - prev < LAST_USED_THROTTLE_SECONDS:
            return
        _last_stamped[row.id] = now
    try:
        with Session(bind=db.get_bind()) as side:
            side.execute(update(ApiKey).where(ApiKey.id == row.id).values(last_used=utcnow()))
            side.commit()
    except Exception:  # noqa: BLE001 — a failed stamp must never fail the call it describes
        with _stamp_lock:
            _last_stamped.pop(row.id, None)
