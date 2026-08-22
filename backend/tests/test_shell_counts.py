"""The shell asks for numbers, not collections (GRPH-431).

Reported as "the PRD page isn't loading" on the live instance. It was not failing — every
request returned 200. One load of `/prds` fetched 765 KB of items, 740 KB of memory shards and
621 KB of candidates to render three nav badges and one stat, while the data the page exists to
show was 2.8 KB. nginx logged three `upstream response is buffered to a temporary file`
warnings serving a single view. It presents as a hang precisely because it is not an error:
there is no message and nothing to retry, and it degrades linearly with project age.

The load-bearing test here is `test_counts_match_the_collections_they_replace`. A count that
disagrees with the list it labels — a badge reading 4 above a list of 7 — is worse than the slow
badge it replaced, and it is the failure this change could plausibly introduce, since `review`
is defined by a Python-side expiry filter rather than by SQL.
"""
import datetime as dt

import pytest

from app.services import memory as mem_svc
from app.services import projects as proj_svc

from tests.decoy import assert_populated


@pytest.fixture()
def db(client):
    """Depends on `client` so the app has started and the database is reset — the same shape
    every other suite here uses."""
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _seed(db, project_id="core", *, items=3, in_progress=1, requests=2, candidates=2):
    from app.services import items as items_svc
    from app.services import requests as req_svc

    for i in range(items):
        it = items_svc.create_item(db, title=f"item {i}", project_id=project_id)
        if i < in_progress:
            items_svc.update_item(db, it.id, status="in_progress")
    for i in range(requests):
        req_svc.create_request(db, type_="feedback", title=f"req {i}", project_id=project_id)
    for i in range(candidates):
        mem_svc.add_memory(db, text_body=f"cand {i}", project_id=project_id, status="candidate")


def test_counts_match_the_collections_they_replace(db, decoy):
    """The pin. Each number must equal `len()` of the list the shell used to fetch, including
    the expiry rule — otherwise the badge and the page it links to disagree, and the badge is
    the one people trust because it is always on screen."""
    _seed(db)

    counts = proj_svc.shell_counts(db, "core")

    from app.services import items as items_svc
    from app.services import requests as req_svc

    assert counts["items"] == len(items_svc.list_items(db, project_id="core"))
    assert counts["items_in_progress"] == len(
        items_svc.list_items(db, project_id="core", status="in_progress")
    )
    assert counts["requests"] == len(req_svc.list_requests(db, project_id="core"))

    auto = mem_svc.auto_triaged_shards(db, project_id="core")
    expected_review = len(mem_svc.list_shards(db, project_id="core", status="candidate")) + sum(
        1 for s in auto if s.scoring_source in ("trusted", "agent")
    )
    assert counts["review"] == expected_review


def test_an_expired_candidate_leaves_the_review_count(db):
    """`review` is not a SQL `count(*)`, and this is why. `list_shards` drops expired candidates
    in Python via `age_state`, so a hand-written count over the same table would keep counting
    one the queue no longer shows. Ages a candidate past the cutoff and asserts both move
    together."""
    _seed(db, candidates=2)
    before = proj_svc.shell_counts(db, "core")["review"]

    stale = mem_svc.list_shards(db, project_id="core", status="candidate")[0]
    stale.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=mem_svc.CANDIDATE_EXPIRY_DAYS + 1
    )
    db.commit()

    assert mem_svc.age_state(stale) == "expired", "the fixture did not actually expire it"
    assert proj_svc.shell_counts(db, "core")["review"] == before - 1


def test_no_project_counts_nothing(db):
    """Asking bare must not count the whole instance. `useItems()` called without a project id
    is how a nav badge starts reporting every project on the box."""
    _seed(db)
    assert proj_svc.shell_counts(db, None) == {
        "items": 0, "items_in_progress": 0, "requests": 0, "review": 0,
    }


def test_the_counts_endpoint_is_small(client, auth):
    """The budget. The point of this change is the wire, so the wire is what is asserted —
    2.1 MB of collections became a payload measured in bytes, and a regression that reintroduced
    rows would show up here rather than as a slow page nobody can pin down."""
    r = client.get("/api/projects/core/counts", headers=auth)
    assert r.status_code == 200
    assert set(r.json()) == {"items", "items_in_progress", "requests", "review"}
    assert all(isinstance(v, int) for v in r.json().values())
    assert len(r.content) < 200, f"counts payload grew to {len(r.content)} bytes"


def test_shard_limit_caps_live_shards_not_rows_read(db):
    """`limit` is applied after the expiry filter. Applying it in SQL would cap rows read and
    then drop expired ones from that page, so a caller asking for 5 could get 3 and read it as
    "that is all there is"."""
    for i in range(6):
        mem_svc.add_memory(db, text_body=f"shard {i}", project_id="core", status="candidate")
    old = mem_svc.list_shards(db, project_id="core", status="candidate")[0]
    old.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=mem_svc.CANDIDATE_EXPIRY_DAYS + 1
    )
    db.commit()

    live = mem_svc.list_shards(db, project_id="core", status="candidate")
    assert len(mem_svc.list_shards(db, project_id="core", status="candidate", limit=3)) == 3
    assert len(mem_svc.list_shards(db, project_id="core", status="candidate", limit=99)) == len(live)


def test_the_counts_are_pinned_to_their_project(db, decoy):
    """Removing the WHERE must fail HERE, because it fails nowhere else (GRPH-436).

    Every other test in this file seeds `core` alone, and with one project "count this
    project" and "count the instance" return the same number — so deleting
    `.where(model.project_id == project_id)` left all five of them green. Measured during the
    review of GRPH-431, not supposed.

    Note what the fix actually is: the FIXTURE, not the assertions. Once a second populated
    project exists, `test_counts_match_the_collections_they_replace` above becomes able to
    fail too — its `count == len(list)` comparison was always the right assertion and simply
    had nothing to differ from. This test states the property directly so the reason survives
    someone re-reading it later. `test_counts_match_the_collections_they_replace` above now
    takes the same fixture and fails under the same mutation.

    Seed-tolerant on purpose: `core` arrives with the prototype dataset already in it, so
    every number here is compared against the collection it labels rather than a literal. A
    literal was the first version and it failed at 12 == 3, which is the fixture talking, not
    the code under test.
    """
    assert_populated(db, decoy)
    _seed(db, items=3, in_progress=1, requests=2, candidates=2)

    from app.services import items as items_svc
    from app.services import requests as req_svc

    did = decoy["project_id"]
    core_items = items_svc.list_items(db, project_id="core")
    decoy_items = items_svc.list_items(db, project_id=did)

    # The control. If the decoy is empty, "excluded" and "never existed" are the same
    # observation and everything below passes for the wrong reason.
    assert len(decoy_items) >= decoy["items"], "the decoy has no items to leak"

    core = proj_svc.shell_counts(db, "core")
    other = proj_svc.shell_counts(db, did)

    assert core["items"] == len(core_items), "core counted rows belonging to another project"
    assert core["items_in_progress"] == len(
        items_svc.list_items(db, project_id="core", status="in_progress"))
    assert core["requests"] == len(req_svc.list_requests(db, project_id="core"))

    # The other direction: each project sees its own. A count that returned the instance
    # total would satisfy neither, but a count that returned ZERO would satisfy the first.
    assert other["items"] == len(decoy_items), "the decoy cannot see its own rows"
    assert other["requests"] == len(req_svc.list_requests(db, project_id=did))
    assert core["items"] != len(core_items) + len(decoy_items)
