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


# ---- one lesson promotes once (GRPH-346) ----------------------------------------------------
# Found by running the first promotion pass over a real ingested corpus: 568 candidates
# produced 96 accepts, and those 96 were 19 DISTINCT texts — one string appeared 32 times.
#
# `_score_shard` dedups a candidate against PUBLISHED shards, but nothing compared a
# candidate to its fellow candidates, so every member of a cluster scored `accept` on the
# strength of the other members. Publishing that set would have put 32 copies into the
# corroboration pool, where they read as 32 independent pieces of evidence for whatever was
# scored next — the ingest runner closes this exact hole one layer down, and the ladder had
# it open.
def _candidate(db, text, session, sid=None):
    return mem_svc.add_memory(
        db, text_body=text, project_id="core", status="candidate",
        source=f"transcript:claude-code:{session}", origin="ingest:claude-code",
        auto_triage=False,
    )


REPEATED = ("The deployment needs the migration range bumped in AGENTS.md before it will "
            "pass, and the guard only reports it after the build has already started.")


def _pass(db):
    rows = mem_svc.score_candidates(db, project_id="core")
    return ([r for r in rows if r["suggestion"] == "accept"],
            [r for r in rows if r["duplicate_of"]], rows)


def test_a_recurring_lesson_is_offered_for_promotion_once(db):
    """THE acceptance criterion. Four occurrences across three sessions are ONE lesson with
    four pieces of evidence, not four lessons."""
    for i in range(4):
        _candidate(db, REPEATED, f"sess-{i % 3}")

    accepts, dupes, _ = _pass(db)
    assert len(accepts) == 1, f"one lesson, one promotion — got {len(accepts)}"
    assert len(dupes) == 3


def test_the_duplicates_point_at_the_shard_that_replaces_them(db):
    """A merge instruction, not a bare rejection — the reviewer has to know what survives."""
    for i in range(4):
        _candidate(db, REPEATED, f"sess-{i % 3}")

    accepts, dupes, _ = _pass(db)
    winner = accepts[0]["shard"].id
    assert {d["duplicate_of"] for d in dupes} == {winner}
    assert all("merge this into it" in d["reasons"][0] for d in dupes)


def test_the_duplicates_are_surfaced_rather_than_dropped(db):
    """Filtering them out of the queue would be cheaper and would misreport the corpus: a
    reviewer would see one row and no sign that thirty-one others exist."""
    for i in range(4):
        _candidate(db, REPEATED, f"sess-{i % 3}")

    _, dupes, rows = _pass(db)
    assert len(rows) == 4, "every candidate still accounted for"
    assert all("4 occurrences" in d["reasons"][0] for d in dupes), \
        "each duplicate must say how large the cluster it belongs to is"


def test_the_survivor_still_carries_the_recurrence_as_its_evidence(db):
    """Collapsing must not cost the cluster its support — the recurrence is precisely what
    earns the promotion."""
    for i in range(4):
        _candidate(db, REPEATED, f"sess-{i % 3}")

    accepts, _, _ = _pass(db)
    assert any("recurs across 4" in r for r in accepts[0]["reasons"])


def test_a_lone_candidate_is_scored_exactly_as_before(db):
    """PRD-16's success metric: verdicts are unchanged for inputs that lack the new signal.
    A shard in no cluster must not acquire a duplicate verdict it never had."""
    _candidate(db, "A one-off observation nothing else in the corpus resembles at all.", "s1")

    _, dupes, rows = _pass(db)
    assert dupes == []
    assert rows[0]["suggestion"] == "review"


def test_the_same_corpus_picks_the_same_survivor_twice(db):
    """A representative that moved between runs would republish the lesson under a new id
    every pass."""
    for i in range(4):
        _candidate(db, REPEATED, f"sess-{i % 3}")

    first = _pass(db)[0][0]["shard"].id
    assert _pass(db)[0][0]["shard"].id == first


def test_the_representative_is_the_one_the_others_agree_with(db):
    """The medoid, not the first written or the longest. `Failed to get project`, `Failed to
    list projects` and `Failed to create project` are three spellings of "re-auth to
    Railway", and the survivor should be the phrasing nearest all of them.

    Asserted on hand-built vectors because it is a claim about geometry, not about text: B
    sits between A and C, so B speaks for the group however the embedder happens to render
    any particular wording."""
    a = MemoryShard(id="m_a", text="get", project_id="core", embedding=[1.0, 0.0, 0.0])
    b = MemoryShard(id="m_b", text="list", project_id="core", embedding=[0.7, 0.7, 0.0])
    c = MemoryShard(id="m_c", text="create", project_id="core", embedding=[0.0, 1.0, 0.0])

    assert mem_svc._cluster_representative([a, b, c]).id == "m_b"
    assert mem_svc._cluster_representative([c, b, a]).id == "m_b", "order must not matter"


def test_a_tie_prefers_the_fuller_text(db):
    """With nothing to choose geometrically, keep the version that says more — the shorter
    one is usually a truncation of the same lesson.

    The ids are chosen so the FINAL tie-break (lowest id, there only for determinism) would
    pick the wrong one. Sabotage caught the first version of this test: with `m_f`/`m_s` the
    id rule happened to agree with the length rule, so removing the length rule changed
    nothing and the test passed either way."""
    short = MemoryShard(id="m_a", text="bump the range", project_id="core",
                        embedding=[1.0, 0.0])
    full = MemoryShard(id="m_z", text="bump the migration range in AGENTS.md first",
                       project_id="core", embedding=[1.0, 0.0])

    assert mem_svc._cluster_representative([short, full]).id == "m_z"


# ---- a failure that records no change must not promote (GRPH-350) ---------------------------
# Classification already refused these; the promotion path refused neither, so "the identical
# call succeeded on retry" could be published into trusted memory as something learned.
def _episode(db, state, session, text="Tool: Bash\nAttempted: x y z\nFailed: broke here"):
    """Distinct sessions on purpose: with one source the GRPH-306 veto fires first and the
    test would pass without the new rule ever being consulted."""
    return mem_svc.add_memory(db, text_body=text, project_id="core", status="candidate",
                              source=f"transcript:claude-code:{session}", auto_triage=False,
                              origin=f"ingest:claude-code:{state}")


@pytest.mark.parametrize("state", ["unresolved", "transient"])
def test_a_failure_recording_no_change_is_held_at_review(db, state):
    """Recurrence cannot rescue it. Ten identical unresolved failures are ten pieces of
    evidence that something is painful and zero evidence of what to do instead."""
    for i in range(4):
        _episode(db, state, f"s{i}")

    accepts = [r for r in mem_svc.score_candidates(db, project_id="core")
               if r["suggestion"] == "accept"]
    assert accepts == []


def test_a_resolved_episode_still_promotes(db):
    """The other half. A veto that held everything back would pass the test above and make
    the whole loop inert."""
    for i in range(4):
        _episode(db, "resolved", f"s{i}",
                 text=("Tool: Bash\nAttempted: sleep 30; tail log\nFailed: blocked\n"
                       "Resolved by: use Monitor with an until-loop"))

    accepts = [r for r in mem_svc.score_candidates(db, project_id="core")
               if r["suggestion"] == "accept"]
    assert len(accepts) == 1, "one lesson, one promotion (GRPH-346)"


def test_the_veto_says_why_it_was_held(db):
    """A reviewer seeing `review` with no reason cannot tell it from anything else novel."""
    for i in range(4):
        _episode(db, "unresolved", f"s{i}")

    rows = [r for r in mem_svc.score_candidates(db, project_id="core")
            if r["suggestion"] == "review"]
    assert any("records nothing that was done differently" in r
               for row in rows for r in row["reasons"])


def test_an_ordinary_candidate_is_untouched_by_the_veto(db):
    """PRD-16's success metric: verdicts are unchanged for inputs that lack the new signal."""
    for i in range(4):
        mem_svc.add_memory(db, text_body="always set a timeout on outbound http calls",
                           project_id="core", status="candidate", auto_triage=False,
                           source=f"transcript:claude-code:s{i}", origin="ingest:claude-code")

    accepts = [r for r in mem_svc.score_candidates(db, project_id="core")
               if r["suggestion"] == "accept"]
    assert len(accepts) == 1
