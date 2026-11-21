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


# ---- the Caddy front end (GRPH-340, GRPH-1635) -------------------------------------
import re
from pathlib import Path

_CADDYFILE = (Path(__file__).resolve().parents[2] / "web" / "Caddyfile").read_text()


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

    m = re.search(r"response_header_timeout\s+(\d+)s", _CADDYFILE)
    assert m, "no response_header_timeout — the default would sever a full park"
    assert int(m.group(1)) > MAX_WAIT_SECONDS, (
        f"response_header_timeout {m.group(1)}s must exceed MAX_WAIT_SECONDS {MAX_WAIT_SECONDS}s; "
        "equal values are a coin flip, not a bound")


def test_the_proxy_outlives_an_llm_bound_call():
    """A synchronous tool bounded by `llm_timeout_seconds` must not race nginx's cutoff.

    `extract_lessons` used to block on the model inside POST /api/mcp; with both limits at
    90s a slow distil returned 504 Gateway Time-out while the upstream was still working.
    Asserted as a relation so raising either side without the other reintroduces it."""
    from app.config import settings

    m = re.search(r"response_header_timeout\s+(\d+)s", _CADDYFILE)
    assert m, "no response_header_timeout"
    assert int(m.group(1)) > settings.llm_timeout_seconds, (
        f"response_header_timeout {m.group(1)}s must exceed llm_timeout_seconds "
        f"{settings.llm_timeout_seconds}s; equal values are a coin flip, not a bound")
