from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

import jwt

from app.db import get_db
from app.models import ApiKey, User
from app.security.apikey import is_api_key, verify_api_key
from app.security.jwt import decode_token


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the logged-in user from a `Bearer <access-jwt>` header."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token, expected_type="access")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
    user = db.get(User, payload.get("sub"))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    # Revocation check: a token minted before the user's last logout/password-change
    # carries a stale `tv` and is rejected even though its signature is still valid (AL-59).
    if payload.get("tv", 0) != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked")
    return user


def get_agent_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ApiKey:
    """Resolve an agent API key from `X-API-Key` or `Authorization: Bearer <key>`."""
    raw = x_api_key
    if not raw and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization.split(" ", 1)[1]
        if is_api_key(candidate):  # any prefix we have ever minted
            raw = candidate
    key = verify_api_key(db, raw or "")
    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
    return key


def get_user_or_agent_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User | ApiKey:
    """Either credential, for a read that both a human and an agent legitimately make.

    Deliberately narrow. Most routes want one or the other and saying so is the point —
    a dependency that quietly accepts anything is how a surface widens without anybody
    deciding to widen it. This exists for `graph_health` (GRPH-405), whose every input an
    agent key can already read via `get_code_map` and `get_backlog`; the gate there
    withheld the convenience of the answer, not the answer.

    **`/api/fleet/presence` must not adopt this.** Presence names which HUMAN is editing
    which file — `user_id`, `user_initials`, `user_color` — and its inputs are NOT already
    agent-readable. That pair of facts is exactly what makes health different, and it is
    the whole argument for the gate staying where it is there.

    Tries the JWT first, because a browser session sends `Authorization: Bearer` too and a
    user who holds both should be resolved as themselves.
    """
    if authorization and authorization.lower().startswith("bearer "):
        candidate = authorization.split(" ", 1)[1]
        if not is_api_key(candidate):
            return get_current_user(authorization=authorization, db=db)
    return get_agent_key(authorization=authorization, x_api_key=x_api_key, db=db)
