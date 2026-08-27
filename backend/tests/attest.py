"""The receipt that satisfies the completion gate, for tests that need an item `done`.

GRPH-543 made `done` refuse without a valid `attestation`. Most tests in this suite reach
`done` as SETUP — for PRD close, the platform judge, evidence rollups, dashboards — not
because completion is what they are asserting. They still need to get there.

**Deliberately not an autouse fixture.** The tempting version attests every item
automatically so nothing has to change; it is wrong, and wrong in the exact way this whole
line of work exists to end. A gate every test satisfies without asking would be a gate no
test exercises, and the default would then be untested — GRPH-466's shape at the level of
the suite. Every caller here opts in visibly, so a reader can see which tests are choosing
to skip past the gate and which are aiming at it.

Tests that assert the gate ITSELF live in `test_gated_completion.py` and must not use this.
"""
from __future__ import annotations

from app.services import items as items_svc

# Not a real revision, and it does not need to be: nothing resolves it, and the gate asks
# only that a receipt names one. A plausible-looking SHA would suggest otherwise.
TEST_COMMIT = "0" * 40


def attestation(*, adapter: str = "tests", commit: str = TEST_COMMIT,
                passed: bool = True, name: str = "suite_setup") -> dict:
    """One well-formed attestation receipt.

    `passed=False` produces a receipt that is structurally valid and does NOT satisfy the
    gate — the case a test needs when it is asserting the refusal rather than routing
    around it.
    """
    return {
        "kind": "attestation",
        "adapter": adapter,
        "commit": commit,
        "predicates": [{"name": name, "passed": passed,
                        "detail": "completed as test setup"}],
    }


def complete(db, item_id: str, **fields):
    """`update_item(status="done")` carrying the attestation the gate requires.

    Any `evidence` the caller passes is kept and the attestation appended, because several
    callers complete an item precisely to assert what its evidence then contains.
    """
    evidence = list(fields.pop("evidence", None) or [])
    evidence.append(attestation())
    return items_svc.update_item(db, item_id, status="done", evidence=evidence, **fields)


def complete_body(**fields) -> dict:
    """The JSON body for an HTTP completion — `PATCH /api/items/{id}`."""
    evidence = list(fields.pop("evidence", None) or [])
    evidence.append(attestation())
    return {"status": "done", "evidence": evidence, **fields}
