"""A structured `sabotage` evidence kind (GRPH-321).

`_EVIDENCE_KINDS` was `test | url | screenshot | health | note`, so a sabotage result — the
deliberate reversion of one behaviour to prove a test actually fails — had nowhere to go but
`note`. It landed as free text nothing could query, aggregate, or check.

**Graphban owns the receipt, not the run.** Running a mutation needs the repo, a test command,
and the ability to edit and revert source, all agent-side; `mutmut` and Stryker already do that
far better than anything here would. The server cannot verify the mutation happened, which
makes the claim *falsifiable* rather than *true* — the same trade already accepted for
citations.

The load-bearing distinction, and the reason this is worth structure at all:

    tests_failed >= 1  the claim is guarded — the mutation broke something
    tests_failed == 0  the TEST is broken — it cannot fail, which is a FINDING

Those look identical as prose and lead to opposite actions. In the session that motivated this
item, two sabotages came back at zero: one because the mutation string did not match the real
source, one because the test was pointed at a seam adjacent to the claim it named. Both were
caught by a human reading output. Neither would have been queryable afterwards.
"""
import pytest

from app.services.items import (has_effective_sabotage, normalize_evidence,
                                sabotage_receipts, vacuous_sabotages)


def _sab(**over):
    base = {"kind": "sabotage", "claim": "the veto holds back an accept",
            "mutation": "return True from may_auto_publish", "tests_failed": 2}
    base.update(over)
    return base


# ---- the receipt ------------------------------------------------------------------------------

def test_a_structured_receipt_keeps_its_structure():
    out = normalize_evidence([_sab()])[0]

    assert out["kind"] == "sabotage"
    assert out["claim"] == "the veto holds back an accept"
    assert out["mutation"] == "return True from may_auto_publish"
    assert out["tests_failed"] == 2


def test_an_unstructured_sabotage_is_demoted_to_a_note():
    """THE property. A structured kind that accepts unstructured input is the free-text field
    with a stronger name, and any gate reading it would be checking a label rather than a
    fact."""
    out = normalize_evidence([{"kind": "sabotage", "detail": "I sabotaged it, trust me"}])[0]

    assert out["kind"] == "note"
    assert out["detail"] == "I sabotaged it, trust me", "the prose survives the demotion"


@pytest.mark.parametrize("missing", ["claim", "mutation", "tests_failed"])
def test_every_field_is_required(missing):
    out = normalize_evidence([_sab(**{missing: None})])[0]

    assert out["kind"] == "note"
    # Demoted, never dropped. An incomplete receipt with no `detail` used to hit the
    # empty-receipt drop and vanish outright — the agent recorded a finding and the server
    # discarded it silently, which is the worst of the three possible outcomes.
    assert "incomplete sabotage receipt" in out["detail"]


def test_a_boolean_is_not_a_count():
    """`True` is an int in Python and would sail through an `isinstance(x, int)` check as
    `tests_failed=1` — a receipt asserting a test failed because somebody passed a flag."""
    assert normalize_evidence([_sab(tests_failed=True)])[0]["kind"] == "note"


def test_a_receipt_reads_as_prose_too():
    """The item view and the ledger render `detail`. A sabotage that showed there as an empty
    string would be invisible on every human surface it was meant to inform."""
    out = normalize_evidence([_sab(detail="")])[0]

    assert "2 test(s) failed" in out["detail"]
    assert "the veto holds back an accept" in out["detail"]


def test_a_vacuous_receipt_says_so_in_its_prose():
    """It has to read differently at a glance, not only under a query — a human scanning the
    item is the first reader, and "0 test(s) failed" scans as a pass."""
    out = normalize_evidence([_sab(tests_failed=0, detail="")])[0]

    assert "NOTHING failed" in out["detail"]


# ---- what a gate may count ---------------------------------------------------------------------

def test_zero_failures_is_recorded_but_never_satisfies():
    """The whole point. A sabotage nothing failed under is evidence the guard is ABSENT, so
    counting it would let the exact condition it detects satisfy the check that exists to
    detect it."""
    ev = normalize_evidence([_sab(tests_failed=0)])

    assert sabotage_receipts(ev), "it is still recorded — the finding matters"
    assert vacuous_sabotages(ev), "and it is findable as a finding"
    assert has_effective_sabotage(ev) is False, "but it proves nothing about the claim"


def test_one_effective_receipt_among_several_is_enough():
    ev = normalize_evidence([_sab(tests_failed=0), _sab(claim="another", tests_failed=3)])

    assert has_effective_sabotage(ev) is True
    assert len(vacuous_sabotages(ev)) == 1, "the vacuous one is still visible"


def test_other_evidence_kinds_do_not_count_as_sabotage():
    """A test-run summary saying "I sabotaged five things" is prose. If it satisfied the gate,
    the structure would be decorative."""
    ev = normalize_evidence([{"kind": "test", "detail": "six sabotages, six failures"}])

    assert sabotage_receipts(ev) == [] and has_effective_sabotage(ev) is False


def test_existing_evidence_is_untouched():
    """Every receipt written before this item keeps its shape — the kind is additive."""
    ev = normalize_evidence([
        {"kind": "test", "detail": "1620 passed"},
        {"kind": "url", "detail": "PR #179", "url": "https://example/pr/179"},
    ])

    assert [e["kind"] for e in ev] == ["test", "url"]
    assert all(set(e) == {"kind", "detail", "url"} for e in ev), "no new keys on old kinds"


# ---- probe attestations satisfy the gate (GRPH-623) ---------------------------------------------

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _probe_attestation(*, passed: bool, commit: str = SHA):
    return {"kind": "attestation", "adapter": "mutation-probe", "commit": commit,
            "predicates": [{"name": "sabotage_observed", "passed": passed,
                            "detail": "4 test(s) failed" if passed else "broke NOTHING"}]}


def test_a_passing_probe_attestation_satisfies_the_gate():
    """The fix (GRPH-623). A probe that OBSERVED tests failing is the same measurement as a
    sabotage receipt with tests_failed > 0, from a different adapter. The gate must read it."""
    ev = normalize_evidence([_probe_attestation(passed=True)])

    assert has_effective_sabotage(ev) is True, \
        "a probe that observed failures must satisfy the adversarial gate"


def test_a_failing_probe_attestation_does_not_satisfy_the_gate():
    """Direction two. A probe that broke nothing is evidence the guard is absent, so counting
    it would let the exact condition it detects satisfy the check that exists to detect it."""
    ev = normalize_evidence([_probe_attestation(passed=False)])

    assert has_effective_sabotage(ev) is False, \
        "a probe that broke nothing must not satisfy the gate"


def test_a_probe_on_a_receipt_with_another_failure_does_not_count():
    """`valid_attestations` drops the whole receipt when ANY predicate failed, so a
    `sabotage_observed` riding on an attestation with a failing sibling has not been
    attested — it has been contradicted. Same property `attested_predicates` already holds
    for every other name; this asserts the probe is not special-cased around it."""
    ev = normalize_evidence([{
        "kind": "attestation", "adapter": "mutation-probe", "commit": SHA,
        "predicates": [
            {"name": "sabotage_observed", "passed": True, "detail": "4 failed"},
            {"name": "suite_green", "passed": False, "detail": "3 failed"},
        ],
    }])

    assert has_effective_sabotage(ev) is False, \
        "sabotage_observed on a receipt with a failing sibling must not count"


def test_a_sabotage_receipt_and_a_probe_attestation_both_satisfy():
    """Either path alone is enough; both together is still enough. Asserts the OR, not XOR."""
    ev = normalize_evidence([
        _sab(tests_failed=2),
        _probe_attestation(passed=True),
    ])

    assert has_effective_sabotage(ev) is True


def test_a_probe_attestation_at_the_wrong_commit_does_not_count():
    """Staleness composes with the gate. `attested_predicates` with commit=None asks 'was
    this ever attested'; the sign_off gate passes the full evidence without commit filtering,
    so this test pins that a stale probe cannot sneak through via has_effective_sabotage."""
    ev = normalize_evidence([_probe_attestation(passed=True, commit="b" * 40)])

    # has_effective_sabotage does NOT filter by commit — it asks whether the item carries
    # the measurement at all. The commit gate is elsewhere (valid_attestations(commit=...)).
    # This test pins the behaviour explicitly so a later change cannot silently narrow it.
    assert has_effective_sabotage(ev) is True, \
        "has_effective_sabotage is commit-agnostic; staleness is gated elsewhere"


# ---- over MCP -------------------------------------------------------------------------------------

def test_a_sabotage_receipt_survives_a_round_trip(client, auth):
    """The seam. Asserting on `normalize_evidence` alone would pass just as well if the schema
    never advertised the fields and every agent's receipt were silently demoted."""
    raw = client.post("/api/api-keys", json={"name": "sab"}, headers=auth).json()["plaintext"]

    def mcp(tool, args):
        return client.post(
            "/api/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": tool, "arguments": args}},
            headers={"X-API-Key": raw},
        ).json()["result"]["structuredContent"]

    made = mcp("create_item", {"title": "sabotage round trip"})
    out = mcp("update_item", {"id": made["id"], "evidence": [_sab()]})

    receipt = out["evidence"][0]
    assert receipt["kind"] == "sabotage"
    assert receipt["tests_failed"] == 2 and receipt["claim"]


def test_the_schema_advertises_the_fields(client):
    """An agent writes what the schema tells it to. Fields the server accepts but never
    mentions are fields nothing sends."""
    from app.mcp_server import _SCHEMA_BY_NAME

    props = _SCHEMA_BY_NAME["update_item"]["properties"]["evidence"]["items"]["properties"]
    assert "sabotage" in props["kind"]["enum"]
    for field in ("claim", "mutation", "tests_failed"):
        assert field in props, f"the schema never mentions {field}"
    assert "ZERO" in props["tests_failed"]["description"], \
        "the zero-is-a-finding warning belongs where the agent reads it"
