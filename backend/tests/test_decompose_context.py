"""GRPH-261 — a decomposed task has to carry its own spec.

`decompose_prd` lifted one section body verbatim, which reads correctly inside a document
and incompletely outside one. Every rule a PRD states — an invariant, a charset, a set of
assigned values — lives in framing prose that the implementable sections assume the reader
has already seen.

On PRD-13 that was literal: the ID grammar, the tag charset and the five tag assignments
all lived in `## Context`, none of them reached the six items meant to implement them, and
all six were hand-rewritten by hand to be self-contained.

The bar is the item's own: **an agent given only `get_item_details` has every value it
needs, with no dangling cross-references and no requirement to read the parent PRD.**
"""
import pytest

from app.services import prds

BODY = """## 1. Overview

The tag charset is `^[A-Z][A-Z0-9]{1,3}$` — two to four characters, uppercase.

## 2. Goals

- Identity is frozen before anything renders it.

## D1 — The validator

Reject a tag that does not match the charset. Assign the five tags above.

## D2 — The mint path

Enforce it at mint time, even if the path below is bypassed.

## 7. Risks

Reusing a retired tag makes an old key ambiguous.
"""


@pytest.fixture()
def proposals(client):
    """The REAL `decompose`, on a real PRD row.

    An earlier draft of this file assembled the description itself and asserted on that.
    It passed, and it would have kept passing with `decompose` deleted — a test that
    reimplements the code under test is worse than no test, because it reports the
    reimplementation as working software.
    """
    from app.db import SessionLocal
    from app.services import prds as prds_svc

    db = SessionLocal()
    try:
        prd = prds_svc.create_prd(db, project_id="core", title="Identity", body=BODY)
        db.commit()
        return prds_svc.decompose(db, prd, create=False)["proposals"], prd
    finally:
        db.close()


def test_framing_context_carries_the_rules_and_not_the_work():
    class P:
        id, title, body, project_id = "GRPH-P99", "Identity", BODY, "core"

    ctx = prds.framing_context(P())

    # The rule an implementer cannot build without.
    assert "^[A-Z][A-Z0-9]{1,3}$" in ctx
    assert "Identity is frozen" in ctx
    assert "Reusing a retired tag" in ctx

    # The other implementable sections are their own items; duplicating them would make
    # every task a copy of the whole PRD.
    assert "Reject a tag that does not match" not in ctx
    assert "Enforce it at mint time" not in ctx


def test_a_decomposed_task_is_self_contained(proposals):
    """The `done when` of GRPH-261, asserted against what decompose actually produces."""
    props, prd = proposals
    d1 = next(p for p in props if p["section"].startswith("D1"))

    assert "Reject a tag that does not match" in d1["description"], "its own work comes first"
    assert "^[A-Z][A-Z0-9]{1,3}$" in d1["description"], (
        "the charset lives in framing prose; without it this task cannot be built"
    )
    assert prd.id in d1["description"], "the task says which spec it belongs to"


def test_dangling_references_are_reported_not_guessed(proposals):
    """'The five tags above' could be repaired by guessing which five, and a wrong guess
    is worse than a visible dangle: the reader who sees 'above' knows to go looking."""
    props, _ = proposals
    d1 = next(p for p in props if p["section"].startswith("D1"))
    d2 = next(p for p in props if p["section"].startswith("D2"))

    assert any("above" in r for r in d1["dangling_refs"])
    assert any("below" in r for r in d2["dangling_refs"])
    # Not rewritten — the original words survive so the dangle stays visible.
    assert "the five tags above" in d1["description"]


def test_framing_sections_do_not_become_their_own_items(proposals):
    """The context is carried, not promoted. Overview and Risks are still framing."""
    props, _ = proposals
    sections = {p["section"] for p in props}
    assert not any(s.startswith("1.") or s.startswith("2.") or s.startswith("7.")
                   for s in sections), sections


def test_a_prd_with_no_framing_sections_adds_no_context_block():
    """A PRD that is all work gets its sections verbatim, exactly as before. The context
    block must not appear as an empty heading nobody can act on."""
    class Bare:
        id, title, project_id = "GRPH-P98", "Bare", "core"
        body = "## D1 — Only work\n\nBuild the thing."

    assert prds.framing_context(Bare()) == ""
