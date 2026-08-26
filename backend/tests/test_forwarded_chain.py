"""The diagnostic that settles which X-Forwarded-For hop is the real caller (GRPH-517).

`security.net.client_ip` takes `xff.split(",")[0]` — the FIRST entry, which is the least
trustworthy position in the chain, because every hop appends and the leftmost value is
whatever the outermost caller wrote. nginx makes that reachable: it sends
`$proxy_add_x_forwarded_for`, which preserves a caller-supplied header and appends the peer
rather than replacing it.

Whether that is currently exploitable depends on something neither codebase states —
whether Railway's edge overwrites the header or appends to it — and the only other way to
observe it is to fire forged-header traffic at production until a rate-limit bucket breaks.
So this endpoint exists to make the chain *visible* instead, from one ordinary request.

These tests do not assert what the production chain looks like; that is the unknown the
endpoint exists to resolve. They assert the endpoint reports faithfully, because a
diagnostic that misreports is worse than none — it would settle the question wrongly and
the wrong answer would then be built on.
"""
from __future__ import annotations

import pytest

from app.config import settings

CHAIN = "203.0.113.7, 198.51.100.4, 10.0.0.9"


@pytest.fixture()
def admin_headers(client, monkeypatch):
    """A platform admin, via the seeded operator the other admin suites use. The router
    404s for everyone else, so without this there is nothing to assert about the body."""
    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "platform_admin_emails", "alex@ascme-labs.com")
    r = client.post("/api/auth/login",
                    json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_it_reports_the_chain_verbatim(client, admin_headers, monkeypatch):
    """The whole point is fidelity. A summarised or de-duplicated chain would hide exactly
    the structure the hop count has to be derived from."""
    monkeypatch.setattr(settings, "trusted_proxy", True)

    r = client.get("/api/admin/forwarded-chain", headers={**admin_headers,
                                                          "X-Forwarded-For": CHAIN})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["xff_raw"] == CHAIN
    assert body["xff_hops"] == ["203.0.113.7", "198.51.100.4", "10.0.0.9"]
    assert body["xff_hop_count"] == 3


def test_it_reports_where_the_resolved_ip_came_from(client, admin_headers, monkeypatch):
    """`from_right` is the number that matters. A hop count expressed from the left breaks
    the moment a chain of a different length arrives, which is the situation an attacker
    controls."""
    monkeypatch.setattr(settings, "trusted_proxy", True)

    body = client.get("/api/admin/forwarded-chain",
                      headers={**admin_headers, "X-Forwarded-For": CHAIN}).json()

    assert body["resolved_client_ip"] == "203.0.113.7", \
        "client_ip no longer takes the first hop; this diagnostic's premise has changed"
    assert body["resolved_position"] == {"from_left": 0, "from_right": 3}


def test_it_reports_the_fallback_when_no_proxy_is_asserted(client, admin_headers, monkeypatch):
    """With TRUSTED_PROXY off the header must be ignored entirely, and the diagnostic has
    to show that rather than echoing a value nothing used."""
    monkeypatch.setattr(settings, "trusted_proxy", False)

    body = client.get("/api/admin/forwarded-chain",
                      headers={**admin_headers, "X-Forwarded-For": CHAIN}).json()

    assert body["trusted_proxy"] is False
    assert body["resolved_client_ip"] != "203.0.113.7", \
        "the forwarded header was honoured with no trusted proxy asserted"
    assert body["resolved_client_ip"] == body["socket_peer"]


def test_it_surfaces_a_scheme_the_app_disagrees_with(client, admin_headers):
    """GRPH-477, answered by the same response. If uvicorn discards X-Forwarded-Proto —
    because the peer is outside `--forwarded-allow-ips` — the app serves https while
    believing it is http, and any URL built from the request carries the wrong scheme onto
    an HSTS origin."""
    body = client.get("/api/admin/forwarded-chain",
                      headers={**admin_headers, "X-Forwarded-Proto": "https"}).json()

    assert body["header_x_forwarded_proto"] == "https"
    assert body["scheme_matches_forwarded"] is (body["scheme"] == "https"), \
        "scheme_matches_forwarded disagrees with the two fields it compares"


def test_no_forwarded_header_is_reported_as_absent_not_as_empty(client, admin_headers):
    """An empty string and a missing header mean different things — one says a proxy set
    nothing, the other says no proxy spoke. Collapsing them would make a chain of zero
    hops indistinguishable from a chain that was never read."""
    body = client.get("/api/admin/forwarded-chain", headers=admin_headers).json()

    assert body["xff_raw"] is None
    assert body["xff_hops"] == []
    assert body["xff_hop_count"] == 0
    assert body["resolved_position"] is None


def test_it_is_not_readable_without_platform_admin(client, auth):
    """It reflects request headers, which is little on its own — but it also states
    TRUSTED_PROXY and the resolved bucket key, which together tell an attacker exactly how
    to shape a header to get their own bucket. Same 404 as the rest of the router, so the
    console's existence stays undisclosed."""
    r = client.get("/api/admin/forwarded-chain", headers=auth)
    assert r.status_code == 404, \
        f"a non-admin read the proxy diagnostic: {r.status_code} {r.text[:200]}"

    anon = client.get("/api/admin/forwarded-chain")
    assert anon.status_code in (401, 403, 404), f"unauthenticated read: {anon.status_code}"
