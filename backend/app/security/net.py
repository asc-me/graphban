"""Request network metadata — shared client-IP resolution for rate limiting."""
from __future__ import annotations

import ipaddress
import logging

from starlette.requests import Request

from app.config import settings

logger = logging.getLogger("app.security")

#: Warned once per process, not per request. The condition is true on every request once it
#: is true at all, and a per-request log would bury the thing it is trying to surface.
#:
#: Keyed by MESSAGE rather than a single boolean, so a second distinct misconfiguration is
#: never silenced by the first having fired.
#:
#: **Today that is not observable, and saying so is more useful than implying otherwise.** The
#: three warnings below are mutually exclusive by configuration — each is reachable only for
#: one setting of `trusted_hops`/`trusted_proxy`, and those do not change under a running
#: process — so a single flag would behave identically. A sabotage run confirmed it: replacing
#: this dict with one boolean passes the whole suite. What is tested is the helper's own
#: contract (`test_two_different_warnings_are_both_emitted`), because the next warning added
#: here is the one that would be swallowed, and it would be swallowed silently.
_warned: set[str] = set()


def _warn_once(msg: str, *args) -> None:
    if msg in _warned:
        return
    _warned.add(msg)
    logger.warning(msg, *args)


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


def _hop_from_the_right(xff: str, hops: int) -> str | None:
    """The address the OUTERMOST TRUSTED proxy observed, counting from the right.

    This is the whole fix, and the direction is the point. `X-Forwarded-For` grows
    left-to-right: each proxy appends the peer it saw. So the RIGHTMOST entries were written
    by infrastructure and the leftmost by whoever spoke first — which, if a client sent its
    own header, is the client. Reading `xff[0]` reads the one value an attacker fully
    controls; reading from the right reads only what a proxy actually observed.

    `hops` is how many proxies stand between this app and the internet:

    - **1** (compose): client -> nginx -> app. nginx appended the socket peer it saw, which
      IS the client, so `xff[-1]` is the answer and any forged prefix is ignored.
    - **2** (Railway): client -> edge -> nginx -> app. `xff[-1]` is the edge; the edge wrote
      the client at `xff[-2]`.

    Returns None when the header is SHORTER than the configured chain, which means the
    request did not arrive the way the operator said it would — through a bypass, or with the
    count set too high. Trusting a short header there would let anyone who reaches the app
    directly pick their own bucket, so this fails closed and the caller falls back to the
    socket peer.
    """
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if len(parts) < hops:
        return None
    candidate = parts[-hops]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        # Not an address. Never a bucket key: an unvalidated one is attacker-chosen and
        # unbounded, so a few thousand junk values are a few thousand buckets.
        return None
    return candidate


def client_ip(request: Request) -> str:
    """The caller's IP for a rate-limit bucket.

    Behind a proxy the socket peer is the proxy, so every client would share one bucket.
    Which forwarded address to believe is the entire question, and both obvious answers are
    wrong (GRPH-553):

    - `TRUSTED_PROXY=true` on the compose stack reads `xff[0]`, and
      `web/nginx.conf.template` sets the header with `$proxy_add_x_forwarded_for`, which
      APPENDS to whatever the client sent. A browser sending its own `X-Forwarded-For` then
      picks its own bucket — worse than sharing one.
    - Making nginx authoritative with `X-Forwarded-For $remote_addr` fixes compose and breaks
      Railway, where the same template runs behind an edge and `$remote_addr` IS the edge, so
      overwriting collapses every hosted caller into one bucket.

    So nginx is left alone — one template, appending everywhere — and the app counts hops from
    the right. `TRUSTED_HOPS` is the topology; see `_hop_from_the_right`.
    """
    peer_now = request.client.host if request.client else "unknown"
    if settings.trusted_hops > 0:
        xff = request.headers.get("x-forwarded-for")
        # The header is believed ONLY when the request actually arrived from the proxy — that
        # is, when the socket peer is an address on this deployment's own network.
        #
        # Hop counting alone does not close the direct hit. With `TRUSTED_HOPS=1`, a caller
        # reaching the app port itself and sending one entry is indistinguishable from nginx
        # having appended one, so it picks its own bucket. That is not hypothetical here:
        # GRPH-478 says the API is publicly reachable bypassing nginx. Checking the peer is
        # what makes the count mean "hops through infrastructure I control" rather than
        # "entries in a header anyone can write".
        found = (_hop_from_the_right(xff, settings.trusted_hops)
                 if xff and _is_private_peer(peer_now) else None)
        if found:
            return found
        _warn_once(
            "TRUSTED_HOPS=%d but this request's X-Forwarded-For was %r — too short, absent "
            "or not an address. Bucketing on the socket peer instead, which is correct but "
            "shared. Either the request bypassed the proxy chain, or the hop count is wrong: "
            "it is the number of proxies in front of this app (1 for the compose stack, 2 "
            "behind an edge that already sets the header).",
            settings.trusted_hops, request.headers.get("x-forwarded-for"),
        )
    elif settings.trusted_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            _warn_once(
                "TRUSTED_PROXY is on and TRUSTED_HOPS is not set, so the bucket key is the "
                "FIRST X-Forwarded-For hop. That value is written by whoever spoke first, so "
                "if the proxy in front APPENDS rather than overwrites — which the bundled "
                "nginx does — a caller can choose its own bucket. Set TRUSTED_HOPS instead "
                "(1 for the compose stack, 2 behind an edge). See docs/configuration.md."
            )
            return xff.split(",")[0].strip()

    peer = request.client.host if request.client else "unknown"
    if not settings.trusted_proxy and settings.trusted_hops == 0 and _is_private_peer(peer):
        _warn_once(
            "Rate limits are bucketing on %s, which is a private address — this request "
            "reached the app through a proxy and neither TRUSTED_HOPS nor TRUSTED_PROXY is "
            "set, so EVERY caller shares one bucket. Set TRUSTED_HOPS to the number of "
            "proxies in front of this app: 1 for the compose stack, 2 behind an edge that "
            "already sets X-Forwarded-For. See docs/deploy.md.", peer,
        )
    return peer
