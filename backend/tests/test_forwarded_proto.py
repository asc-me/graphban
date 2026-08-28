"""The forwarded scheme is only trusted from a peer we said to trust (GRPH-477).

`web/nginx.conf.template` sets `X-Forwarded-Proto: $scheme` correctly. The API discards it,
because uvicorn honours forwarded headers only from `forwarded_allow_ips`, which defaults to
`127.0.0.1` — and on Railway `web` (nginx) and `backend` are SEPARATE SERVICES, so the API's
socket peer is an internal address and never loopback. `request.url.scheme` therefore reads
`http` on an HTTPS-only origin.

**Nothing derives a URL from it today**, which is why the item was filed rather than treated as
an outage: every generated link builds from `settings.app_base_url`. But *"nothing currently
reads the scheme"* is a weaker guarantee than *"the scheme is right"*, and it stops being true
the first time someone adds a redirect, an OAuth callback or a `url_for`. The failure then
presents as a broken feature — an `http://` link that HSTS refuses — rather than as a scheme bug.

**Why these tests drive the middleware directly.** The item's own sabotage note says it: a test
asserting `request.url.scheme == "https"` through `TestClient` passes whatever the setting is,
because there is no proxy in the loop and no peer address to judge. The only checkable claim in
process is the one the middleware actually makes — this peer is trusted, that one is not — so
that is what is asserted here. Whether the deployment sets the variable is a fact about the
deployment, and `docs/deploy-railway.md` is where that is stated.
"""
from __future__ import annotations

import asyncio

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


def _scheme_seen(trusted: str | list[str], peer: str, forwarded: str = "https") -> str:
    """Run one request through the middleware and report the scheme the app would see."""
    seen: dict[str, str] = {}

    async def app(scope, receive, send):
        seen["scheme"] = scope["scheme"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    scope = {
        "type": "http", "scheme": "http", "client": (peer, 51234),
        "headers": [(b"x-forwarded-proto", forwarded.encode()),
                    (b"x-forwarded-for", b"203.0.113.9")],
    }

    async def receive():   # pragma: no cover — the app never reads a body here
        return {"type": "http.request", "body": b""}

    asyncio.run(wrapped(scope, receive, lambda m: asyncio.sleep(0)))
    return seen["scheme"]


def test_the_default_discards_the_forwarded_scheme_from_another_service():
    """THE DEFECT, pinned as the current behaviour rather than as a bug to fix here.

    `127.0.0.1` is uvicorn's default and it is the wrong answer for this topology: nginx runs
    in a different Railway service, so its requests arrive from an internal address. The header
    is set, sent, and thrown away.
    """
    assert _scheme_seen("127.0.0.1", peer="10.250.3.7") == "http", (
        "a non-loopback peer's forwarded scheme was honoured under the default — the defect "
        "this item describes would not exist")


def test_a_trusted_private_peer_is_honoured():
    """The fix, expressed as configuration rather than code: uvicorn reads
    `FORWARDED_ALLOW_IPS` from the environment, so the deployment sets the trusted range and
    nothing in the image changes."""
    assert _scheme_seen("10.0.0.0/8", peer="10.250.3.7") == "https"


def test_an_untrusted_peer_is_still_refused_under_the_fix():
    """THE HALF THAT MAKES IT A FIX RATHER THAN A HOLE. Widening to `*` would satisfy the test
    above and let ANY direct caller forge the scheme.

    That is not hypothetical here: this item recorded that the backend had a second public
    Railway domain bypassing nginx entirely, which is why `*` was ruled out and the ingress was
    closed first (GRPH-478). Order matters — trusting the header before closing the ingress
    leaves a window where the open path is believed.
    """
    assert _scheme_seen("10.0.0.0/8", peer="198.51.100.4") == "http", (
        "a caller outside the trusted range forged the scheme")


def test_a_wildcard_trusts_everyone_and_is_why_it_was_rejected():
    """Stated as a test so the reason is executable rather than a sentence in a runbook. `*`
    is the tempting one-word fix, and it means an arbitrary internet peer chooses the scheme."""
    assert _scheme_seen("*", peer="198.51.100.4") == "https"


@pytest.mark.parametrize("forwarded", ["http", "https"])
def test_the_middleware_reports_what_was_forwarded_not_a_constant(forwarded):
    """A middleware that always said `https` would pass every positive test above while
    telling the app nothing."""
    assert _scheme_seen("10.0.0.0/8", peer="10.250.3.7", forwarded=forwarded) == forwarded


def test_uvicorn_reads_the_setting_from_the_environment(monkeypatch):
    """The fix is a variable, so the plumbing that makes a variable reach uvicorn is the thing
    to pin. `app/serve.py` passes no proxy arguments — it does not need to, because `Config`
    reads `FORWARDED_ALLOW_IPS` itself. If that ever stopped being true, setting the variable
    on the deployment would silently do nothing, which is the failure this whole item is about
    one level down.
    """
    from uvicorn.config import Config

    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    assert Config("app.main:app").forwarded_allow_ips == "127.0.0.1"

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    assert Config("app.main:app").forwarded_allow_ips == "10.0.0.0/8", (
        "uvicorn no longer reads FORWARDED_ALLOW_IPS from the environment, so the deployment "
        "variable does nothing and app/serve.py must pass it explicitly")
    assert Config("app.main:app").proxy_headers is True
