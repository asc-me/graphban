"""The project switcher's rows (GRPH-1131 / PRD-P59 D3).

Each row draws `24 items · 3 agents · main`, a `2 live` count, or `idle 12d`. `ProjectOut`
carries none of those numbers, and counting them by fetching each project's collections is the
GRPH-431 hang — so they come from one fixed-cost read, scoped by the same authz as the list.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from app.models import Agent, Event, Project, User
from app.services import items as items_svc
from app.services import projects as proj_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _owner(db) -> User:
    return db.scalars(select(User).where(User.email == "alex@ascme-labs.com")).one()


def _project(db, name, tag, owner_id=None) -> Project:
    p = proj_svc.create_project(db, name=name, owner_user_id=owner_id or _owner(db).id, tag=tag)
    db.commit()
    return p


def _agent(db, project_id, n, *, seen_ago=0, state="idle", dismissed=False) -> Agent:
    now = datetime.now(timezone.utc)
    a = Agent(id=f"{project_id}-a{n}", project_id=project_id, number=n, state=state,
              last_seen_at=now - timedelta(seconds=seen_ago),
              dismissed_at=now if dismissed else None)
    db.add(a)
    db.commit()
    return a


def _row(rows, project_id) -> dict:
    match = [r for r in rows if r["id"] == project_id]
    assert len(match) == 1, f"{project_id} not in {[r['id'] for r in rows]}"
    return match[0]


def _second_tenant(client, db) -> tuple[dict, Project]:
    """A user who is not a member of anything the seed user owns, owning one project."""
    from app.security.passwords import hash_password

    db.add(User(id="u_other", name="Other", handle="other", email="other@example.com",
                initials="O", password_hash=hash_password("graphban")))
    db.commit()
    theirs = _project(db, "Theirs", "THR", owner_id="u_other")
    for i in range(5):
        items_svc.create_item(db, title=f"theirs {i}", project_id=theirs.id)
    _agent(db, theirs.id, 1)
    token = client.post("/api/auth/login", json={
        "email": "other@example.com", "password": "graphban"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, theirs


def test_one_request_returns_every_readable_project_and_no_other(client, auth, db):
    other_hdr, theirs = _second_tenant(client, db)
    mine = _project(db, "Mine", "MIN")
    for i in range(3):
        items_svc.create_item(db, title=f"mine {i}", project_id=mine.id)

    r = client.get("/api/projects/summaries", headers=auth)
    assert r.status_code == 200, r.text
    rows = r.json()
    listed = {p["id"] for p in client.get("/api/projects", headers=auth).json()}
    assert {row["id"] for row in rows} == listed  # same authz as the project list
    assert theirs.id not in listed
    assert _row(rows, mine.id)["item_count"] == 3

    # And the refusal the other way: the second tenant sees theirs, counted, and nothing of ours.
    rows = client.get("/api/projects/summaries", headers=other_hdr).json()
    assert [row["id"] for row in rows] == [theirs.id]
    assert rows[0]["item_count"] == 5 and rows[0]["agent_count"] == 1


def test_the_service_never_counts_outside_the_ids_it_was_given(client, db):
    """The router passes the readable list; the service must filter by it, not trust it."""
    mine = _project(db, "Mine", "MIN")
    other = _project(db, "Other", "OTH")
    items_svc.create_item(db, title="x", project_id=other.id)
    rows = proj_svc.project_summaries(db, [mine.id])
    assert [r["id"] for r in rows] == [mine.id]
    assert proj_svc.project_summaries(db, []) == []


def test_an_empty_project_reports_nulls_not_zeroes(client, auth, db):
    empty = _project(db, "Empty", "EMP")
    row = _row(client.get("/api/projects/summaries", headers=auth).json(), empty.id)
    assert row["item_count"] == 0 and row["agent_count"] == 0 and row["live_agents"] == 0
    assert row["last_activity_at"] is None
    assert row["default_branch"] is None  # unset is unmeasured, never "main"


def test_default_branch_is_the_gitops_base_branch(client, auth, db):
    p = _project(db, "Branchy", "BRN")
    p.gitops_base_branch = "trunk"
    db.commit()
    row = _row(client.get("/api/projects/summaries", headers=auth).json(), p.id)
    assert row["default_branch"] == "trunk"


def test_a_linked_box_reads_the_cloud_branch_not_the_local_column(client, db, monkeypatch):
    """Linked, the local gitops columns are `was` only. One cloud fetch serves every row; an
    unreachable cloud is null, never the stale local value."""
    from app.schemas import GitopsField
    from app.services import code_sync, gitops

    p = _project(db, "Linked", "LNK")
    p.gitops_base_branch = "stale-local"
    db.commit()
    monkeypatch.setattr(code_sync, "link_status", lambda _db: {"linked": True})

    monkeypatch.setattr(gitops, "fetch_cloud_gitops", lambda _db: None)
    assert _row(proj_svc.project_summaries(db, [p.id]), p.id)["default_branch"] is None

    cloud = gitops.resolve_local(db, None)
    cloud.fields.base_branch = GitopsField(value="develop", source="org")
    calls = []
    monkeypatch.setattr(gitops, "fetch_cloud_gitops", lambda _db: calls.append(1) or cloud)
    other = _project(db, "Linked 2", "LN2")
    rows = proj_svc.project_summaries(db, [p.id, other.id])
    assert [r["default_branch"] for r in rows] == ["develop", "develop"]
    assert len(calls) == 1


def test_last_activity_reads_items_and_the_audit_ledger(client, db):
    p = _project(db, "Active", "ACT")
    it = items_svc.create_item(db, title="x", project_id=p.id)
    item_ts = proj_svc._aware(db.get(type(it), it.id).updated_at)
    row = _row(proj_svc.project_summaries(db, [p.id]), p.id)
    assert row["last_activity_at"] == item_ts

    later = datetime.now(timezone.utc) + timedelta(hours=1)
    db.add(Event(ts=later, actor_type="user", surface="rest", action="update_project",
                 project_id=p.id))
    db.commit()
    row = _row(proj_svc.project_summaries(db, [p.id]), p.id)
    assert row["last_activity_at"] == later


def test_agents_roster_and_live_presence(client, db):
    p = _project(db, "Fleet", "FLT")
    _agent(db, p.id, 1)                                # live
    _agent(db, p.id, 2, state="working")               # live
    _agent(db, p.id, 3, seen_ago=24 * 3600)            # offline: on the roster, not live
    _agent(db, p.id, 4, state="quarantined")           # heartbeating but quarantined
    _agent(db, p.id, 5, dismissed=True)                # dismissed: off the roster entirely
    row = _row(proj_svc.project_summaries(db, [p.id]), p.id)
    assert row["agent_count"] == 4
    assert row["live_agents"] == 2


def _selects(db, fn) -> list[str]:
    seen = []
    engine = db.get_bind()
    hook = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
    event.listen(engine, "before_cursor_execute", hook)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    return [s for s in seen if s.lstrip().upper().startswith("SELECT")]


def test_the_statement_count_does_not_grow_with_items_or_projects(client, db):
    """Asserted as "does not grow", not a magic number — the absolute count varies by engine.
    Ids are read before `expire_all` so the hook sees only the service's own statements."""
    a = _project(db, "A", "AAA").id
    items_svc.create_item(db, title="one", project_id=a)
    _agent(db, a, 1)
    db.expire_all()
    small = len(_selects(db, lambda: proj_svc.project_summaries(db, [a])))

    b = _project(db, "B", "BBB").id
    for pid in (a, b):
        for i in range(12):
            items_svc.create_item(db, title=f"more {i}", project_id=pid)
        _agent(db, pid, 7)
    db.expire_all()
    rows = []
    big = len(_selects(db, lambda: rows.extend(proj_svc.project_summaries(db, [a, b]))))
    assert _row(rows, a)["item_count"] == 13 and _row(rows, b)["item_count"] == 12
    assert big == small
