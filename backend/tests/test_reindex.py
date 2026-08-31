"""The resumable re-index (PRD-25 S4b, GRPH-536).

Three claims, and each has a sabotage that a happy-path test cannot catch.

**Resume, not restart.** A run that dies at 40 of 43 seconds and starts from zero doubles the
window — and at ten times the corpus that is four minutes instead of eight. Resetting progress
on start is the bug, and it looks identical to working code on a run that never crashes.

**Batched, asserted on the REQUEST COUNT.** Wall clock is machine-dependent and would make this
test a flake generator; the number of calls to the embedder is not. A row-by-row loop produces
the same vectors and the same final state — only the request count differs, which is the whole
reason the measurement said batch.

**One table at a time.** `memory_shards` completes before `code_nodes` starts, so a search over
memory is fully old or fully new rather than half of each. Interleaving would leave both tables
half-migrated for the entire run, and without an assertion the claim is decoration.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models import CodeNode, MemoryShard, Project, ReindexProgress
from app.services import reindex


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def corpus(db):
    """A small corpus of known size, replacing whatever the seed left behind.

    Sizes are deliberately either side of `BATCH` so "one batch" and "several batches" are both
    exercised, and so the two tables have DIFFERENT counts — equal counts would let a bug that
    mixes the two tables' progress rows pass.
    """
    db.query(MemoryShard).delete()
    db.query(CodeNode).delete()
    db.commit()
    if db.get(Project, "core") is None:
        db.add(Project(id="core", name="Core", tag="CORE"))
        db.commit()
    for i in range(70):   # > BATCH (64)
        db.add(MemoryShard(id=f"ms_{i:03d}", project_id="core", text=f"shard {i}"))
    for i in range(10):   # < BATCH
        db.add(CodeNode(id=f"cn_{i:03d}", project_id="core", path=f"f{i}.py",
                        kind="file", summary=f"node {i}"))
    db.commit()
    return {"memory_shards": 70, "code_nodes": 10}


def _vector_for(text: str) -> list[float]:
    """A vector that identifies its text.

    **The first version returned `[0.5] * dim` for everything**, which made row-to-vector
    correspondence unobservable — a sabotage that gave every row in a batch the FIRST row's
    embedding passed all nine tests. In production that is silent: search returns confidently
    wrong neighbours and nothing errors. A distinguishable vector is what makes the mistake
    visible.
    """
    v = [0.0] * settings.embed_dim
    v[0] = float(len(text))
    v[1] = float(sum(ord(c) for c in text) % 997)
    return v


class _Counting:
    """An embedder that records how it was called and returns text-identifying vectors."""

    def __init__(self, batched=True):
        self.batch_calls = 0
        self.single_calls = 0
        self.batched = batched

    def embed(self, text):
        self.single_calls += 1
        return _vector_for(text)

    def embed_many(self, texts):
        if not self.batched:
            raise AttributeError("no batch support")
        self.batch_calls += 1
        return [_vector_for(t) for t in texts]


def _use(monkeypatch, embedder):
    monkeypatch.setattr(reindex, "resolve_embedder", lambda *a, **k: embedder)


def _drain(db, limit=50):
    """Run batches until nothing is outstanding. Bounded so a bug cannot hang the suite."""
    passes = 0
    while reindex.run_batch(db, "") and passes < limit:
        passes += 1
    return passes


# ---- resume, not restart --------------------------------------------------------------------


def test_a_run_that_is_interrupted_RESUMES(db, corpus, monkeypatch):
    """THE POINT. `plan` is called again after a partial run — as a restarting process would —
    and must not reset what is already done."""
    e = _Counting()
    _use(monkeypatch, e)
    reindex.plan(db, "")
    reindex.run_batch(db, "")
    part = db.get(ReindexProgress, ("", "memory_shards")).done
    assert part == reindex.BATCH, "the first batch did not do a full batch of work"

    reindex.plan(db, "")  # the process restarted

    assert db.get(ReindexProgress, ("", "memory_shards")).done == part, (
        "progress was reset on start — a resume became a restart, doubling the window"
    )


def test_a_finished_table_is_not_redone_by_a_later_run(db, corpus, monkeypatch):
    e = _Counting()
    _use(monkeypatch, e)
    reindex.plan(db, "")
    _drain(db)
    calls = e.batch_calls

    reindex.plan(db, "")
    _drain(db)

    assert e.batch_calls == calls, "a finished run was redone"


# ---- batched, by request count --------------------------------------------------------------


def test_re_embedding_is_batched_not_row_by_row(db, corpus, monkeypatch):
    """Asserted on the REQUEST COUNT, not the wall clock, which is machine-dependent.

    A row-by-row loop produces identical vectors and an identical final state. The only
    observable difference is how many times the provider was asked — which is exactly what the
    measurement was about.
    """
    e = _Counting()
    _use(monkeypatch, e)
    reindex.plan(db, "")
    _drain(db)

    total_rows = corpus["memory_shards"] + corpus["code_nodes"]
    assert e.single_calls == 0, f"{e.single_calls} single-row requests — it is not batching"
    assert e.batch_calls <= 4, (
        f"{e.batch_calls} requests for {total_rows} rows at batch {reindex.BATCH}; a batched "
        "run needs 2 for memory_shards and 1 for code_nodes"
    )


def test_a_provider_without_batch_support_still_works(db, corpus, monkeypatch):
    """The fallback is honest, not hidden: slower, and it says so once rather than degrading
    quietly."""
    e = _Counting(batched=False)

    class NoBatch:
        def __init__(self):
            self.calls = 0

        def embed(self, text):
            self.calls += 1
            return [0.5] * settings.embed_dim

    nb = NoBatch()
    _use(monkeypatch, nb)
    reindex.plan(db, "")
    _drain(db)

    assert nb.calls == corpus["memory_shards"] + corpus["code_nodes"]
    assert db.get(ReindexProgress, ("", "code_nodes")).finished_at is not None


# ---- one table at a time ----------------------------------------------------------------------


def test_the_second_table_is_untouched_until_the_first_is_finished(db, corpus, monkeypatch):
    """Without this assertion "one table at a time" is decoration. Interleaving leaves BOTH
    tables half-migrated for the whole run, which is the state it exists to prevent."""
    _use(monkeypatch, _Counting())
    reindex.plan(db, "")

    reindex.run_batch(db, "")  # one batch: memory_shards is now partially done

    shards = db.get(ReindexProgress, ("", "memory_shards"))
    nodes = db.get(ReindexProgress, ("", "code_nodes"))
    assert 0 < shards.done < shards.total, "memory_shards should be partway through"
    assert nodes.done == 0, "code_nodes was started before memory_shards finished"
    assert db.query(CodeNode).filter(CodeNode.embedding.isnot(None)).count() == 0


def test_every_row_ends_up_embedded(db, corpus, monkeypatch):
    """The counterpart to every ordering assertion: the run must actually finish the work."""
    _use(monkeypatch, _Counting())
    reindex.plan(db, "")
    _drain(db)

    assert db.query(MemoryShard).filter(MemoryShard.embedding.is_(None)).count() == 0
    assert db.query(CodeNode).filter(CodeNode.embedding.is_(None)).count() == 0


def test_each_row_gets_ITS_OWN_vector(db, corpus, monkeypatch):
    """FOUND BY A SURVIVING SABOTAGE. Giving every row in a batch the first row's embedding
    passed every other test in this file, because the fake embedder returned an identical
    vector for every text.

    This is the worst failure mode in the slice and the quietest: no error, no failed row, just
    a corpus whose neighbours are nonsense. Asserted per row against the text that produced it.
    """
    _use(monkeypatch, _Counting())
    reindex.plan(db, "")
    _drain(db)

    wrong = [
        r.id for r in db.query(MemoryShard).all()
        if list(r.embedding) != _vector_for(r.text)
    ]
    assert not wrong, (
        f"{len(wrong)} shard(s) carry a vector computed from different text — e.g. {wrong[:3]}"
    )

    nodes_wrong = [
        n.id for n in db.query(CodeNode).all()
        if list(n.embedding) != _vector_for(n.summary)
    ]
    assert not nodes_wrong, f"{len(nodes_wrong)} code node(s) carry the wrong vector"


def test_progress_is_recorded_per_table(db, corpus, monkeypatch):
    """The grill amendment. One counter cannot distinguish "finished memory_shards" from
    "partway through memory_shards", so there are two rows with two totals."""
    _use(monkeypatch, _Counting())
    reindex.plan(db, "")
    _drain(db)

    shards = db.get(ReindexProgress, ("", "memory_shards"))
    nodes = db.get(ReindexProgress, ("", "code_nodes"))
    assert (shards.total, nodes.total) == (corpus["memory_shards"], corpus["code_nodes"])
    assert shards.finished_at is not None and nodes.finished_at is not None


def test_status_reports_running_until_both_tables_are_done(db, corpus, monkeypatch):
    _use(monkeypatch, _Counting())
    reindex.plan(db, "")
    reindex.run_batch(db, "")

    assert reindex.status(db, "")["running"] is True
    _drain(db)
    assert reindex.status(db, "")["running"] is False


def test_nothing_outstanding_is_a_no_op(db, monkeypatch):
    """`run_batch` returning 0 is how the background loop knows to stop, without a separate
    "is it running" flag to keep accurate."""
    _use(monkeypatch, _Counting())

    assert reindex.run_batch(db, "") == 0


# ---- wiring: a service nothing calls is the defect, not the gap ----------------------------


def test_the_background_pass_advances_the_reindex(db, corpus, monkeypatch):
    """A re-index nothing drives is the GRPH-496 shape — written, correct and unreachable.

    Asserted by calling the pass the loop actually calls, not by reading `main.py`: the
    question is whether the wiring works, and only running it answers that.
    """
    import app.main as main

    _use(monkeypatch, _Counting())
    reindex.plan(db, "")
    before = db.get(ReindexProgress, ("", "memory_shards")).done

    main._one_background_pass()

    db.expire_all()
    assert db.get(ReindexProgress, ("", "memory_shards")).done > before, (
        "the background pass did not advance the re-index — it is wired to nothing"
    )


def test_a_failing_reindex_does_not_stop_credential_retries(db, corpus, monkeypatch):
    """The two jobs share a pass and must not share a fate. A stuck re-index that took the
    retry loop down with it would turn one broken feature into two."""
    import app.main as main

    monkeypatch.setattr(reindex, "run_batch",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("re-index broke")))
    called = {"retry": 0}
    from app.services import credential_retry
    monkeypatch.setattr(credential_retry, "run_once",
                        lambda *a, **k: called.__setitem__("retry", 1) or 0)

    main._one_background_pass()  # must not raise

    assert called["retry"] == 1, "credential retries were skipped because the re-index failed"


def test_the_endpoint_starts_a_run_and_reports_progress(client, auth, db, corpus, monkeypatch):
    """`restart=True` on the endpoint is the operator asking; the loop's call is the resuming
    one. Asserted over HTTP, because "is it reachable" is the question."""
    _use(monkeypatch, _Counting())

    started = client.post("/api/platform/reindex", headers=auth)
    assert started.status_code == 200, started.text
    assert {r["table"] for r in started.json()["started"]} == {"memory_shards", "code_nodes"}

    seen = client.get("/api/platform/reindex", headers=auth).json()
    assert seen["running"] is True
    assert {t["table"] for t in seen["tables"]} == {"memory_shards", "code_nodes"}


def test_the_endpoint_asks_to_restart_not_resume():
    """THE CALL. The HTTP test POSTs once on a fresh corpus, so resume-vs-restart is
    unobservable and the docstring's `restart=True` claim is decoration (GRPH-536 bounce).
    Dropping it left 13 passed.
    """
    import ast
    import inspect

    from app.routers import platform

    tree = ast.parse(inspect.getsource(platform.start_reindex))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        if name != "plan":
            continue
        for kw in node.keywords:
            if kw.arg == "restart" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                found = True
    assert found, (
        "POST /reindex no longer calls plan(restart=True) — an operator asking would "
        "resume leftover progress instead of starting a new run"
    )
