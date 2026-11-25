"""GRPH-1898 — `GET /api/items` must not cost a statement per row.

Measured on the box: one call, 6039 ms and 1529 KB on a large project, the slowest on the
page. GRPH-1228 took the payload down to lean rows; this is the other half. Rendering those
rows read each row's PRD key and each blocked row's dependency waits ONE ROW AT A TIME — 135
statements for 400 rows against the 7 the same page costs batched. Both were per-row by
construction: a `session.get` of a PRD the identity map had already dropped (it holds objects
weakly, so a PRD nothing else references is collected the moment its key is rendered), and a
`links` query per blocked row.

Counts are asserted as "does not grow", never as a magic number — the absolute figure differs
by engine, and the shape `tests/test_query_counts.py` and `tests/test_project_summaries.py`
already use.
"""
from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import event

# The pinned lean shape (GRPH-1228). Imported rather than restated: two copies of a field set
# is how one of them drifts.
from tests.test_items_list import LEAN_FIELDS

BIG = "x" * 3000
SMALL_N = 20
BIG_N = 400


class _Count:
    """Statements the engine runs, in order — the counter `tests/test_query_counts.py` uses."""

    def __init__(self, db):
        self.engine, self.n, self.sql = db.get_bind(), 0, []

    def _hit(self, conn, cursor, statement, *a, **k):
        self.n += 1
        self.sql.append(" ".join(statement.split())[:110])

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._hit)


@pytest.fixture()
def db(client):
    """A session of its own, opened after the app's lifespan seeded. Not the request's
    session: seeding through it leaves the counter seeing only what the render does."""
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def project(client, auth):
    return client.post("/api/projects", json={"name": "Perf"}, headers=auth).json()["id"]


def _seed(db, project, n, start=0):
    """`n` more rows, in the shapes whose rendering used to cost statements: on a PRD, carrying
    evidence, an open decision wait, blocked behind one, linked to a neighbour."""
    from app.models import Item
    from app.services import items as items_svc
    from app.services import links as links_svc
    from app.services import prds as prds_svc
    from app.services import waits as waits_svc

    prds = [prds_svc.create_prd(db, title=f"spec {i}", project_id=project).id for i in range(5)]
    wait, previous = None, None
    for i in range(start, start + n):
        row = items_svc.create_item(
            db, title=f"item {i}", description=BIG, project_id=project,
            prd_id=prds[i % len(prds)] if i % 5 == 0 else None,
        )
        row = db.get(Item, row.id)
        row.evidence = [{"kind": "note", "detail": BIG, "url": ""}] * 2
        if i % 11 == 0:
            waits_svc.set_decision(row, question=f"q{i}?", options=["a", "b"])
            row.status = "blocked"
            wait = row.id
        elif i % 7 == 0 and wait:
            row.status = "blocked"
            links_svc.create_link(db, a=row.id, b=wait, type_="dependency", project_id=project)
        elif previous:
            links_svc.create_link(db, a=row.id, b=previous, type_="code", project_id=project)
        previous = row.id
    db.commit()


def test_the_statement_count_does_not_grow_with_the_item_count(client, auth, db, project):
    _seed(db, project, SMALL_N)
    db.expire_all()
    with _Count(db) as small:
        little = client.get(f"/api/items?project_id={project}", headers=auth).json()

    _seed(db, project, BIG_N - SMALL_N, start=SMALL_N)
    db.expire_all()
    with _Count(db) as big:
        rows = client.get(f"/api/items?project_id={project}", headers=auth).json()

    assert len(little) == SMALL_N and len(rows) == BIG_N
    # Equality, not a ceiling. A per-row lookup puts the big count an order of magnitude above
    # the small one, and any bound loose enough to accept that accepts the defect — the reason
    # this is two sizes rather than one number (as in `test_credentials_resolution.py`).
    assert small.n == big.n, (small.n, big.n, Counter(big.sql) - Counter(small.sql))
    # Flat because the lookups are batched, not because the rows that need them are absent:
    # a flat count over a page with no PRD and no waiting row would prove nothing.
    assert sum(1 for r in rows if r["prd_id"]) >= BIG_N // 10
    assert sum(1 for r in rows if r["question_state"]) >= BIG_N // 10


def test_the_page_is_the_rows_a_row_at_a_time_render_builds(client, auth, db, project):
    """Batching changes what the render COSTS, not what it says.

    The page is compared against `item_list_row` called outside any batch, where
    `question_state` takes its own link query and `prd_id` its own `session.get` — so every
    value has to come out the same, the rendered PRD keys and the wait states included. The
    lean-shape half of the acceptance rides here too, at 400-row scale rather than at the one
    row a small fixture seeds.
    """
    from app.schemas import ITEM_HEAVY_FIELDS, item_list_row
    from app.services import items as items_svc

    _seed(db, project, SMALL_N)
    _seed(db, project, BIG_N - SMALL_N, start=SMALL_N)

    response = client.get(f"/api/items?project_id={project}", headers=auth)
    page = {row["id"]: row for row in response.json()}
    one_at_a_time = {it.key: item_list_row(it)
                     for it in items_svc.list_items(db, project_id=project)}

    assert page == one_at_a_time
    row = next(iter(page.values()))
    assert set(row) == LEAN_FIELDS
    for field in ITEM_HEAVY_FIELDS:
        assert field not in row
    assert BIG not in response.text
    # The two shapes the batching serves, present in what was compared — otherwise the
    # equality above is between two pages that never rendered a key or a wait state.
    assert any(r["prd_id"] for r in page.values())
    assert any(r["question_state"] for r in page.values())


def _wait(db, project, title, *, tag="wait:decision", person=False, answered=False, done=False):
    """One wait row, in the state named. `done` is written on the row: creating one is refused,
    and `_open_decision_state` reads the status rather than how it got there."""
    from app.models import Item
    from app.services import items as items_svc

    asking = tag == "wait:decision"
    it = items_svc.create_item(
        db, title=title, project_id=project, tags=[] if asking else [tag],
        question="q?" if asking else None, options=["a", "b"] if asking else None,
    )
    row = db.get(Item, it.id)
    row.status = "done" if done else "blocked"
    if person:
        row.decision = {**(row.decision or {}), "needs_person_at": "2026-11-24T00:00:00+00:00"}
    if answered:
        row.decision = {**(row.decision or {}), "answer": "a"}
    db.commit()
    return row.id


def test_the_batch_answers_what_the_single_read_answers(db, project):
    """Every shape `question_state` distinguishes, batched and one row at a time.

    A batch that returned None for the shapes it never fetches would be flat AND wrong, so the
    shapes are asserted against the answer each one MEANS — not only against the single-row
    read, which would agree with a batch that was wrong in the same way.
    """
    from app.services import items as items_svc
    from app.services import links as links_svc
    from app.services import waits as waits_svc

    checking = _wait(db, project, "wait checking")
    person = _wait(db, project, "wait person", person=True)
    answered = _wait(db, project, "wait answered", answered=True)
    finished = _wait(db, project, "wait done", done=True)
    gate = _wait(db, project, "wait merge", tag="wait:merge")
    ordinary = items_svc.create_item(db, title="ordinary", project_id=project).id

    # `Link.a`/`Link.b` carry no foreign key, so an edge can outlive the row it points at.
    dangling = f"{project}-no-such-item"
    expected = {
        "wait checking": waits_svc.CHECKING,
        "wait person": waits_svc.RESPONSE_NEEDED,
        "wait answered": None,
        "wait done": None,
        "wait merge": None,
        "ordinary": None,
        # A blocked row reads the state of what it waits on...
        "behind checking": waits_svc.CHECKING,
        "behind person": waits_svc.RESPONSE_NEEDED,
        # ...the one that needs a person winning over the one that does not...
        "behind both": waits_svc.RESPONSE_NEEDED,
        # ...and a gate wait, an ordinary row, an answered wait, a finished wait and a
        # dangling edge are all "nothing is waiting on a person".
        "behind gate": None,
        "behind ordinary": None,
        "behind answered": None,
        "behind done": None,
        "behind nothing": None,
        "behind checking twice": waits_svc.CHECKING,
        # Not blocked, so its dependency is never consulted.
        "not blocked": None,
    }
    deps = {
        "behind checking": [checking],
        "behind person": [person],
        "behind both": [checking, person],
        "behind gate": [gate],
        "behind ordinary": [ordinary],
        "behind answered": [answered],
        "behind done": [finished],
        "behind nothing": [dangling],
        "behind checking twice": [checking, checking],
        "not blocked": [checking],
    }
    for title, targets in deps.items():
        it = items_svc.create_item(db, title=title, project_id=project)
        row = db.get(type(it), it.id)
        if title != "not blocked":
            row.status = "blocked"
        for target in targets:
            links_svc.create_link(db, a=row.id, b=target, type_="dependency", project_id=project)
    db.commit()

    items = items_svc.list_items(db, project_id=project)
    single = {it.id: waits_svc.question_state(db, it) for it in items}
    assert single == {it.id: expected[it.title] for it in items}

    # The batched read, and the property the row render goes through while it is open.
    assert waits_svc.question_states(db, items) == single
    with waits_svc.batched(db, items):
        assert {it.id: it.question_state for it in items} == single
    # The map is dropped on exit: a later read must not be served a state computed from rows
    # that have since changed.
    assert waits_svc._PRECOMPUTED_STATES not in db.info
