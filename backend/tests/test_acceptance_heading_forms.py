"""Parenthetical and trailing-punctuation acceptance labels are clause lists (GRPH-1289).

`**Acceptance (D3):**`, `Acceptance (D3):` and `**Acceptance.**` parsed to nothing, and the
gate recorded "description states no acceptance clauses". The clauses were there. A
label-shaped line that still has no list under it is unreadable, not that same pass.
"""
from unittest import mock

import pytest

from app.db import SessionLocal
from app.services import fleet
from app.services import items as items_svc


def test_bold_parenthetical_colon_parses_two_clauses():
    """The repro. Two bullets under `**Acceptance (D3):**` are those two clauses."""
    clauses, unreadable = fleet.parse_acceptance(
        "**Acceptance (D3):**\n- clause one\n- clause two\n")
    assert clauses == ["clause one", "clause two"]
    assert unreadable == []


@pytest.mark.parametrize("label,bullets", [
    ("Acceptance (D3):", ["plain one", "plain two"]),
    ("**Acceptance.**", ["period one", "period two"]),
    ("__Acceptance (D3):__", ["under one"]),
    ("__Acceptance.__", ["under period"]),
    ("## Acceptance (D3):", ["heading one", "heading two"]),
    ("## Acceptance.", ["dotted heading"]),
])
def test_plain_parenthetical_and_bold_period_parse(label, bullets):
    """Plain `Acceptance (D3):` and `**Acceptance.**` (and the underscore and heading forms)."""
    body = f"{label}\n" + "".join(f"- {bullet}\n" for bullet in bullets)
    clauses, unreadable = fleet.parse_acceptance(body)
    assert clauses == bullets
    assert unreadable == []


def test_a_sentence_that_mentions_acceptance_parses_to_no_clauses():
    """The control. The word in a sentence is not a label, and this raises nothing."""
    clauses, unreadable = fleet.parse_acceptance("The acceptance of this is obvious.")
    assert clauses == []
    assert unreadable == []
    clauses, unreadable = fleet.parse_acceptance(
        "## Problem\n\nThe acceptance of this is obvious.\n\n- a list\n")
    assert clauses == []
    assert unreadable == []


def test_parenthetical_label_with_no_list_raises(client, auth):
    """`**Acceptance (D3)**` with no list is not an empty pass: sign_off raises.

    The gates before the acceptance check are mocked, the same way the filed-fix
    shape test reaches its own raise. What is under test is the raise itself.
    """
    proj = client.post("/api/projects", json={"name": "Heading forms"},
                       headers=auth).json()["id"]
    description = "**Acceptance (D3)**\n"
    clauses, unreadable = fleet.parse_acceptance(description)
    assert clauses == []
    assert unreadable
    assert any("Acceptance (D3)" in line for line in unreadable)

    with SessionLocal() as db:
        item = items_svc.create_item(
            db, title="unopened label", description=description, project_id=proj,
            status="next", effort=1)
        item.status = "review"
        item.built_by = "builder"
        db.commit()
        item_id = item.id
        with mock.patch.object(fleet, "_independent_of_author", return_value=True), \
             mock.patch.object(fleet, "needs_adversarial_evidence", return_value=False), \
             mock.patch.object(fleet, "require_reviewer_sabotage"), \
             mock.patch.object(fleet, "require_gap_followups"), \
             mock.patch.object(fleet, "require_reviewer_property"), \
             mock.patch.object(fleet, "require_no_human_clause"):
            with pytest.raises(fleet.UnreadableAcceptance, match="cannot read"):
                fleet.sign_off(db, item_id=item_id, agent_id="reviewer")
        db.rollback()
        fresh = db.get(type(item), item_id)
        assert fresh.status == "review"
