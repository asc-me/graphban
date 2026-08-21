"""Linking a request stores the item's frozen ID, not the key it was asked with (GRPH-459).

`items.id` is frozen at issue time; the key a human sees is rendered from the project's
CURRENT tag. Retagging a project separates them — `AL-141` on disk, `GRPH-141` on screen —
and `ItemOut.id` is aliased from `key`, so the rendered key is exactly what a client has to
send back. `link_request` resolved that key to prove the item existed and then stored the
caller's spelling into `requests.linked_to`, which carries a foreign key to `items(id)`.

Result on the live database: every attempt to link a request to an item through the UI died
on `requests_linked_to_fkey`, and had since the AL->GRPH retag. R-1 sat unlinked for a month.

WHY THE EXISTING SUITE COULD NOT FAIL. Fixtures create items whose row id and rendered key
are the same string, because nothing in them retags a project. With one spelling, storing the
key and storing the id are indistinguishable — the same shape as GRPH-436, where a single
seeded project made "scoped" and "unscoped" the same set. So every test below RETAGS the
project, which is the only thing that makes the two spellings differ.

They also assert on the STORED VALUE rather than on the absence of an exception. This app
turns foreign keys ON for SQLite too (`app/db.py` sets `PRAGMA foreign_keys=ON`), so the
broken code does raise on both engines — but "it raised" and "it stored the right string"
are different claims, and only the second one is what the fix is about. Checking the value
also keeps these tests meaningful if the constraint is ever relaxed.
"""
import pytest

from app.models import Item, Request
from app.services import projects as proj_svc
from app.services import requests as req_svc


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
    """A project whose items' frozen ids and rendered keys DISAGREE.

    Returns the item, and asserts the divergence before any test uses it — without it every
    assertion below passes against the broken code, which is precisely how this shipped.
    """
    from app.models import User

    owner = db.query(User).first()
    project = proj_svc.create_project(db, name="Retagged", owner_user_id=owner.id, tag="AA")
    db.commit()

    from app.services import items as items_svc

    item = items_svc.create_item(db, title="the target", project_id=project.id)
    db.commit()
    frozen = item.id

    proj_svc.retag_project(db, project.id, "BB")
    db.commit()
    db.refresh(item)

    assert item.id == frozen, "retagging must not move a frozen id — PRD-13"
    assert item.key != item.id, (
        f"the fixture did not diverge: id={item.id!r} key={item.key!r}. Every assertion in "
        "this file is vacuous unless these differ."
    )
    request = req_svc.create_request(db, type_="bug", title="probe", project_id=project.id)
    db.commit()
    return {"item": item, "request": request, "frozen": frozen, "project": project.id}


def test_linking_by_the_rendered_key_stores_the_frozen_id(retagged, db):
    """The bug. `ItemOut.id` is the rendered key, so this is what the client sends."""
    item, request = retagged["item"], retagged["request"]

    req = req_svc.link_request(db, request.id, item.key)

    assert req.linked_to == retagged["frozen"], (
        f"stored {req.linked_to!r}; the foreign key points at items(id) = "
        f"{retagged['frozen']!r}. On Postgres this is a ForeignKeyViolation."
    )
    assert req.status == "linked"


def test_linking_by_the_frozen_id_still_works(retagged, db):
    """The other spelling must keep working — `accept_request` passes `item.id` directly."""
    req = req_svc.link_request(db, retagged["request"].id, retagged["frozen"])

    assert req.linked_to == retagged["frozen"]


def test_what_the_client_reads_back_is_the_rendered_key(retagged, db):
    """The round trip a human sees. `RequestOut.linked_to` is aliased from `linked_to_key`,
    which renders the CURRENT key from the stored id — so storing the id is what makes the
    UI's `request.linked_to === it.id` comparison line up, and the tick appear."""
    item = retagged["item"]

    req = req_svc.link_request(db, retagged["request"].id, item.key)
    db.refresh(req)

    assert req.linked_to_key == item.key, "the row cannot render the key the client sent"
    assert req.linked_to != req.linked_to_key, "the fixture stopped diverging"


def test_the_stored_value_satisfies_the_foreign_key(retagged, db):
    """Asserted as a JOIN rather than as "no exception". The constraint does fire on both
    engines here, so a test that only caught the raise would pass on a build that stored a
    dangling-but-valid-looking id; this one has to find the row."""
    req = req_svc.link_request(db, retagged["request"].id, retagged["item"].key)
    db.commit()

    joined = db.query(Item).join(Request, Request.linked_to == Item.id).filter(
        Request.id == req.id).one_or_none()

    assert joined is not None, "linked_to does not point at any row in items"
    assert joined.id == retagged["frozen"]


def test_an_unknown_item_is_still_refused(retagged, db):
    """Resolution must not turn a bad reference into a silent link. There is no
    degrade-as-given path here — unlike `Item.prd_id`, this column has a foreign key, so an
    unresolvable value cannot be stored at all."""
    with pytest.raises(ValueError, match="item not found"):
        req_svc.link_request(db, retagged["request"].id, "NOPE-999")


def test_unlinking_clears_both_fields(retagged, db):
    """The other branch, so the fix cannot have broken it."""
    req_svc.link_request(db, retagged["request"].id, retagged["item"].key)
    req = req_svc.link_request(db, retagged["request"].id, None)

    assert req.linked_to is None and req.status == "new"
