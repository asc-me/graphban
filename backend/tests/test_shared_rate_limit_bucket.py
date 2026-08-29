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
def _hops_off(monkeypatch):
    """`TRUSTED_HOPS` off by default here, so every test below still measures the legacy
    `TRUSTED_PROXY` path it was written for. Without this the new setting would silently
    change what the old tests mean, and they would keep passing while asserting something
    else."""
    monkeypatch.setattr(settings, "trusted_hops", 0)


@pytest.fixture(autouse=True)
def _fresh_warning():
    """The warning is once per process, so it has to be re-armed between tests — otherwise
    every test after the first would assert against a flag another test already tripped."""
    net._warned.clear()
    yield
    net._warned.clear()


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


# ── the fix: counting hops from the RIGHT (GRPH-553) ──────────────────────────

def test_a_forged_prefix_cannot_choose_its_bucket_on_the_compose_stack(monkeypatch):
    """THE ONE THE TICKET IS ABOUT. Compose is client -> nginx -> app, and nginx APPENDS with
    `$proxy_add_x_forwarded_for`, so a browser sending its own header produces
    `<forged>, <real>`. Reading the first hop hands the caller its bucket key; reading one
    from the right reads what nginx actually saw."""
    monkeypatch.setattr(settings, "trusted_hops", 1)
    assert net.client_ip(_Req("172.20.0.4", xff="9.9.9.9, 203.0.113.7")) == "203.0.113.7"


def test_a_caller_behind_no_proxy_still_gets_its_own_bucket(monkeypatch):
    monkeypatch.setattr(settings, "trusted_hops", 1)
    assert net.client_ip(_Req("172.20.0.4", xff="203.0.113.7")) == "203.0.113.7"


def test_the_edge_topology_reads_one_further_left(monkeypatch):
    """Railway is client -> edge -> nginx -> app. The same template appends, so the header is
    `<real>, <edge>` and the rightmost entry is the EDGE. Taking it would collapse every
    hosted caller into one bucket — the exact breakage that makes the naive nginx fix wrong."""
    monkeypatch.setattr(settings, "trusted_hops", 2)
    assert net.client_ip(_Req("172.20.0.4", xff="203.0.113.7, 100.64.0.1")) == "203.0.113.7"


def test_a_forged_prefix_cannot_choose_its_bucket_behind_an_edge(monkeypatch):
    """The same attack one topology along: the client sends a header, the edge appends the
    real address, nginx appends the edge. Two from the right is still what the edge saw."""
    monkeypatch.setattr(settings, "trusted_hops", 2)
    got = net.client_ip(_Req("172.20.0.4", xff="9.9.9.9, 203.0.113.7, 100.64.0.1"))
    assert got == "203.0.113.7"


def test_a_header_shorter_than_the_chain_fails_CLOSED(monkeypatch):
    """A request that did not come through the configured chain — a direct hit on the app
    port, which GRPH-478 says is reachable. Trusting a short header here would let anyone who
    reaches the app directly pick a bucket by sending one entry."""
    monkeypatch.setattr(settings, "trusted_hops", 2)
    assert net.client_ip(_Req("172.20.0.4", xff="9.9.9.9")) == "172.20.0.4"


def test_no_header_at_all_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "trusted_hops", 1)
    assert net.client_ip(_Req("172.20.0.4")) == "172.20.0.4"


@pytest.mark.parametrize("junk", ["not-an-ip", "a" * 400, "203.0.113.7; rm -rf /"])
def test_a_hop_that_is_not_an_address_is_never_a_bucket_key(monkeypatch, junk):
    """An unvalidated key is attacker-chosen and unbounded: a few thousand junk values are a
    few thousand buckets, which is a memory question rather than only a fairness one."""
    monkeypatch.setattr(settings, "trusted_hops", 1)
    assert net.client_ip(_Req("172.20.0.4", xff=f"9.9.9.9, {junk}")) == "172.20.0.4"


def test_the_short_header_case_says_which_of_the_two_things_went_wrong(monkeypatch, caplog):
    """Fails closed AND says so. Silent fallback is how the original defect survived: a
    misconfigured deployment behaves like a working one with strict limits."""
    monkeypatch.setattr(settings, "trusted_hops", 3)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        net.client_ip(_Req("172.20.0.4", xff="9.9.9.9"))
    assert "TRUSTED_HOPS=3" in caplog.text
    assert "bypassed" in caplog.text and "hop count is wrong" in caplog.text


def test_a_correctly_configured_hop_count_does_not_warn(monkeypatch, caplog):
    """The fixed state must be quiet, or the warning is noise operators learn to skip."""
    monkeypatch.setattr(settings, "trusted_hops", 1)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        net.client_ip(_Req("172.20.0.4", xff="203.0.113.7"))
    assert not caplog.text.strip()


def test_hops_takes_precedence_over_the_legacy_flag(monkeypatch):
    """Both set is what an upgrading deployment looks like mid-migration. The correct
    mechanism must win, not the one that reads the spoofable end."""
    monkeypatch.setattr(settings, "trusted_hops", 1)
    monkeypatch.setattr(settings, "trusted_proxy", True)
    assert net.client_ip(_Req("172.20.0.4", xff="9.9.9.9, 203.0.113.7")) == "203.0.113.7"


def test_the_legacy_flag_now_says_it_is_spoofable(monkeypatch, caplog):
    """`TRUSTED_PROXY` still works, because silently changing what a live deployment's config
    means is worse than a footgun with a label on it. But it no longer says nothing."""
    monkeypatch.setattr(settings, "trusted_proxy", True)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        assert net.client_ip(_Req("172.20.0.4", xff="9.9.9.9, 203.0.113.7")) == "9.9.9.9"
    assert "choose its own bucket" in caplog.text
    assert "TRUSTED_HOPS" in caplog.text


def test_the_shared_bucket_warning_still_fires_with_neither_set(monkeypatch, caplog):
    """The GRPH-439 detection is not traded away for the fix."""
    monkeypatch.setattr(settings, "trusted_proxy", False)
    monkeypatch.setattr(settings, "trusted_hops", 0)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        net.client_ip(_Req("172.20.0.4"))
    assert "shares one bucket" in caplog.text


def test_a_direct_caller_bypassing_the_proxy_cannot_pick_its_bucket(monkeypatch):
    """FOUND BY THE TEST ABOVE, and it is the hole hop counting does not close on its own.

    With `TRUSTED_HOPS=1`, one entry in the header is exactly what nginx produces for a real
    client — so a caller hitting the app port DIRECTLY and sending one entry is
    indistinguishable from it. GRPH-478 says that port is publicly reachable, so this is a
    live path rather than a thought experiment. The socket peer is what tells them apart: a
    real proxied request comes from the container network, a direct one does not.
    """
    monkeypatch.setattr(settings, "trusted_hops", 1)
    # A genuinely routable peer. `198.51.100.x` and `203.0.113.x` read as PRIVATE to
    # `ipaddress` — Python counts the RFC 5737 documentation ranges as private — so using the
    # conventional example addresses here would have made this test pass by taking the
    # trusted-proxy branch, proving the opposite of what it claims.
    assert net.client_ip(_Req("8.8.8.8", xff="9.9.9.9")) == "8.8.8.8"


def test_a_public_peer_is_its_own_bucket_rather_than_a_shared_one(monkeypatch):
    """The control: refusing the header must not collapse direct callers together. Each still
    buckets on its own socket address, which is the one value it cannot forge."""
    monkeypatch.setattr(settings, "trusted_hops", 1)
    a = net.client_ip(_Req("8.8.8.8", xff="9.9.9.9"))
    b = net.client_ip(_Req("1.1.1.1", xff="9.9.9.9"))
    assert a != b


def test_two_different_warnings_are_both_emitted(caplog):
    """The `_warn_once` contract, tested directly because nothing else can reach it.

    The three call sites are mutually exclusive by configuration, so a single boolean flag
    would behave identically today and a sabotage run proved it — replacing the dict with one
    flag passes every other test in this file. That makes this the guard for the FOURTH
    warning, which would otherwise be swallowed by whichever of the three had already fired,
    and swallowed silently.
    """
    net._warned.clear()
    with caplog.at_level(logging.WARNING, logger="app.security"):
        net._warn_once("first problem: %s", "a")
        net._warn_once("first problem: %s", "b")   # same message, suppressed
        net._warn_once("second, unrelated problem")

    assert caplog.text.count("first problem") == 1, "the same warning repeated"
    assert "second, unrelated problem" in caplog.text, (
        "a distinct warning was swallowed by an earlier one"
    )
