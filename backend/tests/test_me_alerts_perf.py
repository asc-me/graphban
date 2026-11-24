"""GRPH-1897 — GET /api/me/alerts must not cost statements per alert.

The bell is fetched on every page. Measured on the box at 6149 ms for a 5 KB body, because
`alert_dict` read the wait, its asker, its project and the drain once per row and the access
check (GRPH-1535 D6) was the one thing that had to stay. Counts are asserted as "does not
grow", never as a number: the absolute count varies by engine, the shape does not — the same
rule `tests/test_project_summaries.py` pins.

Two fixture details are load-bearing. Every alert gets its OWN wait and ticket, because
repeated ids are served from the session's identity map and a per-row `db.get` would then look
batched. And no drain is live anywhere, because which branch `drain_status` takes decides
whether it runs one statement or two — a presence that expired between two measured runs would
move the count all by itself.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select

from app.db import SessionLocal
from app.models import Agent, Alert, Membership, Project, User
from app.security.authz import readable_project_ids
from app.services import alerts as alerts_svc
from app.services import items as items_svc
from app.services import links as links_svc

OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
PROJECTS = ("core", "web")
ME = "alex@ascme-labs.com"


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def no_live_drain(db):
    """Every agent offline, so `drain_status` answers `not_live` by the same two statements
    in every run. Without it the count depends on when the seed's agents age out of presence."""
    db.query(Agent).update({Agent.last_seen_at: OLD}, synchronize_session=False)
    db.commit()


def _me(db):
    return db.scalar(select(User.id).where(User.email == ME))


def _seed_alerts(db, user_id, n, start=0, projects=PROJECTS):
    """`n` more alerts, each on its own wait and its own ticket. Returns what each one must
    say: (wait key, ticket key, project id, project tag)."""
    out = []
    for i in range(start, start + n):
        project = projects[i % len(projects)]
        ticket = items_svc.create_item(db, project_id=project, title=f"ticket {i}",
                                       status="in_progress")
        wait = items_svc.create_item(db, project_id=project, title=f"question {i}",
                                     status="blocked", question="Which store?", options=["a", "b"])
        links_svc.create_link(db, a=ticket.id, b=wait.id, type_="dependency", project_id=project)
        alerts_svc.write_for_wait(db, wait, [user_id])
        out.append((wait.key, ticket.key, project, db.get(Project, project).tag))
    db.commit()
    return out


def _counted(db, fn):
    """The SELECTs `fn` ran, and what it returned. SELECTs only: what else a request emits
    (a PRAGMA on SQLite, transaction control on Postgres) is engine bookkeeping."""
    seen = []
    engine = db.get_bind()
    hook = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
    event.listen(engine, "before_cursor_execute", hook)
    try:
        out = fn()
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    return [s for s in seen if s.lstrip().upper().startswith("SELECT")], out


def _get(client, auth):
    r = client.get("/api/me/alerts", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_get_me_alerts_statement_count_is_flat_in_alerts(client, auth, db):
    me = _me(db)
    seeded = _seed_alerts(db, me, 5)
    small, body = _counted(db, lambda: _get(client, auth))
    assert body["unread"] == 5 and len(body["alerts"]) == 5

    seeded += _seed_alerts(db, me, 45, start=5)
    big, body = _counted(db, lambda: _get(client, auth))
    assert len(body["alerts"]) == 50, "the fixture must reach the size the bell was slow at"
    assert len(big) == len(small), (len(small), len(big), big)

    # Flat and WRONG is the failure this refactor could actually have: every alert must still
    # name its own wait, its own asker, its own project and that project's tag.
    assert {(a["wait"], a["ticket"], a["project"], a["project_tag"])
            for a in body["alerts"]} == set(seeded)
    assert all(a["impact"] == "blocks" for a in body["alerts"])


def test_get_me_alerts_statement_count_is_flat_in_readable_projects(client, auth, db):
    """The other half of the acceptance: 45 more projects the user can read, same count. A
    read that listed alerts per readable project, or re-checked access per alert, grows here."""
    me = _me(db)
    _seed_alerts(db, me, 5)
    small, body = _counted(db, lambda: _get(client, auth))
    before = len(readable_project_ids(db, me))

    for i in range(45):
        db.add(Project(id=f"ro{i}", name=f"Read Only {i:02d}", tag=f"RO{i}"))
        db.flush()
        db.add(Membership(user_id=me, project_id=f"ro{i}", role="member", access="read"))
    db.commit()
    assert len(readable_project_ids(db, me)) == before + 45

    big, after = _counted(db, lambda: _get(client, auth))
    assert after == body, "a project with no alerts in it adds nothing to the bell"
    assert len(big) == len(small), (len(small), len(big), big)


def test_a_project_the_user_lost_takes_its_alerts_with_it(client, auth, db):
    """GRPH-1535 D6 kept: access is still decided at read time, once for the list, and losing
    a project hides every alert in it — the batching did not turn the filter into a formality."""
    me = _me(db)
    db.add(Project(id="gone", name="Gone", tag="GONE"))
    db.flush()
    db.add(Membership(user_id=me, project_id="gone", role="member", access="read"))
    db.commit()
    kept = _seed_alerts(db, me, 3)
    lost = _seed_alerts(db, me, 2, projects=("gone",))

    body = _get(client, auth)
    assert {a["wait"] for a in body["alerts"]} == {w for w, _, _, _ in kept + lost}

    db.query(Membership).filter(Membership.user_id == me,
                                Membership.project_id == "gone").delete()
    db.commit()
    body = _get(client, auth)
    assert body["unread"] == 3
    assert {a["wait"] for a in body["alerts"]} == {w for w, _, _, _ in kept}
    assert all(a["project"] != "gone" for a in body["alerts"])


def test_the_list_matches_reading_each_alert_on_its_own(db):
    """The batched read and one alert read alone say the same thing. This is the claim the
    refactor rests on, and nothing else states it: a shared map keyed by the wrong id is flat,
    fast and wrong."""
    me = _me(db)
    _seed_alerts(db, me, 6)
    batched = alerts_svc.list_for_user(db, me)
    rows = db.scalars(select(Alert).where(Alert.user_id == me, Alert.cleared_at.is_(None))
                      .order_by(Alert.created_at.desc(), Alert.id.desc())).all()
    ids = [r.id for r in rows]
    db.expire_all()
    assert [a["id"] for a in batched["alerts"]] == ids
    assert batched["alerts"] == [alerts_svc.alert_dict(db, r) for r in rows]
    assert batched["unread"] == 6
