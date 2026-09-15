"""D9 — the adversarial evidence gate (GRPH-341 / PRD-17).

**Accept:** `sign_off` on an above-threshold item without adversarial evidence is refused,
naming what is missing. A reviewer that dispatches two opposing-lens critics and records their
receipts passes. A below-threshold item signs off without one. The refusal is in the ledger.

The argument for a precondition rather than a practice: **reviewer and adversary are different
jobs and must not become one habit.** A reviewer CONVERGES — the queue is three deep, and an
agent that blocks everything is a bad reviewer. An adversary DIVERGES — the job is one more
failure mode, and finding nothing is failure. Merge them and the convergent incentive wins
under queue pressure, which is the audit pack's self-congratulation problem moved one seat
over.

The threshold is not a convenience. `a gate nobody satisfies is a gate people route around` is
what kept GRPH-321 parked, and firing on a one-line fix is exactly how a gate earns that. Below
the threshold, agent-distinct review is sufficient on its own.
"""
import pytest

from app.models import Event, Item
from app.services import fleet


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _ok(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Adversarial"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "adv", "project_id": proj},
                       headers=auth).json()["plaintext"]


SABOTAGE = {"kind": "sabotage", "claim": "the veto holds back an accept",
            "mutation": "return True from may_auto_publish", "tests_failed": 2}


def _ready_for_review(client, key, effort):
    """An item built by one agent and waiting for another — the state a reviewer acts on."""
    worker = _ok(client, key, "register_agent",
                 {"label": "w", "capabilities": {"instance": "w"}})
    made = _ok(client, key, "create_item",
               {"title": "some work", "status": "next", "effort": effort})
    c = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    _ok(client, key, "update_item",
        {"id": c["item"]["id"], "status": "review", "agent_id": worker["agent_id"]})
    reviewer = _ok(client, key, "register_agent",
                   {"label": "r", "role_hint": "reviewer", "capabilities": {"instance": "r"}})
    return c["item"]["id"], reviewer


# ---- the gate ---------------------------------------------------------------------------------

def test_substantial_work_cannot_be_signed_off_unchallenged(client, key, db):
    """THE criterion. Without it the reviewer role is a second opinion and nothing more — and
    a second opinion under queue pressure converges on yes."""
    item, reviewer = _ready_for_review(client, key, effort=5)

    res = _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict", "permitted, just not accounted for"
    assert "sabotage" in err["message"] and "tests_failed" in err["message"]
    assert err["hint"], "and it says how to satisfy it"
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_a_sabotage_receipt_lets_it_through(client, key):
    item, reviewer = _ready_for_review(client, key, effort=5)

    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "evidence": [SABOTAGE]})

    assert out["status"] == "done"


def test_two_opposing_lenses_are_two_receipts(client, key):
    """The shape the PRD describes: subagents with opposing lenses, each recording its own
    receipt. Adversarial multiplicity does not need a fourth fleet role competing for the
    human's attention."""
    item, reviewer = _ready_for_review(client, key, effort=8)

    out = _ok(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [
            dict(SABOTAGE, claim="the reservation blocks a colliding claim"),
            dict(SABOTAGE, claim="the pin lapses", mutation="never expire", tests_failed=1),
        ],
    })

    assert out["status"] == "done"


def test_trivial_work_is_not_taxed(client, key):
    """A gate that fires on a one-line fix is a gate people route around — the AL-96 trust
    failure, and the objection that kept GRPH-321 parked. The threshold answers it directly:
    the cheapest way to satisfy this gate is never to avoid it."""
    item, reviewer = _ready_for_review(client, key, effort=1)

    out = _ok(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    assert out["status"] == "done"


def test_a_sabotage_that_broke_nothing_does_not_satisfy_it(client, key):
    """The distinction the whole receipt exists for. `tests_failed: 0` means the TEST cannot
    fail — evidence the guard is absent. Accepting it would let precisely the condition the
    gate detects satisfy the gate."""
    item, reviewer = _ready_for_review(client, key, effort=5)

    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [dict(SABOTAGE, tests_failed=0)]})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict"
    assert "broke NOTHING" in err["message"], "and it says which way it failed"


def test_prose_claiming_a_sabotage_does_not_satisfy_it(client, key):
    """If a `note` saying "I sabotaged it" passed, the structure would be decorative and the
    gate would be checking a sentence."""
    item, reviewer = _ready_for_review(client, key, effort=5)

    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [{"kind": "note", "detail": "ran six sabotages, all caught"}]})

    assert res["structuredContent"]["error"]["code"] == "conflict"


def test_the_workers_own_receipts_count(client, key, db):
    """Adversarial evidence is adversarial evidence whoever recorded it. Requiring the reviewer
    to re-run sabotages the author already recorded would be tax rather than rigour — and the
    author is better placed to break their own claim."""
    item, reviewer = _ready_for_review(client, key, effort=5)
    _ok(client, key, "update_item", {"id": item, "evidence": [SABOTAGE]})

    out = _ok(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    assert out["status"] == "done"


def test_the_refusal_is_in_the_ledger(client, key, db):
    """A gate whose refusals leave no trace cannot be audited for whether it is being routed
    around — which is the exact failure mode it was parked over."""
    item, reviewer = _ready_for_review(client, key, effort=5)

    _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    ev = db.query(Event).filter(
        Event.action == "sign_off_refused").order_by(Event.id.desc()).first()
    assert ev is not None, "the refusal left no trace"
    assert "sabotage" in ev.meta["reason"]
    assert ev.meta["principal"]["id"], "and names the human behind the key"


def test_the_threshold_is_a_named_constant(client, key):
    """Pick one number and let somebody hit it, rather than a per-project slider nobody tunes
    and everybody sets to infinity the first time it is inconvenient."""
    assert fleet.ADVERSARIAL_EFFORT_THRESHOLD == 3
    assert fleet.needs_adversarial_evidence(Item(effort=3)) is True
    assert fleet.needs_adversarial_evidence(Item(effort=2)) is False
    assert fleet.needs_adversarial_evidence(Item(effort=None)) is False


# ---- probe attestations satisfy the gate (GRPH-623) -------------------------------------------

PROBE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _probe_attestation(*, passed: bool):
    return {"kind": "attestation", "adapter": "mutation-probe", "commit": PROBE_SHA,
            "predicates": [{"name": "sabotage_observed", "passed": passed,
                            "detail": "4 test(s) failed" if passed else "broke NOTHING"}]}


def test_a_probe_attestation_lets_a_substantial_item_through(client, key, auth, proj):
    """THE FIX (GRPH-623). A probe that OBSERVED tests failing is the same measurement as a
    sabotage receipt with tests_failed > 0, from a different adapter. sign_off must accept it."""
    item, reviewer = _ready_for_review(client, key, effort=5)
    gate_key = client.post("/api/api-keys", json={"name": "gate", "project_id": proj,
                           "scopes": ["read", "write", "gate"]},
                           headers=auth).json()["plaintext"]
    _ok(client, gate_key, "update_item",
        {"id": item, "evidence": [_probe_attestation(passed=True)]})

    out = _ok(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    assert out["status"] == "done"


def test_a_failing_probe_attestation_does_not_let_it_through(client, key, auth, proj):
    """Direction two. A probe that broke nothing must not satisfy the gate — same property
    as a sabotage receipt with tests_failed=0."""
    item, reviewer = _ready_for_review(client, key, effort=5)
    gate_key = client.post("/api/api-keys", json={"name": "gate", "project_id": proj,
                           "scopes": ["read", "write", "gate"]},
                           headers=auth).json()["plaintext"]
    _ok(client, gate_key, "update_item",
        {"id": item, "evidence": [_probe_attestation(passed=False)]})

    res = _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict"
    assert "adversarial" in err["message"].lower() or "sabotage" in err["message"].lower()


def test_the_minted_attestation_says_probe_or_sabotage(client, key, auth, proj):
    """The sign_off attestation records WHY adversarial evidence passed. With the probe path
    open, the detail must say so — a later reader must not assume it was a sabotage receipt."""
    item, reviewer = _ready_for_review(client, key, effort=5)
    gate_key = client.post("/api/api-keys", json={"name": "gate", "project_id": proj,
                           "scopes": ["read", "write", "gate"]},
                           headers=auth).json()["plaintext"]
    _ok(client, gate_key, "update_item",
        {"id": item, "evidence": [_probe_attestation(passed=True)]})

    out = _ok(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"], "commit": PROBE_SHA})

    from app.services import items as items_svc
    atts = items_svc.valid_attestations(out["evidence"], commit=PROBE_SHA)
    sign_off_att = next(a for a in atts if a.get("adapter") == "fleet.sign_off")
    adv = next(p for p in sign_off_att["predicates"] if p["name"] == "adversarial_evidence")
    assert "probe attestation" in adv["detail"], \
        f"the receipt does not mention the probe path: {adv['detail']}"


# ---- acceptance coverage gate (GRPH-884) -----------------------------------------------------

DESC_TWO_CLAUSES = """\
## Problem

Something needs doing.

## Tests

- the veto blocks an accept
- the pin lapses after timeout
"""

DESC_ONE_CLAUSE = """\
## Tests

- the veto blocks an accept
"""


def _ready_with_description(client, key, *, effort, description):
    """Like `_ready_for_review` but stamps a description on the item at creation."""
    worker = _ok(client, key, "register_agent",
                 {"label": "w", "capabilities": {"instance": "w"}})
    made = _ok(client, key, "create_item",
               {"title": "some work", "status": "next", "effort": effort,
                "description": description})
    c = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    _ok(client, key, "update_item",
        {"id": c["item"]["id"], "status": "review", "agent_id": worker["agent_id"]})
    reviewer = _ok(client, key, "register_agent",
                   {"label": "r", "role_hint": "reviewer",
                    "capabilities": {"instance": "r"}})
    return c["item"]["id"], reviewer


def test_uncovered_acceptance_clause_is_refused(client, key):
    """THE criterion (GRPH-884). Two acceptance clauses, one named test, two sabotage receipts.
    Sabotage receipts do NOT substitute for a missing test — that is the defect this closes."""
    item, reviewer = _ready_with_description(
        client, key, effort=5, description=DESC_TWO_CLAUSES)

    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [
            {"kind": "test", "detail": "the veto blocks an accept"},
            SABOTAGE,
            dict(SABOTAGE, claim="the pin lapses", mutation="never expire", tests_failed=1),
        ]})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict", err
    assert "pin lapses after timeout" in err["message"], \
        f"the refusal does not name the uncovered clause: {err['message']}"
    assert "acceptance" in err["message"].lower() or "clause" in err["message"].lower()
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_all_clauses_named_lets_it_through(client, key):
    """The control. Both clauses have a named test, adversarial evidence is present — the
    item proceeds. The acceptance gate does not add tax when its condition is met."""
    item, reviewer = _ready_with_description(
        client, key, effort=5, description=DESC_TWO_CLAUSES)

    out = _ok(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [
            {"kind": "test", "detail": "the veto blocks an accept"},
            {"kind": "test", "detail": "the pin lapses after timeout"},
            SABOTAGE,
        ]})

    assert out["status"] == "done"


def test_below_threshold_item_with_clauses_is_still_refused(client, key):
    """The acceptance gate is independent of the effort threshold. A trivial item with
    acceptance clauses still needs them covered — the D9 effort gate stays, and this gate
    fires on its own condition (clauses present) regardless of effort."""
    item, reviewer = _ready_with_description(
        client, key, effort=1, description=DESC_TWO_CLAUSES)

    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [
            {"kind": "test", "detail": "the veto blocks an accept"},
        ]})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict", err
    assert "pin lapses after timeout" in err["message"]


def test_item_without_tests_section_is_unaffected(client, key):
    """Items that predate the `## Tests` convention have no clauses, so the gate does not
    fire. This is what keeps the existing ~71 test setup paths working."""
    item, reviewer = _ready_for_review(client, key, effort=5)

    out = _ok(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [SABOTAGE]})

    assert out["status"] == "done"


def test_attestation_records_acceptance_coverage(client, key, auth, proj):
    """The attestation predicate records pass/fail with the clause count, not a receipt count.
    A later reader must be able to tell whether the gate checked anything."""
    item, reviewer = _ready_with_description(
        client, key, effort=5, description=DESC_TWO_CLAUSES)
    gate_key = client.post("/api/api-keys", json={"name": "gate", "project_id": proj,
                           "scopes": ["read", "write", "gate"]},
                           headers=auth).json()["plaintext"]
    _ok(client, gate_key, "update_item", {"id": item, "evidence": [
        {"kind": "test", "detail": "the veto blocks an accept"},
        {"kind": "test", "detail": "the pin lapses after timeout"},
        SABOTAGE,
    ]})

    out = _ok(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"], "commit": PROBE_SHA})

    from app.services import items as items_svc
    atts = items_svc.valid_attestations(out["evidence"], commit=PROBE_SHA)
    sign_off_att = next(a for a in atts if a.get("adapter") == "fleet.sign_off")
    cov = next(p for p in sign_off_att["predicates"] if p["name"] == "acceptance_coverage")
    assert cov["passed"] is True
    assert "2 acceptance clause(s)" in cov["detail"]


def test_sabotage_receipts_do_not_cover_a_clause(client, key):
    """The defect, pinned. Two sabotage receipts and zero test evidence must NOT satisfy the
    gate — sabotage can only mutate code some test already reaches, so a clause with no test
    is invisible to it."""
    item, reviewer = _ready_with_description(
        client, key, effort=5, description=DESC_ONE_CLAUSE)

    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [
            SABOTAGE,
            dict(SABOTAGE, claim="the pin lapses", mutation="never expire", tests_failed=1),
        ]})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict", err
    assert "veto blocks an accept" in err["message"], \
        f"the refusal does not name the uncovered clause: {err['message']}"


def test_the_call_is_load_bearing():
    """Sabotage the CALL. A helper with unit tests is not the gate — deleting the check from
    sign_off must make the suite fail. This test reads the source and asserts the call exists.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "services" /
           "fleet.py").read_text()
    assert "acceptance_covered(clauses, merged)" in src, (
        "sign_off no longer calls acceptance_covered — the gate is a helper with tests "
        "that nothing calls, which is the defect GRPH-884 exists to close"
    )
    assert "MissingAcceptanceCoverage" in src, (
        "the refusal exception is missing — sign_off cannot refuse uncovered clauses"
    )
