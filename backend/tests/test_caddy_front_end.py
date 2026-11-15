"""The Caddy front end keeps what the nginx one was paid for (GRPH-523, GRPH-1635).

Reads `web/Caddyfile` and the snippets it imports. The behaviour itself is proven against the
real image by `web/verify-upstream-reresolution.sh`; these pin the lines whose loss is silent.
"""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"
CADDYFILE = (WEB / "Caddyfile").read_text()


def _code() -> str:
    return "\n".join(ln for ln in CADDYFILE.splitlines() if not ln.strip().startswith("#"))


def test_proxied_paths_are_not_rewritten():
    """A `rewrite`/`uri` directive or a path on the upstream address would change the path the
    API sees while still following a redeploy, so the re-resolution harness stays green."""
    assert not re.search(r"^\s*(rewrite|uri|handle_path)\b", _code(), re.M)
    passes = re.findall(r"reverse_proxy\s+(\S+)", _code())
    assert len(passes) == 3, passes  # org subdomains, /api/*, /health
    assert all(p == "{$API_SCHEME}://{$API_UPSTREAM}" for p in passes), passes


def test_spa_is_the_fallback_and_history_routes_resolve_to_index():
    assert "try_files {path} {path}/ /index.html" in CADDYFILE


def test_cloud_apex_is_exempt_from_the_org_wildcard():
    assert "host *.graphban.dev" in CADDYFILE
    assert "not host cloud.graphban.dev" in CADDYFILE


def test_xff_is_appended_not_overwritten():
    """The API reads TRUSTED_HOPS hops from the right; appending keeps a spoofed prefix to the
    left of the real client."""
    assert "trusted_proxies static 0.0.0.0/0 ::/0" in CADDYFILE


def test_admin_api_is_loopback_only():
    assert re.search(r"^\s*admin localhost:2019\s*$", CADDYFILE, re.M)
    assert "0.0.0.0:2019" not in CADDYFILE


def test_access_log_drops_the_query_string_and_headers():
    """The share link's credential travels as `?token=`; it must not reach the log store."""
    log = CADDYFILE.split("(access_log)")[1].split("(proxy_headers)")[0]
    assert r"request>uri regexp \?.*$" in log
    assert "request>headers delete" in log


def test_on_demand_tls_asks_the_api():
    assert "ask {$ASK_URL}" in CADDYFILE


def test_https_listener_is_on_its_own_port_and_loads_the_box_certificate():
    snippet = (WEB / "https.on.caddy").read_text()
    assert "https://:{$HTTPS_PORT}" in snippet
    assert "/etc/graphban/tls/box.crt /etc/graphban/tls/box.key" in snippet
    assert "{$HTTPS_PORT}" not in (WEB / "https.off.caddy").read_text()


def test_hosted_mode_removes_the_https_listener():
    entry = (WEB / "docker-entrypoint.sh").read_text()
    assert 'HOSTED_MODE' in entry and "https.off.caddy" in entry


def test_proxy_timeouts_outlive_a_park_and_an_llm_call():
    from app.config import settings
    from app.services.fleet import MAX_WAIT_SECONDS

    m = re.search(r"response_header_timeout\s+(\d+)s", CADDYFILE)
    assert m, "no response_header_timeout — the default would sever a full park"
    assert int(m.group(1)) > MAX_WAIT_SECONDS
    assert int(m.group(1)) > settings.llm_timeout_seconds


def test_security_headers_are_set_on_every_response():
    block = CADDYFILE.split("(security_headers)")[1].split("(access_log)")[0]
    for name in ("X-Content-Type-Options", "Referrer-Policy", "Strict-Transport-Security",
                 "Content-Security-Policy"):
        assert name in block


def test_assets_cache_hard_and_the_shell_revalidates():
    assets = CADDYFILE.split("handle /assets/*")[1].split("handle {")[0]
    shell = CADDYFILE.split("handle {\n")[1]
    assert "immutable" in assets and "no-cache" not in assets
    assert "no-cache" in shell
