"""Spam protection for the public feedback endpoints.

Three layers, cheapest first:
1. Honeypot — a hidden field real users never fill.
2. Per-(project, IP) sliding-window rate limit, with a per-project configurable cap.
3. Optional Cloudflare Turnstile — verified only when a project configures a secret
   (so the app stays fully offline by default).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque

_WINDOW = 60.0  # seconds
_hits: dict[str, deque] = defaultdict(deque)
_SWEEP_EVERY = 1000  # evict emptied keys periodically so _hits can't grow unbounded
_checks = 0


def _sweep_stale(now: float) -> None:
    """Drop keys whose window has fully expired (AL-44 — keys were never freed)."""
    for k in [k for k, q in _hits.items() if not q or now - q[-1] > _WINDOW]:
        del _hits[k]


def check_rate(key: str, limit: int) -> bool:
    """Return True if this call is under the limit for `key` in the last minute."""
    global _checks
    now = time.monotonic()
    _checks += 1
    if _checks % _SWEEP_EVERY == 0:
        _sweep_stale(now)
    q = _hits[key]
    while q and now - q[0] > _WINDOW:
        q.popleft()
    if len(q) >= max(1, limit):
        return False
    q.append(now)
    return True


def verify_turnstile(secret: str, token: str, remoteip: str | None = None) -> bool:
    """Verify a Turnstile token. No secret configured → not required (returns True)."""
    if not secret:
        return True
    if not token:
        return False
    data = {"secret": secret, "response": token}
    if remoteip:
        data["remoteip"] = remoteip
    try:
        req = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=urllib.parse.urlencode(data).encode(),
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — fixed trusted host  # nosec B310
            body = json.loads(resp.read())
        return bool(body.get("success"))
    except Exception:
        return False
