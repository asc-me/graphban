"""nginx forwards the original request URI (GRPH-523).

A `proxy_pass` with a URI part — a trailing path after the host, most commonly
`${API_SCHEME}://$api_upstream/` — rewrites `/health` and `/api/items?x=1` to
`uri:/`. Re-resolution still works (who=B), so the docker harness that only
checks the backend identity PASSES, and routing is silently wrong.

Cited from `web/nginx.conf.template`. The file did not exist (never in 10fd287);
this is the pin the bounce asked for.
"""
from __future__ import annotations

from pathlib import Path

TEMPLATE = (Path(__file__).resolve().parents[2] / "web" / "nginx.conf.template").read_text()


def test_proxy_pass_has_no_uri_part():
    """THE CALL. `verify-upstream-reresolution.sh` greps who=B. Adding a trailing
    slash still re-resolves, so that harness stays green while every proxied path
    arrives at the backend as `/`.
    """
    passes = [ln.strip() for ln in TEMPLATE.splitlines()
              if ln.strip().startswith("proxy_pass ") and ln.strip().endswith(";")]
    assert passes, "no proxy_pass directive in nginx.conf.template"
    bad = [p for p in passes if p != "proxy_pass ${API_SCHEME}://$api_upstream;"]
    assert not bad, (
        "these proxy_pass lines carry a URI part, so nginx rewrites the request "
        f"path: {bad}"
    )


def test_both_proxied_locations_are_pinned():
    """/api/ and /health in the catch-all server, plus the *.graphban.dev wildcard
    server block (PRD-43 D8) — three proxy_pass directives, all without a URI part."""
    assert TEMPLATE.count("proxy_pass ${API_SCHEME}://$api_upstream;") == 3


def test_spa_catch_all_is_the_default_server():
    """LAN Hosts (ubuntu-srv) match neither `*.graphban.dev` nor an exact name.

    nginx's default is the FIRST `listen` block unless `default_server` is set.
    The wildcard was first, so `/` was proxied to FastAPI and 404'd as JSON.
    """
    assert "listen ${PORT} default_server;" in TEMPLATE
    assert TEMPLATE.count("listen ${PORT} default_server;") == 1
    # wildcard must NOT be the default
    wild = TEMPLATE.split("server_name *.graphban.dev;")[0]
    assert "default_server" not in wild.split("server {")[-1]


def test_cloud_apex_is_exact_on_the_spa_server():
    """`*.graphban.dev` matches `cloud.graphban.dev`. Exact name on the SPA
    server beats the wildcard so hosted Install is not FastAPI 404."""
    assert "server_name _ cloud.graphban.dev;" in TEMPLATE
