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
import uuid
from datetime import timedelta, timezone

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
) -> tuple[ApiKey, str]:
    """Create a key row and return (row, plaintext). Plaintext is not persisted.

    `project_id` scopes the key to one project (agent writes target it by default);
    None makes a global key. `expires_in_days` sets an optional lifetime; None =
    non-expiring.
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
    )
    db.add(row)
    db.commit()
    db.refresh(row)
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
    row.last_used = utcnow()
    db.commit()
    return row
