"""Rate limiting with a pluggable backend (Phase 5).

One front door — :func:`allow` — used by the login guard, the public feedback
endpoints, and the hosted per-org agent-call cap. Two backends:

- **In-process** (default): the existing sliding-window limiter in ``services.spam``.
  Correct for self-host, a single container, and tests; state is per-process, so caps
  are per-instance.
- **Redis** (``REDIS_URL`` set): a shared fixed-window counter, so a cap holds across
  every replica. Falls open to the in-process check if Redis is unreachable — a rate
  limiter outage must not take the app down.

All current limits are per-minute, matching the in-process window.
"""
from __future__ import annotations

import logging
import time

from app.config import settings
from app.services import spam

logger = logging.getLogger("graphban.ratelimit")

_client = None
_client_resolved = False


def _redis():
    """Lazily build the Redis client (only when REDIS_URL is set). Import is lazy so
    the dependency is never touched on self-host / in tests."""
    global _client, _client_resolved
    if _client_resolved:
        return _client
    _client_resolved = True
    if settings.redis_url:
        import redis  # lazy: optional dependency

        _client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _redis_allow(client, key: str, limit: int, window: int) -> bool:
    bucket = int(time.time() // window)
    rkey = f"rl:{key}:{bucket}"
    pipe = client.pipeline()
    pipe.incr(rkey)
    pipe.expire(rkey, window)
    count, _ = pipe.execute()
    return int(count) <= max(1, limit)


def allow(key: str, limit: int, window: int = 60) -> bool:
    """True if this call is within ``limit`` for ``key`` in the current window."""
    client = _redis()
    if client is not None:
        try:
            return _redis_allow(client, key, limit, window)
        except Exception:  # noqa: BLE001 — never let a limiter outage block traffic
            logger.exception("redis rate-limit check failed; falling back to in-process")
    return spam.check_rate(key, limit)


#: Handle availability lookups allowed per signed-in user per minute (GRPH-1788). A probe
#: for who holds what is an enumeration tool, so it is capped per user, not per IP.
HANDLE_CHECK_PER_MIN = 30


def allow_handle_check(user_id: str) -> bool:
    return allow(f"handle-check:{user_id}", HANDLE_CHECK_PER_MIN)
