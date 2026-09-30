"""`--prd` refuses a server that ignores it (GRPH-800).

An MCP server that has never heard of `prd_id` does not refuse the argument — it drops it and
answers the unfiltered question. Measured against the deployed 2026.09.16, which predates the
filter: scoped and unscoped both returned the same four clusters, and nothing errored.

So shipping `--prd` without this would give an operator a flag that silently drains the whole
project while reporting the wave as scoped. That is not "the fix is missing", it is the
original bug wearing the fix's clothes, and it is worse than having no flag at all.
"""
from __future__ import annotations

import pytest

from gbfleet import until
from gbfleet.client import ServerUnreachable


class Server:
    """Answers `collision_clusters`. `filters=False` is the old server: it drops the
    unrecognised argument and answers the unfiltered question."""

    def __init__(self, total=4, filters=True, error=None, seat_scope=True,
                 manifest_error=None):
        self.total, self.filters, self.error, self.asked = total, filters, error, []
        # GRPH-827: whether `delegate` declares `scope`. A server can filter the divvy and
        # still drop the seat's scope — those shipped in different releases, so the two are
        # separate switches here rather than one "modern server" flag.
        self.seat_scope, self.manifest_error = seat_scope, manifest_error

    def call(self, tool, **kw):
        self.asked.append(kw)
        if self.error is not None:
            raise self.error
        if self.filters and kw.get("prd_id"):
            return {"clusters": [], "total": 0}
        return {"clusters": [{"items": ["X-1"]}], "total": self.total}

    def list_tools(self):
        if self.manifest_error is not None:
            raise self.manifest_error
        props = {"id": {"type": "string"}}
        if self.seat_scope:
            props["scope"] = {"type": "string"}
        return [{"name": "collision_clusters", "inputSchema": {"properties": {}}},
                {"name": "delegate", "inputSchema": {"properties": props}}]


def test_a_server_that_ignores_the_filter_is_refused(capsys):
    """THE POINT. Loud, before a single child is spawned."""
    with pytest.raises(until.ConfigError) as exc:
        until.check_scope_is_honoured(Server(filters=False), "SA-P11")

    said = str(exc.value)
    assert "ignores prd_id" in said
    assert "every ready item" in said, "did not say what it would actually do"
    assert "Upgrade the server" in said, "refused without a remedy"


def test_a_server_that_filters_is_accepted():
    until.check_scope_is_honoured(Server(filters=True), "SA-P11")


def test_the_probe_asks_for_a_prd_that_cannot_exist():
    """The question has to be one whose answer is unambiguous. Probing with the REAL prd id
    would read "supported" whenever that PRD happened to have no ready work."""
    server = Server(filters=True)

    until.check_scope_is_honoured(server, "SA-P11")

    assert server.asked == [{"prd_id": until.PROBE_PRD}]
    assert "SA-P11" not in str(server.asked)


def test_an_unreachable_server_does_not_refuse_the_wave(capsys):
    """Could not ask is not evidence either way, and the loop already handles an unreachable
    server. Refusing here would turn a transient outage into a config error."""
    until.check_scope_is_honoured(Server(error=ServerUnreachable("down")), "SA-P11")


def test_an_empty_project_reads_as_supported_and_that_is_fine():
    """The one case the probe cannot distinguish, asserted rather than left implicit: a
    project with no ready clusters answers zero either way. It does not matter, because there
    is nothing to delegate."""
    until.check_scope_is_honoured(Server(total=0, filters=False), "SA-P11")


# ---- the second half: the server takes the scope but does not put it on the seat -------------

def test_a_server_that_filters_but_cannot_scope_a_seat_is_refused():
    """THE ONE THAT MATTERS for GRPH-827. This server passes the probe above completely: every
    delegation lands inside the PRD. Its children then hold unscoped credentials and claim
    whatever they like, which is the measured behaviour — three delegated inside the scope,
    six self-claimed outside it. A wave that reports as scoped while its workers are not is
    the same lie one layer down."""
    with pytest.raises(until.ConfigError) as exc:
        until.check_scope_is_honoured(Server(filters=True, seat_scope=False), "SA-P11")

    said = str(exc.value)
    assert "no `scope`" in said
    assert "claim the whole" in said, "did not say what it would actually do"
    assert "Upgrade the server" in said, "refused without a remedy"


def test_a_server_with_both_halves_is_accepted():
    until.check_scope_is_honoured(Server(filters=True, seat_scope=True), "SA-P11")


def test_the_seat_probe_reads_the_manifest_rather_than_minting_a_seat():
    """A seat is the thing being bounded; minting one with an impossible scope to find out
    whether scoping works would leave a real credential behind on every server that passes."""
    server = Server(filters=True)

    until.check_scope_is_honoured(server, "SA-P11")

    assert [kw for kw in server.asked if "role" in kw] == [], "the probe minted something"


def test_an_unreadable_manifest_does_not_refuse_the_wave():
    """Same rule as the unreachable server above: could not ask is not evidence."""
    until.check_scope_is_honoured(
        Server(filters=True, manifest_error=ServerUnreachable("down")), "SA-P11")


# ---- the third probe: can this server mint a seat that cannot claim? (GRPH-988) --------------

class MintServer:
    """Answers `tools/list` and nothing else — the review-only probe reads the manifest, so a
    `call` reaching this class is the probe doing the thing it must not."""

    def __init__(self, review_only=True, manifest_error=None):
        self.review_only, self.manifest_error = review_only, manifest_error
        self.asked = []

    def call(self, tool, **kw):
        self.asked.append((tool, kw))
        raise AssertionError(f"the review-only probe called {tool}; it must only read the "
                             "manifest, because the alternative is minting a real seat to find "
                             "out whether seats can be limited")

    def list_tools(self):
        if self.manifest_error is not None:
            raise self.manifest_error
        props = {"agent_id": {"type": "string"}, "role": {"type": "string"}}
        if self.review_only:
            props["review_only"] = {"type": "boolean"}
        return [{"name": "mint_enrolment", "inputSchema": {"properties": props}}]


def test_a_server_that_cannot_mint_a_review_only_seat_is_refused():
    """THE POINT for GRPH-988. A server that has never heard of `review_only` drops the
    argument and mints a plain worker seat, so a supervisor started with `--max-workers 0` —
    a promise that it builds nothing — would put a child on the board that can `claim_next`,
    and every report would say the wave was review-only."""
    with pytest.raises(until.ConfigError) as exc:
        until.check_review_only_is_honoured(MintServer(review_only=False))

    said = str(exc.value)
    assert "no `review_only`" in said
    assert "claim build work" in said, "did not say what it would actually do"
    assert "Upgrade the server" in said, "refused without a remedy"


def test_a_server_that_can_mint_review_only_is_accepted():
    until.check_review_only_is_honoured(MintServer(review_only=True))


def test_the_review_only_probe_reads_the_manifest_rather_than_minting_a_seat():
    """The same instrument as the seat-scope probe, for the same reason: a seat is the thing
    being bounded, and minting one to find out whether it can be bounded leaves a live
    credential behind on every server that passes."""
    server = MintServer(review_only=True)

    until.check_review_only_is_honoured(server)

    assert server.asked == [], "the probe called a tool"


def test_an_unreadable_manifest_does_not_refuse_a_review_only_wave():
    """Could not ask is not evidence either way, and refusing here would turn a transient
    outage into a config error — the rule the two probes above already follow."""
    until.check_review_only_is_honoured(
        MintServer(manifest_error=ServerUnreachable("down")))


def test_a_manifest_with_no_mint_enrolment_does_not_refuse_the_wave():
    """`mint_enrolment` absent entirely is a different problem — a credential that cannot mint
    at all — and the loop's own handling of the first failed mint reports it better than a
    guess here would. Same fallthrough as the seat-scope probe's missing `delegate`."""
    class NoMint(MintServer):
        def list_tools(self):
            return [{"name": "delegate", "inputSchema": {"properties": {}}}]

    until.check_review_only_is_honoured(NoMint())
