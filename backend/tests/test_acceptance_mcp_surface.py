"""The agent-facing surface for delivery acceptance (GRPH-254 / PRD-12).

Everything PRD-12 built is reachable over REST; an auditor is an agent, and an agent
speaks MCP. These are the round-trip tests — a real JSON-RPC `tools/call` envelope with an
`X-API-Key`, not a service call in disguise.

**Four tools, not eight.** The item flags the `tools/list` footprint (AL-146/AL-48), and
every read here is the same question asked of a different surface, so they take a `view`
rather than each claiming a name. There is deliberately **no invalidation tool**: the hold
rides on every item an agent reads (GRPH-242/312), because a notice you have to remember
to fetch is one you miss.
"""
import pytest

from app.services import items as items_svc
from app.services import prds as prd_svc
from tests import attest

BODY = (
    "# Spec\n\n"
    "## Problem\n\nNothing checks delivery.\n\n"
    "## Baseline\n\nFreeze the spec at approval.\n\n"
    "## Judging\n\nClassify each completed item against the goal.\n"
)


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def mcp_headers(client, auth):
    """A real agent credential. The MCP layer identifies the signer as `agent:<key name>`,
    which is the same string `claim_next` stamps into `claimed_by` — so self-signing is
    detectable without the two surfaces having to agree by convention."""
    key = client.post("/api/api-keys", json={"name": "auditor"}, headers=auth).json()
    return {"X-API-Key": key["plaintext"]}


AGENT = "agent:auditor"


@pytest.fixture()
def approved(db):
    prd = prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY)
    prd_svc.record_grill_turns(db, prd.id, [{"role": "user", "text": "An answer."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    prd_svc.sync_status(db, prd)
    item = items_svc.create_item(db, title="Built it", project_id="core",
                                 prd_id=prd.id, prd_section="Baseline")
    attest.complete(db, item.id)
    return prd


def _call(client, mcp_headers, tool, args):
    r = client.post("/api/mcp", headers=mcp_headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    })
    assert r.status_code == 200, r.text
    return r.json()


def _ok(payload):
    assert "error" not in payload, payload["error"]
    return payload["result"]["structuredContent"]


def _err(payload):
    """A refused tool call comes back as a RESULT carrying `isError`, not a JSON-RPC
    transport error — the call reached the server and the server said no. Asserting on
    `payload["error"]` would pass a broken tool off as a protocol failure.

    Returns the whole error, because the CODE is the part worth asserting: every failure
    produces some error, so a test that only greps the message passes just as happily when
    a deliberate `validation` degrades into an unhandled `internal`.
    """
    result = payload.get("result", {})
    assert result.get("isError"), payload
    return result["structuredContent"]["error"]


# ---- reads ------------------------------------------------------------------------------
@pytest.mark.parametrize("view", ["completeness", "drift", "evidence", "close_report",
                                  "readiness", "lineage", "verdicts", "baseline"])
def test_every_acceptance_view_round_trips(client, mcp_headers, approved, view):
    out = _ok(_call(client, mcp_headers, "prd_acceptance",
                    {"prd_id": approved.key, "view": view}))
    assert out["view"] == view and isinstance(out["result"], dict)


def test_completeness_reports_what_has_nothing_delivered(client, mcp_headers, approved):
    out = _ok(_call(client, mcp_headers, "prd_acceptance",
                    {"prd_id": approved.key, "view": "completeness"}))["result"]
    assert out["absent"] == ["Judging"]


def test_the_close_report_reads_the_original_baseline(client, mcp_headers, approved):
    out = _ok(_call(client, mcp_headers, "prd_acceptance",
                    {"prd_id": approved.key, "view": "close_report"}))["result"]
    assert out["original_version"] == "v1.0"


def test_an_unknown_view_is_refused_before_dispatch(client, mcp_headers, approved):
    """Arguments are validated against the inputSchema before the handler runs, so a typo
    is a clear error rather than a surprising default."""
    err = _err(_call(client, mcp_headers, "prd_acceptance",
                     {"prd_id": approved.key, "view": "vibes"}))
    assert err["code"] == "validation" and "vibes" in err["message"]


# ---- writes ------------------------------------------------------------------------------
def test_a_verdict_citing_nothing_is_refused(client, mcp_headers, approved):
    """The rule that makes a verdict arguable, enforced at the agent surface rather than
    only in the service — an agent that never touches REST must still hit it."""
    err = _err(_call(client, mcp_headers, "submit_verdict",
                     {"prd_id": approved.key, "outcome": "pass", "citations": []}))

    # `validation`, not `internal`: the payload is malformed and an identical retry fails
    # identically. An unhandled exception would also produce an error, which is why the
    # code is asserted and not just the wording.
    assert err["code"] == "validation" and "cite" in err["message"].lower()


def test_a_verdict_citing_intent_is_accepted_and_stamped(client, mcp_headers, approved):
    out = _ok(_call(client, mcp_headers, "submit_verdict", {
        "prd_id": approved.key, "outcome": "not_delivered", "reasoning": "Nothing built.",
        "citations": [{"kind": "intent", "ref": "Judging"}]}))

    assert out["baseline_version"] == "v1.0" and out["self_signed"] is False
    listed = _ok(_call(client, mcp_headers, "prd_acceptance",
                       {"prd_id": approved.key, "view": "verdicts"}))["result"]["verdicts"]
    assert [v["outcome"] for v in listed] == ["not_delivered"]


def test_signing_your_own_claimed_work_is_flagged_not_refused(client, mcp_headers, db,
                                                              approved):
    """Flagged, never refused — on a solo project the signer and the implementer are the
    same person. The agent identity here is the key's own name, which is what `claim_next`
    stamps into `claimed_by`."""
    item = items_svc.create_item(db, title="Mine", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    items_svc.claim_item(db, item.id, AGENT)

    out = _ok(_call(client, mcp_headers, "submit_verdict", {
        "prd_id": approved.key, "outcome": "pass",
        "citations": [{"kind": "intent", "ref": "Judging"}]}))

    assert out["self_signed"] is True and out["self_signed_items"] == [item.key]


def test_requesting_a_rebaseline_reopens_the_grill_without_approving(client, mcp_headers,
                                                                     db, approved):
    out = _ok(_call(client, mcp_headers, "request_rebaseline", {
        "prd_id": approved.key, "reason_type": "learning",
        "reason": "The close rule bricks a default install."}))

    assert out["status"] == "review"
    assert out["pending_rebaseline"]["reason_type"] == "learning"


def test_closing_with_intent_nobody_decided_about_is_refused(client, mcp_headers, approved):
    err = _err(_call(client, mcp_headers, "close_prd",
                     {"prd_id": approved.key, "dispositions": []}))

    # `conflict`, not `validation`: the request is well-formed and the caller is permitted
    # — the PRD simply is not accounted for yet, and the message says what is outstanding.
    assert err["code"] == "conflict" and "Judging" in err["message"]


def test_closing_with_every_section_dispositioned_succeeds(client, mcp_headers, approved):
    out = _ok(_call(client, mcp_headers, "close_prd", {
        "prd_id": approved.key, "verdict": "shipped what mattered",
        "dispositions": [{"section": "Judging", "disposition": "deferred",
                          "reason": "Not for v1."}]}))

    assert out["mode"] == "mechanical" and "not assessed" in out["disclosure"]


def test_a_closed_prd_refuses_a_rebaseline_over_mcp(client, mcp_headers, approved):
    """Terminal means terminal at every surface, not just the one with a UI in front."""
    _ok(_call(client, mcp_headers, "close_prd", {
        "prd_id": approved.key,
        "dispositions": [{"section": "Judging", "disposition": "deferred", "reason": "no"}]}))

    err = _err(_call(client, mcp_headers, "request_rebaseline", {
        "prd_id": approved.key, "reason_type": "correction", "reason": "Oops."}))
    assert err["code"] == "conflict" and "successor" in err["message"]


# ---- scope gating ----------------------------------------------------------------------------
@pytest.fixture()
def other_project_key(client, auth):
    """A credential scoped to a DIFFERENT project. Without one, removing the scope gate on
    the write tools passes every test — the fixtures all use a key that is allowed."""
    client.post("/api/projects", json={"name": "Elsewhere", "tag": "ZZ"}, headers=auth)
    key = client.post("/api/api-keys", json={"name": "outsider", "project_id": "elsewhere"},
                      headers=auth).json()
    return {"X-API-Key": key["plaintext"]}


@pytest.mark.parametrize("tool,args", [
    ("submit_verdict", {"outcome": "pass",
                        "citations": [{"kind": "intent", "ref": "Judging"}]}),
    ("request_rebaseline", {"reason_type": "learning", "reason": "Because."}),
    ("close_prd", {"dispositions": []}),
])
def test_write_tools_refuse_a_prd_outside_the_keys_scope(client, other_project_key,
                                                         approved, tool, args):
    """Scope-gated like every other write tool, and checked BEFORE the service call — so a
    key without write scope cannot learn whether a PRD exists by reading the error."""
    err = _err(_call(client, other_project_key, tool, {"prd_id": approved.key, **args}))

    # `unauthorized` is the wire code for authz.Forbidden — authenticated but out of scope,
    # and distinct from validation so an agent can tell "fix your payload" from "retrying
    # will not help; you need a different key".
    assert err["code"] == "unauthorized"


# ---- the tool that is deliberately absent --------------------------------------------------
def test_there_is_no_invalidation_tool_because_the_hold_rides_on_the_item(client,
                                                                          mcp_headers, db,
                                                                          approved):
    """GRPH-242/312: a notice you have to remember to fetch is one you miss. The hold
    appears on every item an agent reads, so no tool exists — and none should be added."""
    from app.mcp_server import TOOLS

    assert not [t for t in TOOLS if "invalidat" in t["name"]]

    item = items_svc.create_item(db, title="In flight", project_id="core",
                                 prd_id=approved.id, prd_section="Judging")
    items_svc.claim_item(db, item.id, "agent:worker")
    prd_svc.request_rebaseline(db, approved, reason_type="learning", reason="Moved.",
                               requested_by="agent:t")
    prd_svc.update_prd(db, approved.id, body=BODY.replace("Classify each completed item "
                                                          "against the goal.", "Rewritten."))
    prd_svc.record_grill_turns(db, approved.id, [{"role": "user", "text": "New answer."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, approved.id, name, "resolved")
    prd_svc.sync_status(db, approved)

    out = _ok(_call(client, mcp_headers, "get_item_details", {"id": item.key}))
    assert out["intent_hold"]["baseline_version"] == "v1.1"
