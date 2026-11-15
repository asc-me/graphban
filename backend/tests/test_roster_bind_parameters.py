"""A roster bigger than the driver's parameter limit still lists (GRPH-1668).

`list_agents` loaded the whole roster and then asked for held items with
`claimed_by IN (ids) OR review_claimed_by IN (ids)`, which is two bind parameters per
agent. On ubuntu-srv the agentledger project passed 32,767 agents, and from then on every
statement went over Postgres's 65,535-parameter limit. `propose_allocation` returned
"internal error", and every `gbfleet up`/`until` died before it spawned anything.
SQLite's limit is 32,766, so a roster of half that size already breaks this path there.

The first test builds a roster past both limits, so it fails on the old code on either
engine. The second shrinks the batch size so that the holdings and the bound seats are
split across several batches, and checks that none of them is lost at a batch boundary.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, insert

from app.models import Agent, ApiKey, Enrolment, Item, Project, User
from app.services import fleet as fleet_svc

#: Past Postgres's limit at two parameters per agent, and past SQLite's at one.
BIG = 33_000


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(db):
    db.add(Project(id="fleet", name="Fleet", tag="FL"))
    owner = User(id="u_fleet", name="Fleet Owner", handle="fleet", email="fleet@example.com",
                 initials="FO", password_hash="x")
    db.add(owner)
    db.flush()
    db.add(ApiKey(id="k_fleet", user_id=owner.id, project_id="fleet", name="fleet-key",
                  prefix="gb_sk_ab12", hashed_key="x", scopes=["read", "write"], roles=[]))
    db.commit()
    return "fleet"


def _agents(db, proj, n):
    db.execute(insert(Agent), [
        {"id": f"a_{i:06d}", "project_id": proj, "number": i, "api_key_id": "k_fleet",
         "label": "", "active_role": "worker", "capabilities": {}, "worktree": "",
         "branch": "", "state": "idle"}
        for i in range(1, n + 1)])
    db.commit()


def _seat(db, proj, seat_id, item_id):
    db.add(Enrolment(id=seat_id, project_id=proj, code_hash=seat_id, role="worker",
                     item_id=item_id,
                     expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    db.commit()


def _max_params(db):
    """Record the largest parameter count of any statement the session sends."""
    seen = {"max": 0}

    def count(conn, cursor, statement, parameters, context, executemany):
        if not executemany and parameters:
            seen["max"] = max(seen["max"], len(parameters))

    event.listen(db.get_bind(), "before_cursor_execute", count)
    return seen, lambda: event.remove(db.get_bind(), "before_cursor_execute", count)


def test_a_roster_past_the_parameter_limit_lists_with_its_holdings(db, proj):
    _agents(db, proj, BIG)
    last = f"a_{BIG:06d}"
    db.add_all([
        Item(id="i_build", project_id=proj, number=1, title="b", status="in_progress",
             claimed_by="a_000001"),
        Item(id="i_rev", project_id=proj, number=2, title="r", status="review",
             review_claimed_by=last),
        Item(id="i_bound", project_id=proj, number=3, title="s", status="in_progress",
             claimed_by="a_020000"),
    ])
    db.commit()
    _seat(db, proj, "seat_1", "i_bound")
    db.query(Agent).filter(Agent.id == "a_020000").update({"enrolment_id": "seat_1"})
    db.commit()

    seen, stop = _max_params(db)
    try:
        roster = fleet_svc.list_agents(db, proj)
    finally:
        stop()

    assert len(roster) == BIG
    rows = {r["id"]: r for r in roster}
    assert [h["stored_id"] for h in rows["a_000001"]["holdings"]] == ["i_build"]
    assert [(h["stored_id"], h["hold"]) for h in rows[last]["holdings"]] == [("i_rev", "review")]
    assert rows["a_020000"]["assigned"]["item"] == "FL-3"
    assert rows["a_020000"]["assigned"]["state"] == "claimed"
    # Two IN lists of one batch each is the most any roster statement may bind.
    assert 0 < seen["max"] <= 2 * fleet_svc.ROSTER_BATCH


def test_holdings_and_seats_survive_the_batch_boundaries(db, proj, monkeypatch):
    monkeypatch.setattr(fleet_svc, "ROSTER_BATCH", 2)
    _agents(db, proj, 7)
    for n, aid in enumerate(["a_000002", "a_000003", "a_000006"], start=1):
        db.add(Item(id=f"i_{n}", project_id=proj, number=n, title="t", status="in_progress",
                    claimed_by=aid))
    # Built by one agent and reviewed by another in a different batch: both rows carry it.
    db.add(Item(id="i_both", project_id=proj, number=9, title="t", status="review",
                claimed_by="a_000001", review_claimed_by="a_000007"))
    db.commit()
    for aid, item in [("a_000003", "i_2"), ("a_000006", "i_3")]:
        _seat(db, proj, f"seat_{aid}", item)
        db.query(Agent).filter(Agent.id == aid).update({"enrolment_id": f"seat_{aid}"})
    db.commit()

    rows = {r["id"]: r for r in fleet_svc.list_agents(db, proj)}

    held = {aid: sorted(h["stored_id"] for h in r["holdings"]) for aid, r in rows.items()}
    assert held == {"a_000001": ["i_both"], "a_000002": ["i_1"], "a_000003": ["i_2"],
                    "a_000004": [], "a_000005": [], "a_000006": ["i_3"],
                    "a_000007": ["i_both"]}
    assert {aid for aid, r in rows.items() if r.get("assigned")} == {"a_000003", "a_000006"}
