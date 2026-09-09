"""A cluster says why it is a cluster (GRPH-810).

Reported as "area reservations are coarser than declared touchpoints, and invisible", with
`platform-models/[tier]/route.ts` vs `platform-models/route.ts` as the example. **That pair
does not collide** — measured: neither rule matches, their parent directories differ.

What does is broader and less obvious. `_match` relates any two files in the SAME DIRECTORY,
so a directory of five files collapses five items into one cluster. And `collision_clusters`
is union-find, so the effect spreads transitively: A and C serialise together when neither
touches anything the other does, because some B matched both.

That is a defensible clustering heuristic and a costly reservation rule, and until now there
was no way to tell which one you were looking at. This does not change the rule; it makes the
rule legible, so the decision to change it can rest on evidence.
"""
import pytest

from app.services import collision as collision_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(client, auth, title, touchpoints):
    r = client.post("/api/items", json={"title": title, "project_id": "core",
                                        "touchpoints": touchpoints}, headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _cluster_with(clusters, item_id):
    return next(c for c in clusters if item_id in c["items"])


def test_a_directory_merge_says_it_was_a_directory(client, auth, db):
    """THE ONE THAT MATTERS. An operator seeing two unrelated files serialise can now read
    that it was the directory rule and judge whether that is true of the work."""
    a = _item(client, auth, "one", ["svc/alpha.py"])
    _item(client, auth, "two", ["svc/beta.py"])

    got = _cluster_with(collision_svc.clusters_for_project(db, "core"), a)

    assert got["because"], "merged two items and gave no reason"
    rules = {r["rule"] for m in got["because"] for r in m["on"]}
    assert rules == {"directory"}


def test_the_exact_same_file_says_exact(client, auth, db):
    a = _item(client, auth, "one", ["svc/same.py"])
    _item(client, auth, "two", ["svc/same.py"])

    got = _cluster_with(collision_svc.clusters_for_project(db, "core"), a)

    assert any(r["rule"] == "exact" for m in got["because"] for r in m["on"])


def test_a_lone_item_merged_with_nothing_and_says_so(client, auth, db):
    """The control. A `because` populated for a cluster of one would make the field noise,
    and noise is how the real ones get scrolled past."""
    a = _item(client, auth, "alone", ["nowhere/alone.py"])

    got = _cluster_with(collision_svc.clusters_for_project(db, "core"), a)

    assert got["items"] == [a]
    assert got["because"] == []


def test_transitive_members_are_not_given_a_false_reason(client, auth, db):
    """A and C do not touch each other. They are together because B matched both, and the
    explanation must be those two merges — not a fabricated A-C pair, which would send
    somebody looking for an overlap that does not exist."""
    a = _item(client, auth, "a", ["x/a.py"])
    b = _item(client, auth, "b", ["x/b.py", "y/b.py"])
    c = _item(client, auth, "c", ["y/c.py"])

    got = _cluster_with(collision_svc.clusters_for_project(db, "core"), a)

    assert {a, b, c} <= set(got["items"])
    pairs = {tuple(m["items"]) for m in got["because"]}
    assert tuple(sorted((a, c))) not in pairs, "invented a reason A and C do not have"
    assert all(b in pair for pair in pairs), "a merge that did not involve b"


def test_the_reasons_are_bounded(client, auth, db):
    """A reason nobody reads is not a reason. Two items overlapping on fifty paths make the
    point in three."""
    many = [f"wide/f{i}.py" for i in range(20)]
    a = _item(client, auth, "a", many)
    _item(client, auth, "b", many)

    got = _cluster_with(collision_svc.clusters_for_project(db, "core"), a)

    assert all(len(m["on"]) <= 3 for m in got["because"])


def test_the_merge_count_is_bounded_by_the_cluster(client, auth, db):
    """At most n-1 merges build a cluster of n, and storing the quadratic comparisons instead
    would be a wall of text nobody reads."""
    ids = [_item(client, auth, f"i{n}", [f"one-dir/f{n}.py"]) for n in range(5)]

    got = _cluster_with(collision_svc.clusters_for_project(db, "core"), ids[0])

    assert len(got["items"]) == 5
    assert len(got["because"]) <= 4


def test_the_reported_pair_does_not_actually_collide(client, auth, db):
    """Recorded because the report named it and it is not true. Keeping the measurement here
    stops the example being re-derived from the prose next time somebody reads the finding."""
    a = _item(client, auth, "nested", ["platform-models/[tier]/route.ts"])
    b = _item(client, auth, "flat", ["platform-models/route.ts"])

    clusters = collision_svc.clusters_for_project(db, "core")

    assert _cluster_with(clusters, a)["items"] == [a]
    assert _cluster_with(clusters, b)["items"] == [b]
