"""The map can say which commit it describes, or admit it cannot (GRPH-54).

`content_hash` already answers "has this ONE file changed since I described it", and only for
a file the caller still holds and can re-hash. The question an agent asks before trusting the
projection is different and map-level: *is this current for the tree I am looking at?* A map
with no revision reads exactly like a map that is up to date, which is the whole defect.

**THE LOAD-BEARING TEST IN THIS FILE IS `test_a_map_at_two_revisions_has_no_revision`.**
Every other test here passes against an implementation that reports the newest sha it saw —
and that implementation is worse than none. An agent reads `revision` to decide whether it
may reason from the map; answering optimistically means half the nodes were described three
commits ago and it is told the map is current. The field's only job is to be trustworthy in
the pessimistic direction.
"""
from __future__ import annotations

import pytest

from app.services import code_graph as code_svc

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture()
def db(client):
    """Depends on `client` so the app has started and the schema exists — the shape every
    other service-level suite here uses."""
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def project(db):
    from app.models import Project

    p = Project(id="revproj", name="Rev", tag="REV")
    db.add(p)
    db.commit()
    return p


def describe(db, project, paths, revision=""):
    return code_svc.describe_code(
        db, project_id=project.id, revision=revision,
        nodes=[{"path": p, "kind": "file", "summary": "x"} for p in paths],
    )


def test_a_map_described_at_one_commit_reports_it(db, project):
    """The control. Without it, hardcoding `revision: None` satisfies every other test in
    this file and the feature ships as a field that is always null."""
    describe(db, project, ["a.py", "b.py"], revision=SHA_A)

    m = code_svc.get_code_map(db, project.id)

    assert m["revision"] == SHA_A
    assert m["revisions"]["distinct"] == 1
    assert m["revisions"]["unknown_nodes"] == 0


def test_a_map_at_two_revisions_has_no_revision(db, project):
    """THE DEFECT THIS FEATURE EXISTS TO AVOID.

    Two describe passes at different commits. The tempting implementation reports the newest
    — `max(...)`, or "the last one written wins" — and an agent then reads a confident sha
    for a map that is half stale, which is strictly worse than reading nothing.

    Null, plus enough detail to act: which shas are in play, so a caller can re-describe the
    minority instead of the whole tree.
    """
    describe(db, project, ["a.py"], revision=SHA_A)
    describe(db, project, ["b.py"], revision=SHA_B)

    m = code_svc.get_code_map(db, project.id)

    assert m["revision"] is None, (
        f"a map spanning two commits reported {m['revision']!r} as its revision — an agent "
        f"reading that would treat stale nodes as current"
    )
    assert m["revisions"]["distinct"] == 2
    assert sorted(m["revisions"]["known"]) == [SHA_A, SHA_B]


def test_one_unknown_node_un_pins_the_whole_map(db, project):
    """The subtler half, and the one a `len(set(...)) == 1` implementation gets wrong.

    Nine nodes at the same sha and one that never carried a revision is NOT a map pinned to
    that sha: the tenth may have been described at any commit, which is exactly the case the
    field exists to expose. Unknown does not agree with anything.
    """
    describe(db, project, ["a.py", "b.py"], revision=SHA_A)
    describe(db, project, ["legacy.py"])  # no revision — an older agent

    m = code_svc.get_code_map(db, project.id)

    assert m["revision"] is None, (
        "a map containing a node of unknown revision was reported as pinned; unknown was "
        "treated as agreement"
    )
    assert m["revisions"]["unknown_nodes"] == 1
    assert m["revisions"]["known"] == [SHA_A], "the known sha should still be reported"


def test_an_empty_map_is_not_pinned_to_anything(db, project):
    """Vacuous truth is the enemy here: `len(known) == 1` is false for an empty map, but so
    is `len(known) > 1`, and an implementation that defaults to "pinned" would claim a
    revision for a project with no described code at all."""
    m = code_svc.get_code_map(db, project.id)

    assert m["revision"] is None
    assert m["node_count"] == 0
    assert m["revisions"]["distinct"] == 0


def test_re_describing_at_a_new_commit_re_pins_the_map(db, project):
    """The recovery path. If a mixed map could never become pinned again, the field would
    report None forever after one stale describe and callers would learn to ignore it."""
    describe(db, project, ["a.py"], revision=SHA_A)
    describe(db, project, ["b.py"], revision=SHA_B)
    assert code_svc.get_code_map(db, project.id)["revision"] is None

    describe(db, project, ["a.py", "b.py"], revision=SHA_B)  # both, at one commit

    assert code_svc.get_code_map(db, project.id)["revision"] == SHA_B


def test_a_describe_without_a_revision_does_not_erase_a_known_one(db, project):
    """`revision or node.revision`, matching how content_hash is retained.

    A describe that omits the revision is saying "I do not know", not "forget what you
    knew". Overwriting a recorded sha with "" would let a single legacy caller silently
    un-pin a map that was correctly pinned — and the map would then report None with no
    record of why.
    """
    describe(db, project, ["a.py"], revision=SHA_A)

    describe(db, project, ["a.py"])  # same path, no revision

    m = code_svc.get_code_map(db, project.id)
    assert m["revision"] == SHA_A, "an unversioned re-describe wiped the recorded commit"
    assert m["revisions"]["unknown_nodes"] == 0


def test_each_node_carries_its_own_revision(db, project):
    """Map-level null says "do not trust me" but not what to fix. Per-node revisions are how
    a caller re-describes only the stale minority."""
    describe(db, project, ["a.py"], revision=SHA_A)
    describe(db, project, ["b.py"], revision=SHA_B)

    by_path = {n["path"]: n for n in code_svc.get_code_map(db, project.id)["nodes"]}

    assert by_path["a.py"]["revision"] == SHA_A
    assert by_path["b.py"]["revision"] == SHA_B


def test_the_known_list_cannot_grow_without_bound(db, project):
    """A neglected map accumulates one revision per describe. `known` is what a caller reads
    to decide what to re-describe, and an unbounded list on a hot read path is how a
    diagnostic field becomes a payload problem."""
    for i in range(15):
        describe(db, project, [f"f{i}.py"], revision=f"{i:040x}")

    revs = code_svc.get_code_map(db, project.id)["revisions"]

    assert revs["distinct"] == 15
    assert len(revs["known"]) == 10
    assert revs["truncated"] == 5, "the count of what was dropped is missing"
