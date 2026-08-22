"""GRPH-32's checklist, for the two items that were not actually in place.

The list is a pre-public hardening checklist for hosted mode. Auditing it against the tree
found three of five done — nginx carries HSTS, nosniff, Referrer-Policy and a
`frame-ancestors 'self' https:` CSP that still permits the roadmap embed; the public
surface is unprobeable (a project that has not opted in is 404, and hosted mode accepts
only the unguessable share token); API keys are hashed with revocation and expiry gates.

Two were not:

**`/api/public/roadmap` had no rate limit** — the one endpoint the checklist named by name.
Every other public route had one. It runs a full roadmap query per request and is reachable
by anyone with a share link.

**A rate limit that cannot tell callers apart.** `security.net.client_ip` honours
X-Forwarded-For only when `TRUSTED_PROXY` is set, and that setting defaults to False, is
read in exactly one place, and is tied to nothing. Behind a proxy with it off, the socket
peer is the proxy, so every caller shares one bucket: the endpoints read as protected while
the protection is aimed at the wrong thing. Warned rather than refused, because hosted mode
with no proxy is legitimate and forcing it on would let any caller forge their bucket key.
"""
from __future__ import annotations

import pytest

from app.config import settings

# Buckets are cleared between tests by conftest's autouse `_reset_rate_limit`, which
# clears `spam._hits`. Without that, the first test to exhaust a limit would make the
# others pass for its reason rather than their own.


def test_the_public_roadmap_is_rate_limited(client, auth, monkeypatch):
    """The endpoint GRPH-32 named. Unlimited, it is a full roadmap query per request for
    anyone holding a share link."""
    monkeypatch.setattr(settings, "trusted_proxy", False)

    seen = set()
    for _ in range(60):
        r = client.get("/api/public/roadmap")
        seen.add(r.status_code)
        if r.status_code == 429:
            break

    assert 429 in seen, (
        f"60 consecutive public roadmap reads never hit a limit (saw {sorted(seen)}); "
        "the endpoint the hardening checklist named is unbounded"
    )


def test_the_widget_config_is_rate_limited(client, auth):
    """Its neighbour, unlimited for the same reason and found in the same pass."""
    seen = set()
    for _ in range(60):
        r = client.get("/api/public/widget-config")
        seen.add(r.status_code)
        if r.status_code == 429:
            break

    assert 429 in seen, f"widget-config never rate limited (saw {sorted(seen)})"


def test_a_forwarded_ip_is_only_trusted_when_the_operator_says_so(monkeypatch):
    """The header is client-spoofable, so honouring it unconditionally would let any
    caller mint a fresh rate-limit bucket per request and opt out of limiting entirely."""
    from starlette.requests import Request

    from app.security.net import client_ip

    def req(xff: str | None) -> Request:
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": ("10.0.0.9", 1234)})

    monkeypatch.setattr(settings, "trusted_proxy", False)
    assert client_ip(req("1.2.3.4")) == "10.0.0.9", \
        "X-Forwarded-For was honoured without TRUSTED_PROXY — anyone can forge a bucket"

    monkeypatch.setattr(settings, "trusted_proxy", True)
    assert client_ip(req("1.2.3.4, 10.0.0.1")) == "1.2.3.4", \
        "with a trusted proxy the FIRST hop is the client"
    assert client_ip(req(None)) == "10.0.0.9", "no header falls back to the socket peer"


def test_hosted_mode_without_a_trusted_proxy_says_what_it_costs(monkeypatch, capsys):
    """Silence here is the failure mode: the limits exist, the endpoints look protected,
    and every caller shares one bucket. The warning has to name that consequence, not just
    the setting."""
    from app.security import startup

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "trusted_proxy", False)
    monkeypatch.setattr(settings, "secret_encryption_key", "x" * 44)
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u:p@h/db")

    startup.check_security()
    out = capsys.readouterr().out

    assert "TRUSTED_PROXY" in out, "the warning does not name the setting to change"
    assert "bucket" in out.lower(), (
        "the warning does not say what it costs — an operator who reads 'TRUSTED_PROXY is "
        "off' with no consequence attached has no reason to act on it"
    )


def test_the_proxy_warning_is_silent_when_correctly_configured(monkeypatch, capsys):
    """The control. A warning that always fires is noise, and noise is how the real ones
    get ignored."""
    from app.security import startup

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "trusted_proxy", True)
    monkeypatch.setattr(settings, "secret_encryption_key", "x" * 44)
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://u:p@h/db")

    startup.check_security()
    assert "TRUSTED_PROXY" not in capsys.readouterr().out, \
        "the proxy warning fires even when a trusted proxy is configured"
