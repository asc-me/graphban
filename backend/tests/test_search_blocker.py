"""`search_items` finds the text somebody wrote into `blocker` (GRPH-806).

Reported from super-arc: `query="PARKED by planner"` returned 0 against ten items carrying
that exact string. Because a wave could not be scoped, the operator bounded it by parking the
rest of the backlog — and the field that parking writes into was the one field nobody could
search. Only its author could tell a scoping park from a real blocker.

Less acute since `--prd` (GRPH-797) stopped parking being the only lever, and still wrong: a
field the product writes prose into and cannot search is a field whose contents exist only for
whoever typed them.
"""
import pytest

from app.services import items as items_svc


@pytest.fixture()
def session(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _parked(client, auth, title, blocker):
    made = client.post("/api/items", json={"title": title, "project_id": "core"},
                       headers=auth).json()
    client.patch(f"/api/items/{made['id']}", json={"blocker": blocker}, headers=auth)
    return made["id"]


def test_a_parked_item_is_found_by_its_blocker(client, auth, session):
    """THE REGRESSION. Ten items said this and search found none of them."""
    parked = _parked(client, auth, "unrelated title", "PARKED by planner for the P11 wave")

    found = items_svc.search_items(session, query="PARKED by planner", project_id="core")

    assert parked in [i.id for i in found]


def test_the_search_is_case_insensitive_like_the_others(client, auth, session):
    parked = _parked(client, auth, "another", "Parked By Planner")

    found = items_svc.search_items(session, query="parked by planner", project_id="core")

    assert parked in [i.id for i in found]


def test_an_item_with_no_blocker_is_not_matched_by_an_empty_one(client, auth, session):
    """`(it.blocker or "")` makes every unblocked item carry an empty string, and every query
    is `in` an empty string only when the query is empty too — which the caller guards. The
    control that this did not turn search into "match everything"."""
    client.post("/api/items", json={"title": "clean", "project_id": "core"}, headers=auth)

    found = items_svc.search_items(session, query="PARKED", project_id="core")

    assert [i.title for i in found] == [] or all("clean" != i.title for i in found)


def test_title_and_description_still_match(client, auth, session):
    """The regression guard for the fields that already worked. Rewriting the condition is
    how the working half gets lost."""
    made = client.post("/api/items", json={"title": "findable title",
                                           "description": "findable body",
                                           "project_id": "core"}, headers=auth).json()

    by_title = items_svc.search_items(session, query="findable title", project_id="core")
    by_body = items_svc.search_items(session, query="findable body", project_id="core")

    assert made["id"] in [i.id for i in by_title]
    assert made["id"] in [i.id for i in by_body]


def test_tags_still_match(client, auth, session):
    made = client.post("/api/items", json={"title": "tagged", "tags": ["distinctive"],
                                           "project_id": "core"}, headers=auth).json()

    found = items_svc.search_items(session, query="distinctive", project_id="core")

    assert made["id"] in [i.id for i in found]
