"""A rate limit that cannot tell callers apart says so (GRPH-439).

`TRUSTED_PROXY` was reasoned about correctly for Railway and never carried to the compose
stack, which has the identical topology — nginx in front of uvicorn. With the flag unset,
`client_ip` falls back to the socket peer, which behind nginx is the container address for
everyone. So on the port the UI, the MCP endpoint and every public form actually use, the
per-IP sliding window became a per-DEPLOYMENT one.

Not a bypass: the limits still fire. They fire on the wrong population — one noisy caller of
a public form exhausts `/api/public/*` for every visitor, and the login limiter's IP half
starts refusing unrelated users during someone else's brute-force attempt.

**Why nothing said so.** `client_ip` fails soft by design, so a misconfigured deployment
behaves like a working one with unusually strict limits. `startup.py` does warn about exactly
this — and could not have caught it, because that check is gated on `hosted_mode`, which the
self-host does not set.

The tests below cover the detection and the protection that must not be traded away for it.
The deployment half — flipping the flag, and what nginx must do to `X-Forwarded-For` first —
is deliberately not here; see the ticket.
"""
from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.security import net


@pytest.fixture(autouse=True)
def _fresh_warning():
    """The warning is once per process, so it has to be re-armed between tests — otherwise
    every test after the first would assert against a flag another test already tripped."""
    net._warned_shared_bucket = False
    yield
    net._warned_shared_bucket = False


class _Req:
    def __init__(self, peer: str | None, xff: str | None = None):
        self.client = type("C", (), {"host": peer})() if peer else None
        self.headers = {"x-forwarded-for": xff} if xff else {}


# ── the protection that must survive ──────────────────────────────────────────

def test_a_spoofed_header_is_still_ignored_when_the_flag_is_off(monkeypatch):
    """The existing guard, asserted because this ticket is about turning the flag ON and the
    reason it is off by default must not be quietly traded away."""
    monkeypatch.setattr(settings, "trusted_proxy", False)
    assert net.client_ip(_Req("172.20.0.4", xff="9.9.9.9")) == "172.20.0.4"


def test_the_header_is_honoured_only_when_the_operator_asserts_a_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy", True)
    assert net.client_ip(_Req("172.20.0.4", xff="9.9.9.9")) == "9.9.9.9"


def test_the_first_hop_is_the_one_taken(monkeypatch):
    """Load-bearing for the deployment question: with the flag on, the bucket key is the
    FIRST entry. A proxy that appends to a client-supplied header therefore hands the caller
    its own bucket key, which is why flipping the flag is not a one-line change."""
    monkeypatch.setattr(settings, "trusted_proxy", True)
    assert net.client_ip(_Req("172.20.0.4", xff="9.9.9.9, 10.0.0.1")) == "9.9.9.9"


# ── the detection ─────────────────────────────────────────────────────────────

def test_a_private_peer_with_the_flag_off_warns(monkeypatch, caplog):
    """The one thing that would have made this visible without somebody going looking."""
    monkeypatch.setattr(settings, "trusted_proxy", False)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        net.client_ip(_Req("172.20.0.4"))
    assert "shares one bucket" in caplog.text
    assert "172.20.0.4" in caplog.text, "the warning must name the address it is bucketing on"


def test_it_warns_once_and_not_per_request(monkeypatch, caplog):
    """The condition is true on every request once it is true at all. A per-request warning
    would bury the thing it exists to surface."""
    monkeypatch.setattr(settings, "trusted_proxy", False)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        for _ in range(5):
            net.client_ip(_Req("172.20.0.4"))
    assert caplog.text.count("shares one bucket") == 1


def test_a_public_peer_does_not_warn(monkeypatch, caplog):
    """No proxy in front is a legitimate deployment, and warning about it is how a real
    warning gets scrolled past."""
    monkeypatch.setattr(settings, "trusted_proxy", False)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        net.client_ip(_Req("8.8.8.8"))
    assert "shares one bucket" not in caplog.text


def test_a_correctly_configured_proxy_does_not_warn(monkeypatch, caplog):
    """The flag on is the fixed state — warning there would report the fix as the fault."""
    monkeypatch.setattr(settings, "trusted_proxy", True)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        net.client_ip(_Req("172.20.0.4", xff="203.0.113.7"))
    assert "shares one bucket" not in caplog.text


@pytest.mark.parametrize("peer", ["127.0.0.1", "10.1.2.3", "192.168.0.9", "169.254.1.1"])
def test_the_private_ranges_that_mean_a_proxy(monkeypatch, caplog, peer):
    monkeypatch.setattr(settings, "trusted_proxy", False)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        net.client_ip(_Req(peer))
    assert "shares one bucket" in caplog.text


@pytest.mark.parametrize("peer", ["unknown", "not-an-ip", ""])
def test_an_unparseable_peer_is_not_guessed_at(monkeypatch, caplog, peer):
    """Treating anything unrecognised as private would warn on deployments that are fine,
    and a warning that cries wolf is one operators learn to scroll past."""
    monkeypatch.setattr(settings, "trusted_proxy", False)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        net.client_ip(_Req(peer or None))
    assert "shares one bucket" not in caplog.text


def test_a_configured_proxy_does_not_warn_even_when_a_request_bypasses_it(monkeypatch, caplog):
    """The discriminating case, and it survived the first sabotage pass.

    `test_a_correctly_configured_proxy_does_not_warn` sends a header, so `client_ip` returns
    at the `trusted_proxy` branch and never reaches the warning at all — removing the
    `not settings.trusted_proxy` guard broke nothing. The path that DOES reach it is a
    request with the flag on and no header: someone hitting the API port directly, past
    nginx. The operator has configured this correctly and must not be told otherwise.
    """
    monkeypatch.setattr(settings, "trusted_proxy", True)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        assert net.client_ip(_Req("172.20.0.4")) == "172.20.0.4"
    assert "shares one bucket" not in caplog.text
