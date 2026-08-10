"""The promotion ladder: distinct-source recurrence and the decay clock (GRPH-306 / PRD-16).

PRD-16 extends the existing scorer rather than running beside it — its first non-goal is
**no second scorer**, because a parallel lifecycle would mean two answers to "is this
candidate worth keeping" and no way to tell which one acted.

So the new signal is a **veto on an accept**, never a new accept path. That shape is what
guarantees the success metric PRD-16 also states: *"the current scorer's verdicts are
unchanged by the ladder for inputs that lack the new signals."*

The failure it closes: `support` counts corroborating candidates, and one long session
restating itself produces as many as three independent ones do. Recurrence within a single
source is repetition, not evidence.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import MemoryShard
from app.services import memory as mem_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


VEC = [0.0] * 8


def _score(support, distinct, correction=False):
    return mem_svc._score_shard(
        VEC, [], [], support, human_derived=False,
        distinct_sources=distinct, correction=correction)[0]


# ---- distinct-source recurrence -----------------------------------------------------------
def test_three_repeats_from_one_session_do_not_promote(db):
    """THE acceptance criterion. One long session restating itself is repetition, and
    promoting on it lets a lesson that happened once look like a pattern."""
    assert _score(support=3, distinct=1) == "review"


def test_three_repeats_from_three_sessions_do_promote(db):
    """The other half of the same criterion — the ladder must not simply raise the bar for
    everyone, or nothing is ever learned."""
    assert _score(support=3, distinct=3) == "accept"


def test_a_correction_earns_trust_on_two_sources(db):
    """A lesson learned from something GOING WRONG carries its own evidence: the failure
    happened, and nobody writes down a correction they did not need."""
    assert _score(support=2, distinct=2, correction=True) == "accept"
    assert _score(support=2, distinct=2, correction=False) == "review"


def test_the_veto_never_creates_an_accept_that_was_not_there(db):
    """It can only downgrade. A signal that could promote on its own would be the second
    scorer PRD-16's non-goal forbids."""
    assert _score(support=1, distinct=9) == "review"


def test_verdicts_are_unchanged_when_the_new_signal_is_absent(db):
    """PRD-16's stated success metric, pinned. A caller that knows nothing about sources
    gets exactly the behaviour it got before the ladder existed."""
    before = mem_svc._score_shard(VEC, [], [], 3, human_derived=False)[0]
    assert before == "accept"


def test_the_reason_says_why_it_was_held(db):
    """A verdict a reviewer cannot argue with is one they learn to click through."""
    _, _, reasons, _ = mem_svc._score_shard(
        VEC, [], [], 3, human_derived=False, distinct_sources=1)
    assert any("one session" in r or "source(s)" in r for r in reasons)


def test_a_shared_placeholder_source_is_not_a_shared_session(db):
    """Caught by the existing suite, and it is the sharpest edge in this change. `source`
    doubles as an origin AND a category: an ingested event carries a session id, while an
    ordinary write carries `global` — a bucket every producer in the project shares.

    Counting `global` as one origin would collapse every agent write into one apparent
    session, and the ladder would refuse to promote anything an agent ever learned. A
    shared PLACEHOLDER is not evidence of a shared source, and reading it as one is the
    same mistake as reading an absence as a clean result."""
    group = [MemoryShard(id=f"m_g{i}", text="x", project_id="core", source="global")
             for i in range(3)]
    assert mem_svc._distinct_origins(group) is None, "unknown, not one"

    sessions = [MemoryShard(id=f"m_s{i}", text="x", project_id="core",
                            source=f"transcript:claude-code:sess-{i}") for i in range(3)]
    assert mem_svc._distinct_origins(sessions) == 3


def test_one_unknown_origin_makes_the_whole_count_unavailable(db):
    """All-or-nothing on purpose: with one member unknown the count is a lower bound rather
    than a fact, and vetoing on a lower bound holds back lessons that may be independent."""
    group = [MemoryShard(id="m_a", text="x", source="transcript:claude-code:s1"),
             MemoryShard(id="m_b", text="x", source="global")]
    assert mem_svc._distinct_origins(group) is None


# ---- the decay clock ------------------------------------------------------------------------
def _aged(db, days, status="candidate", sid="m_old"):
    shard = MemoryShard(id=sid, text="An old lesson.", project_id="core", status=status)
    shard.created_at = datetime.now(timezone.utc) - timedelta(days=days)
    db.add(shard)
    db.commit()
    return shard


def test_a_candidate_nobody_repeated_expires(db):
    assert mem_svc.age_state(_aged(db, mem_svc.CANDIDATE_EXPIRY_DAYS + 1)) == "expired"


def test_an_expired_candidate_leaves_retrieval(db):
    """THE acceptance criterion: it stops appearing without being hard-deleted."""
    _aged(db, mem_svc.CANDIDATE_EXPIRY_DAYS + 1, sid="m_gone")

    listed = [s.id for s in mem_svc.list_shards(db, project_id="core", status="candidate")]
    assert "m_gone" not in listed


def test_an_expired_candidate_is_still_there_to_be_found(db):
    """"Without being hard-deleted" has to mean something. A row nothing can fetch is
    deleted in every way that matters, so the review surface can still reach it."""
    _aged(db, mem_svc.CANDIDATE_EXPIRY_DAYS + 1, sid="m_kept")

    listed = [s.id for s in mem_svc.list_shards(db, project_id="core", status="candidate",
                                                include_expired=True)]
    assert "m_kept" in listed
    assert db.get(MemoryShard, "m_kept") is not None


def test_a_published_shard_is_flagged_stale_never_hidden(db):
    """Something a human stood behind does not stop being true because nobody restated it
    lately. Silently retiring it would delete the corpus's oldest and most-settled
    knowledge first — exactly backwards."""
    _aged(db, mem_svc.PUBLISHED_STALE_DAYS + 1, status="published", sid="m_stale")

    assert mem_svc.age_state(db.get(MemoryShard, "m_stale")) == "stale"
    listed = [s.id for s in mem_svc.list_shards(db, project_id="core", status="published")]
    assert "m_stale" in listed, "a stale published shard must stay retrievable"


def test_a_recent_candidate_is_fresh(db):
    assert mem_svc.age_state(_aged(db, 1, sid="m_new")) == "fresh"


def test_a_published_shard_does_not_expire_on_the_candidate_clock(db):
    """The two clocks mean different things and must not be collapsed: 45 days of silence
    condemns an uncorroborated candidate and says nothing about a reviewed fact."""
    shard = _aged(db, mem_svc.CANDIDATE_EXPIRY_DAYS + 1, status="published", sid="m_pub")
    assert mem_svc.age_state(shard) == "fresh"


def test_the_thresholds_are_constants_not_settings(db):
    """Deliberate. 49 settings already exist and each is a permanent branch in every
    deployment's behaviour; we do not know the right values yet, and a slider is how you
    avoid finding out."""
    for name in ("CANDIDATE_EXPIRY_DAYS", "PUBLISHED_STALE_DAYS",
                 "MIN_DISTINCT_SOURCES", "MIN_DISTINCT_SOURCES_CORRECTION"):
        assert isinstance(getattr(mem_svc, name), int)
