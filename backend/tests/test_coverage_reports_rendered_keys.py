"""Coverage and decompose report the RENDERED key, not the frozen id (GRPH-319 follow-on).

`Prd.id` is frozen at issue time and never rewritten — that is the whole design of PRD-13, and
it is why nine unchecked columns holding an entity id do not have to be hand-corrected on every
rename. The cost is that the stored id stops matching the project's tag the moment it changes:
on the live instance `AL-P14` and `AL-P15` still carry a tag the project no longer holds, and
`PRD-1`..`PRD-13` carry no project marker at all.

Every return in `services/prds.py` renders — except two, in the same dicts as an `item_ids`
field that was already fixed for exactly this reason, with the reason written above it.

**Only a retagged project exposes this**, which is why it survived. On a fresh instance the
stored id and the rendered key are the same string, so an assertion over either passes and
proves nothing. Every test here retags first.

The last test is the control, and it took two attempts. The first version asserted that
`decompose(create=True)` stores a frozen id, then SURVIVED the sabotage that changed its call
site to pass `prd.key` — because `items_svc.create_item` normalises through `_stored_prd_id`,
so both forms store the frozen id and the assertion could not fail. It is now asserted where
the invariant actually lives.
"""
import pytest

from app.services import items as items_svc
from app.services import prds as prd_svc
from app.services import projects as proj_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def retagged(db):
    """A project whose tag has MOVED, so a frozen id and a rendered key differ."""
    prd = prd_svc.create_prd(
        db, title="Subject", project_id="core",
        body="# Subject\n\n## Overview\n\nframing.\n\n## Build the thing\n\ndo it.\n",
    )
    frozen = prd.id
    proj_svc.retag_project(db, "core", "ZZZ")
    db.refresh(prd)
    assert prd.key != frozen, "the fixture did not actually produce a divergence"
    return prd, frozen


def test_coverage_reports_the_current_tag(db, retagged):
    prd, frozen = retagged

    out = prd_svc.coverage(db, prd)

    assert out["prd_id"] == prd.key
    assert out["prd_id"].startswith("ZZZ-P")
    assert out["prd_id"] != frozen, "coverage reported the tag the project no longer holds"


def test_decompose_reports_the_current_tag(db, retagged):
    prd, frozen = retagged

    out = prd_svc.decompose(db, prd)

    assert out["prd_id"] == prd.key
    assert out["prd_id"] != frozen


def test_the_reported_key_still_resolves(db, retagged):
    """Rendering is only safe because the key resolves back. `keys.resolve_prd` takes a rendered
    key and a frozen id, so a caller that feeds `prd_id` straight back into another call is
    unaffected by this change."""
    prd, _ = retagged

    reported = prd_svc.coverage(db, prd)["prd_id"]

    assert prd_svc.get_prd(db, reported) is not None


def test_a_stored_reference_is_frozen_whatever_form_it_arrives_in(db, retagged):
    """THE CONTROL, asserted at the layer that enforces it.

    `Item.prd_id` must never hold a rendering — it would point at nothing after the next retag,
    the failure PRD-13 exists to prevent. But the guard is `items_svc._stored_prd_id`, not the
    call site in `decompose`, so passing `prd.key` there is harmless and an assertion aimed at
    `decompose` cannot fail. That is what the first version of this test did, and it survived
    its own sabotage.

    Sabotage `_stored_prd_id` to skip resolution and this fails."""
    prd, frozen = retagged

    by_key = items_svc.create_item(db, title="via key", project_id="core", prd_id=prd.key)
    by_id = items_svc.create_item(db, title="via id", project_id="core", prd_id=frozen)

    assert by_key.prd_id == frozen, "a rendered key was stored verbatim; it will rot on retag"
    assert by_id.prd_id == frozen


def test_the_context_block_names_the_current_tag(db, retagged):
    """The prose carried into every generated item is read by a human, so it renders too."""
    prd, frozen = retagged

    out = prd_svc.decompose(db, prd)
    described = " ".join(p["description"] for p in out["proposals"])

    assert "Context from" in described, (
        "the fixture PRD produced no framing context — this test would pass vacuously"
    )
    assert f"Context from {prd.key}" in described
    assert f"Context from {frozen}" not in described
