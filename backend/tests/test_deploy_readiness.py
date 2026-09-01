"""SaaS-arc Phase 5 (slice A): Railway readiness, rate limiting, observability.

Covers the unit-testable code — DATABASE_URL normalization, the request-id
middleware, the rate-limiter front door (in-process fallback), and the hosted
per-org MCP burst cap. Docker/nginx/railway.json plumbing is verified at deploy time.
"""
import pytest


# ---- DATABASE_URL normalization (AL-26) ---------------------------------------
@pytest.mark.parametrize(
    "given,expected",
    [
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql+psycopg://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("sqlite:///./x.db", "sqlite:///./x.db"),
    ],
)
def test_database_url_normalized(given, expected):
    from app.config import Settings

    assert Settings(database_url=given, _env_file=None).database_url == expected


# ---- request-id middleware (AL-56) --------------------------------------------
def test_request_id_generated_when_absent(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    rid = r.headers.get("x-request-id")
    assert rid and len(rid) >= 8


def test_request_id_echoed_when_provided(client):
    r = client.get("/api/config", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers.get("x-request-id") == "trace-abc-123"


# ---- rate-limit front door (in-process fallback) ------------------------------
def test_ratelimit_allow_blocks_over_limit():
    from app.services import ratelimit, spam

    spam._hits.clear()
    key = "test:ratelimit:key"
    assert ratelimit.allow(key, 2) is True
    assert ratelimit.allow(key, 2) is True
    assert ratelimit.allow(key, 2) is False  # third within the window is blocked


# ---- hosted per-org MCP burst cap ---------------------------------------------
def _mcp(client, key, tool):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": {}}},
        headers={"X-API-Key": key},
    ).json()


def test_org_rate_cap_trips_rate_limited(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    monkeypatch.setattr(settings, "org_rate_per_min", 2)

    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/api/orgs", json={"name": "Acme"}, headers=auth)
    client.post("/api/projects", json={"name": "Rocket"}, headers=auth)
    key = client.post("/api/api-keys", json={"name": "agent"}, headers=auth).json()["plaintext"]

    _mcp(client, key, "get_backlog")
    _mcp(client, key, "get_backlog")
    third = _mcp(client, key, "get_backlog")["result"]
    assert third.get("isError") is True
    assert third["structuredContent"]["error"]["code"] == "rate_limited"


def test_org_rate_cap_off_self_host(client, monkeypatch):
    """With hosted_mode off, the per-org cap never engages."""
    from app.config import settings

    monkeypatch.setattr(settings, "org_rate_per_min", 1)  # would bite if hosted
    auth_r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    auth = {"Authorization": f"Bearer {auth_r.json()['access_token']}"}
    key = client.post("/api/api-keys", json={"name": "agent"}, headers=auth).json()["plaintext"]
    for _ in range(3):
        assert "error" not in _mcp(client, key, "get_backlog")["result"].get("structuredContent", {})


# ---- the nginx template (GRPH-340) --------------------------------------------
#
# This file's docstring said nginx plumbing is "verified at deploy time", which in practice
# meant not verified: nothing read the template, so a wrong directive shipped and was found by
# a human noticing stale behaviour in a browser. These two rules are the ones whose breakage
# is silent — a served page looks completely normal whether or not it carries them.
import re
from pathlib import Path

_TEMPLATE = (Path(__file__).resolve().parents[2] / "web" / "nginx.conf.template").read_text()

_SECURITY_HEADERS = (
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Strict-Transport-Security",
    "Content-Security-Policy",
)


def _location_blocks(text):
    """(match, body) for each `location … { … }`."""
    blocks = []
    for m in re.finditer(r"^\s*location\s+([^{]+)\{", text, re.M):
        start = text.index("{", m.start())
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append((m.group(1).strip(), text[start:i]))
                    break
    return blocks


def test_index_html_revalidates_while_hashed_assets_are_cached_hard():
    """The failure this prevents is the one that wastes a debugging session rather than
    breaking a page: index.html is the only unhashed file and it NAMES the hashed bundle, so a
    cached copy pins a browser to the previous deploy's JS. The server reports the new version,
    the tab runs the old one, and every test run against it is quietly measuring stale code."""
    by_match = dict(_location_blocks(_TEMPLATE))

    assert "no-cache" in by_match["/"], "index.html must revalidate on every load"
    assert "immutable" in by_match["/assets/"], "content-hashed assets should cache hard"
    assert "no-cache" not in by_match["/assets/"], "hashed assets need no revalidation"


@pytest.mark.parametrize("header", _SECURITY_HEADERS)
def test_no_location_drops_a_security_header_by_setting_one_of_its_own(header):
    """nginx's `add_header` REPLACES the inherited set instead of appending to it. So the
    moment a location adds a Cache-Control, it silently loses nosniff, HSTS, Referrer-Policy
    and the CSP — and the response looks entirely fine. This is the whole reason the cache
    blocks above repeat all four."""
    for match, body in _location_blocks(_TEMPLATE):
        if "add_header" not in body:
            continue
        assert header in body, f"location {match} sets headers of its own and drops {header}"


def test_the_proxy_outlives_a_full_length_park():
    """A parked agent must get its answer, not a 504.

    `claim_cluster(wait_seconds=60)` blocks for up to `MAX_WAIT_SECONDS` by design, and nginx's
    DEFAULT `proxy_read_timeout` is also 60s — so a full-length park raced the proxy's cutoff
    and lost about half the time. A real Cursor client hit it on the PRD-17 acceptance walk:
    `upstream timed out ... POST /api/mcp`, 504. Every park before that ran against an injected
    clock, which is why nothing caught it.

    Asserted as a RELATION, not a literal: raising MAX_WAIT_SECONDS without raising the proxy
    would silently reintroduce it, and the number that matters is the gap between them."""
    from app.services.fleet import MAX_WAIT_SECONDS

    m = re.search(r"proxy_read_timeout\s+(\d+)s", _TEMPLATE)
    assert m, "no proxy_read_timeout — nginx defaults to 60s and would sever a full park"
    assert int(m.group(1)) > MAX_WAIT_SECONDS, (
        f"proxy_read_timeout {m.group(1)}s must exceed MAX_WAIT_SECONDS {MAX_WAIT_SECONDS}s; "
        "equal values are a coin flip, not a bound")


def test_the_proxy_outlives_an_llm_bound_call():
    """A synchronous tool bounded by `llm_timeout_seconds` must not race nginx's cutoff.

    `extract_lessons` used to block on the model inside POST /api/mcp; with both limits at
    90s a slow distil returned 504 Gateway Time-out while the upstream was still working.
    Asserted as a relation so raising either side without the other reintroduces it."""
    from app.config import settings

    m = re.search(r"proxy_read_timeout\s+(\d+)s", _TEMPLATE)
    assert m, "no proxy_read_timeout — nginx defaults to 60s"
    assert int(m.group(1)) > settings.llm_timeout_seconds, (
        f"proxy_read_timeout {m.group(1)}s must exceed llm_timeout_seconds "
        f"{settings.llm_timeout_seconds}s; equal values are a coin flip, not a bound")
