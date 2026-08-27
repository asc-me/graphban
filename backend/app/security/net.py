"""Request network metadata — shared client-IP resolution for rate limiting."""
from __future__ import annotations

import ipaddress
import logging

from starlette.requests import Request

from app.config import settings

logger = logging.getLogger("app.security")

#: Warned once per process, not per request. The condition is true on every request once it
#: is true at all, and a per-request log would bury the thing it is trying to surface.
_warned_shared_bucket = False


def _is_private_peer(host: str) -> bool:
    """Is the socket peer a private address — i.e. something on this deployment's own
    network rather than a caller off the internet?

    A container-network address (`172.20.0.4`), a loopback, or a link-local peer means the
    request reached the app through something else. Anything unparseable is not treated as
    private: guessing here would warn on deployments that are fine, and a warning that cries
    wolf is one operators learn to scroll past.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def client_ip(request: Request) -> str:
    """The caller's IP for a rate-limit bucket. Behind a proxy/LB the socket peer is
    the proxy, so every client would share one bucket; honor the first
    X-Forwarded-For hop ONLY when the operator asserts a trusted proxy sits in front
    (otherwise the header is client-spoofable).

    **The warning is the point of GRPH-439.** `TRUSTED_PROXY` was reasoned about correctly
    for Railway and never carried to the compose stack, which has the identical topology —
    nginx in front of uvicorn — so on the self-host every request through port 8080 shared
    one bucket. The failure is quiet by construction: this function fails soft, so a
    misconfigured deployment behaves exactly like a working one with unusually strict limits.

    `startup.py` already warns about this, and could not have caught it: that check is gated
    on `hosted_mode`, which the self-host does not set. The condition that actually
    distinguishes the two is visible only per request — the socket peer being an address on
    the deployment's own network — so it is checked here, once.
    """
    if settings.trusted_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    peer = request.client.host if request.client else "unknown"
    global _warned_shared_bucket
    if not _warned_shared_bucket and not settings.trusted_proxy and _is_private_peer(peer):
        _warned_shared_bucket = True
        logger.warning(
            "Rate limits are bucketing on %s, which is a private address — this request "
            "reached the app through a proxy and TRUSTED_PROXY is off, so EVERY caller "
            "shares one bucket. Set TRUSTED_PROXY=true if, and only if, a trusted proxy "
            "terminates every request and overwrites X-Forwarded-For; if it appends to a "
            "client-supplied header instead, turning this on makes the bucket key "
            "attacker-controlled. See docs/deploy.md.", peer,
        )
    return peer
