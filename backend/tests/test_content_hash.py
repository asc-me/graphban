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


def test_two_independent_producers_following_the_spec_agree():
    """The whole point. Both compute from the same bytes by the stated rule and land on the
    same value, so a second agent re-describing an unchanged file changes nothing."""
    agent_a = hashing.content_hash(SOURCE)
    agent_b = hashing.content_hash("".join(SOURCE))  # same bytes, different construction
    assert agent_a == agent_b

    changed, removed = compute_diff({"app/f.py": agent_b}, {"app/f.py": agent_a})
    assert changed == [] and removed == []


def test_a_producer_that_does_not_follow_the_spec_re_pushes_everything():
    """The cost being paid today, made visible.

    Before the rule existed this was the *normal* case, because nothing said what to
    compute. A plausible variant — hashing without the trailing-whitespace normalisation —
    disagrees on a file that did not change, and every path it touched re-pushes.
    """
    import hashlib

    spec = hashing.content_hash(SOURCE)
    plausible_variant = "sha256:" + hashlib.sha256(SOURCE.encode()).hexdigest()  # no rstrip
    assert plausible_variant != spec

    changed, _ = compute_diff({"app/f.py": plausible_variant}, {"app/f.py": spec})
    assert changed == ["app/f.py"], "an unchanged file reads as changed"


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

    assert hashing.same_content(specified, specified) is True
    assert hashing.same_content(legacy, legacy) is False, (
        "two equal legacy digests are not proof; they are two unknowns that match"
    )
    assert hashing.same_content(legacy, specified) is False


def test_a_legacy_hash_still_works_for_same_project_staleness():
    """The exclusion is scoped. Staleness compares a value against its own predecessor from
    the same producer, which never needed the rule — so legacy rows keep working and are
    not forced into a re-describe by this change."""
    legacy = artifact_inventory.content_hash(SOURCE)
    changed, _ = compute_diff({"app/f.py": legacy}, {"app/f.py": legacy})
    assert changed == []

    changed, _ = compute_diff({"app/f.py": legacy}, {"app/f.py": legacy + "x"})
    assert changed == ["app/f.py"]
