"""GRPH-406 — one content-hash rule, and an unknown that looks unknown.

`CodeNode.content_hash` was agent-supplied with no stated algorithm, which is fine while a
hash only ever meets its own previous value and useless the moment two producers have to
agree. Two places already need them to agree: `code_sync.compute_diff`, which decides what
an incremental push sends, and any cross-repo duplication check, for which identical hashes
are the only proof-grade signal the cloud can hold.
"""
import pytest

from app import hashing
from app.services import artifact_inventory
from app.services.code_sync import compute_diff

SOURCE = "def f():\n    return 1\n"


def test_the_algorithm_is_pinned_to_a_recorded_value():
    """A spec nothing checks is a comment.

    This digest is recorded, not derived — computing the expected value with the same
    function under test would assert that sha256 is deterministic and nothing about the
    rule. Pinned, a silent change to the algorithm or the normalisation breaks here, which
    is the only place that would notice before every stored hash had quietly stopped
    meaning what the rows around it assume.
    """
    assert hashing.content_hash(SOURCE) == (
        "sha256:8795b1c438f59538c9f384a0cd430eecc44422596db895a1d5c17f27da4ccf1c"
    )


def test_trailing_whitespace_is_normalised_and_nothing_else_is():
    """An editor adding a final newline is not a change. A leading one is."""
    assert hashing.content_hash(SOURCE) == hashing.content_hash(SOURCE + "\n\n  ")
    assert hashing.content_hash(SOURCE) != hashing.content_hash("\n" + SOURCE)
    assert hashing.content_hash(SOURCE) != hashing.content_hash(SOURCE.replace("1", "2"))


def test_there_is_one_rule_and_not_two():
    """`artifact_inventory` had the only prior implementation. It now delegates, so the
    two surfaces cannot drift apart — which they would, silently, as separate copies."""
    assert artifact_inventory.content_hash(SOURCE) == hashing.bare_digest(SOURCE)
    assert hashing.content_hash(SOURCE) == f"sha256:{artifact_inventory.content_hash(SOURCE)}"


def test_a_second_agent_re_describing_an_unchanged_file_causes_no_re_push(client):
    """The acceptance criterion, with the two sides coming from DIFFERENT producers.

    The first version of this test had both sides call `hashing.content_hash`. Two calls to
    one function agreeing cannot fail against code where that function did not exist — it
    asserted that sha256 is deterministic and nothing about the rule. Bounced, correctly.

    Here agent A describes with the spec and agent B re-describes the same unchanged file
    with a raw digest, which is what an agent that has not adopted the rule actually sends.
    B's hash no longer overwrites A's, so `compute_diff` — the consumer this item names as
    where the churn is paid — reports nothing changed.

    **Fails against pre-GRPH-406 code**, where `upsert_node` took whatever arrived.
    """
    import hashlib

    from app.db import SessionLocal
    from app.services import code_graph

    spec = hashing.content_hash(SOURCE)
    raw = hashlib.sha256(SOURCE.encode()).hexdigest()  # no prefix, no rstrip: a real variant
    assert raw != spec and not hashing.comparable(raw)

    db = SessionLocal()
    try:
        a = code_graph.describe_code(db, project_id="core", nodes=[
            {"path": "app/f.py", "kind": "file", "summary": "f", "content_hash": spec}], edges=[])
        db.commit()
        assert a["hash_retained"] == [], "the spec-compliant hash must be stored as sent"
        pushed = {"app/f.py": spec}

        b = code_graph.describe_code(db, project_id="core", nodes=[
            {"path": "app/f.py", "kind": "file", "summary": "f", "content_hash": raw}], edges=[])
        db.commit()
        assert b["hash_retained"] == ["app/f.py"], "the retain must be REPORTED, not silent"

        node = next(n for n in code_graph.list_nodes(db, "core") if n.path == "app/f.py")
        assert node.content_hash == spec, "provenance must not degrade"

        changed, removed = compute_diff({"app/f.py": node.content_hash}, pushed)
        assert changed == [] and removed == [], "an unchanged file must not re-push"
    finally:
        db.close()


def test_a_known_hash_is_still_replaced_by_another_known_one(client):
    """The retain is scoped to provenance, not to change. A spec-compliant hash for genuinely
    different contents must still land, or the graph would freeze the first value it saw."""
    from app.db import SessionLocal
    from app.services import code_graph

    first = hashing.content_hash(SOURCE)
    second = hashing.content_hash(SOURCE.replace("1", "2"))

    db = SessionLocal()
    try:
        for h in (first, second):
            code_graph.describe_code(db, project_id="core", nodes=[
                {"path": "app/g.py", "kind": "file", "summary": "g", "content_hash": h}], edges=[])
            db.commit()
        node = next(n for n in code_graph.list_nodes(db, "core") if n.path == "app/g.py")
        assert node.content_hash == second
    finally:
        db.close()


def test_an_unspecified_hash_still_lands_where_there_is_nothing_better(client):
    """Monotone, not restrictive. An agent that has not adopted the rule is not blocked from
    describing — 498 of the live graph's nodes carry a bare hash today, and refusing them
    would make a re-describe fail against the real graph rather than improve it."""
    from app.db import SessionLocal
    from app.services import code_graph

    db = SessionLocal()
    try:
        r = code_graph.describe_code(db, project_id="core", nodes=[
            {"path": "app/h.py", "kind": "file", "summary": "h", "content_hash": "h-legacy"}],
            edges=[])
        db.commit()
        assert r["hash_retained"] == []
        node = next(n for n in code_graph.list_nodes(db, "core") if n.path == "app/h.py")
        assert node.content_hash == "h-legacy"
    finally:
        db.close()


def test_a_legacy_hash_is_refused_for_cross_producer_comparison():
    """Rows written before this module hold digests of unknown provenance.

    Unprefixed, they are indistinguishable from specified ones, and the system would
    compare incomparable values and be confidently wrong. Refusing is the honest answer —
    including when the two strings happen to be equal, because 'probably the same rule' is
    exactly the guess this exists to prevent.
    """
    legacy = artifact_inventory.content_hash(SOURCE)  # bare, no prefix
    specified = hashing.content_hash(SOURCE)

    assert hashing.comparable(specified) is True
    assert hashing.comparable(legacy) is False
    assert hashing.comparable(None) is False
    assert hashing.comparable("") is False

    # `same_content` was removed with this rework. It existed for a cross-repo duplication
    # check that has not been built, and nothing outside its own test ever called it — the
    # same dead-code standard this repo applied to the `total <= 0` guard. It comes back
    # with the feature that needs it, where a test can exercise it for real.


def test_a_legacy_hash_still_works_for_same_project_staleness():
    """The exclusion is scoped. Staleness compares a value against its own predecessor from
    the same producer, which never needed the rule — so legacy rows keep working and are
    not forced into a re-describe by this change."""
    legacy = artifact_inventory.content_hash(SOURCE)
    changed, _ = compute_diff({"app/f.py": legacy}, {"app/f.py": legacy})
    assert changed == []

    changed, _ = compute_diff({"app/f.py": legacy}, {"app/f.py": legacy + "x"})
    assert changed == ["app/f.py"]
