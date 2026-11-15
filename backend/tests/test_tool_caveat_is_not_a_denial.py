"""Honest disclosure of an uncommitted verification artefact is not a clause denial (GRPH-1019).

GRPH-1005's builder wrote that its clause was delivered, verified by a claim checker that was
not committed, and that the checker "cannot resolve" four literals from a checkout. Two
reviewers could not sign the merged, green item: `cannot` read as the clause being undone.
A builder who left the sentence out would have passed. The distinction this pins is WHO the
denial is about — the deliverable or the tool — not how far from the clause it sits.

Sabotage: make `_about_the_tool` return False and the same-sentence case goes red (the
others are already outside the clause's sentence scope; they pin that it stays that way).
"""
import pytest

from app.services.fleet import DENIAL_RULE, acceptance_covered, denial_in

CLAUSE = "the §1.3 table is published"


@pytest.mark.parametrize("receipt", [
    # Same sentence as the clause.
    f"Clause {CLAUSE}: delivered; the claim checker that verified it is not committed and "
    "cannot resolve four design literals from a checkout.",
    # The GRPH-1005 shape: the disclosure sentences away.
    f"Clause {CLAUSE}: delivered, verified by a doc-driven claim checker, 36 checks, all "
    "resolve. It is not committed (a new test file is outside this item's touchpoints); "
    "the four design-project literals it cannot resolve from a checkout are allowlisted.",
    # A terse sentence right after the clause, which `_clause_scope` keeps.
    f"{CLAUSE} is implemented. The uncommitted script cannot run in CI.",
])
def test_a_disclosed_uncommitted_tool_is_not_a_denial(receipt):
    assert denial_in(CLAUSE, receipt) == "", receipt
    ok, uncovered, contradicted = acceptance_covered(
        [CLAUSE], [{"kind": "test", "detail": receipt}])
    assert ok and not uncovered and not contradicted, (uncovered, contradicted)


@pytest.mark.parametrize("receipt", [
    f"Clause {CLAUSE} — NOT DELIVERED; the checker is not committed either.",
    f"Clause {CLAUSE}: the table cannot be published yet. The checker is uncommitted, "
    "though the rest is delivered.",
    f"Clause {CLAUSE}: delivered, but I skipped the Postgres run. The checker is not "
    "committed.",
    # A bare `it` in the sentence that claims delivery may be the deliverable: ambiguous,
    # so it refuses (review bounce on 502bdf96).
    f"Clause {CLAUSE}: delivered; the uncommitted checker passed, but it cannot be "
    "published yet.",
    f"{CLAUSE} is implemented, the uncommitted script ran, but it cannot be published yet.",
    f"Clause {CLAUSE}: delivered; the checker is not committed and it cannot be published "
    "yet.",
    # A named deliverable between the tool and the denial word is the denial's subject,
    # whatever tool noun shares its phrase (review bounce on 872a95b9).
    f"Clause {CLAUSE}: delivered; the uncommitted checker passed but the table cannot be "
    "published yet.",
    f"Clause {CLAUSE}: delivered; the uncommitted checker shows the table cannot be "
    "published yet.",
    f"Clause {CLAUSE}: delivered; per the uncommitted checker the table cannot be "
    "published yet.",
    # The same with no determiner: a clause-opening conjunction or a reporting verb.
    f"Clause {CLAUSE}: delivered; the uncommitted checker passed but tables cannot be "
    "published yet.",
    f"Clause {CLAUSE}: delivered; the uncommitted checker shows tables cannot be published.",
    # A relative clause on a pronoun does not make the tool the subject.
    f"Clause {CLAUSE}: delivered; the checker is uncommitted, the table it reads cannot be "
    "published.",
    # A tool that FAILED on or SKIPPED the deliverable is negative evidence about the
    # clause, not a caveat about the tool: only `cannot` is exempt (review bounce on
    # 77a6b5cc).
    f"Clause {CLAUSE}: delivered; the uncommitted checker failed on the table.",
    f"Clause {CLAUSE}: delivered; the uncommitted checker skipped the table.",
    # A bare noun after the tool noun is a compound whose head is the real subject.
    f"Clause {CLAUSE}: delivered; the uncommitted checker tables cannot be published.",
    f"Clause {CLAUSE}: delivered; the uncommitted checker table cannot be published.",
    f"Clause {CLAUSE}: delivered; the uncommitted script table cannot be published.",
])
def test_a_receipt_that_really_denies_its_clause_still_refuses(receipt):
    """The control. A disclosure is not a pass: a denial about the DELIVERABLE in the same
    receipt still convicts, whatever else the receipt discloses."""
    assert denial_in(CLAUSE, receipt) != "", receipt
    ok, _, contradicted = acceptance_covered([CLAUSE], [{"kind": "test", "detail": receipt}])
    assert not ok and CLAUSE in contradicted


def test_a_tool_caveat_without_a_delivery_claim_still_denies():
    """Disclosing the tool is not enough: the receipt must say the clause IS delivered."""
    receipt = (f"Clause {CLAUSE}: the uncommitted checker cannot resolve four literals "
               "from a checkout.")
    assert denial_in(CLAUSE, receipt) == "cannot"


def test_a_tool_caveat_without_an_uncommitted_disclosure_still_denies():
    """A checker that cannot run, with nothing disclosed as left out of the change, is a
    verification that did not happen — still a denial."""
    receipt = f"Clause {CLAUSE}: delivered; the checker cannot run here."
    assert denial_in(CLAUSE, receipt) == "cannot"


def test_the_refusal_names_what_to_write_instead(client, auth):
    """Where the gate still fires, the refusal says what an honest builder should have
    written: the clause delivered, the tool named, or the caveat moved to a note."""
    from app.db import SessionLocal
    from app.services import fleet as fleet_svc
    from app.services import items as items_svc

    receipt = f"Clause {CLAUSE}: the checker cannot resolve four literals."
    proj = client.post("/api/projects", json={"name": "ToolCaveat"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "tc", "project_id": proj},
                      headers=auth).json()["plaintext"]
    db = SessionLocal()
    try:
        it = items_svc.create_item(db, project_id=proj, title="a slice", effort=1,
                                   description=f"## Acceptance\n\n- {CLAUSE}\n")
        items_svc.update_item(db, item_id=it.id, status="review",
                              evidence=[{"kind": "test", "detail": receipt}])
        with pytest.raises(fleet_svc.MissingAcceptanceCoverage) as caught:
            fleet_svc.sign_off(db, item_id=it.id, agent_id="GRPH-AX", api_key=key,
                               evidence=[{"kind": "test", "detail": receipt}])
    finally:
        db.close()
    msg = str(caught.value)
    assert DENIAL_RULE in msg
    assert "verification tool" in msg and "not committed" in msg and "{kind: note}" in msg, msg
