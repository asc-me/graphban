"""GRPH-1243 — read paths whose statement count must not grow with the data.

`measured()` used to `session.get` an agent and an item per delegation row; `cluster_board`
re-listed the project's items per unclassified item and `db.get` each cluster member. The
counts are pinned at two sizes so a per-row query comes back as a failure, not a slow page.

`tests/snapshots/grph1243.json` was written by THE PRE-CHANGE CODE (eabab860) on the fixture
below, so the snapshot test compares the batched paths against the per-row ones they replace.
Regenerate only on purpose: `GRPH1243_WRITE_SNAPSHOT=1 pytest tests/test_query_counts.py`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.models import Agent, Delegation, Item, Project
from app.services import delegation as dsvc
from app.services import fleet as fleet_svc
from app.services import links as links_svc

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
MEASURED_MAX = 8
FINISHED_BY_MODEL_MAX = 3
FLEET_STATUS_MAX = 22
FLEET_STATUS_REAL_MAX = 28
FLEET_ROUTE_MAX = 66
ITEM_SELECTS_MAX = 8
LINK_SELECTS_MAX = 2
SNAPSHOT = Path(__file__).parent / "snapshots" / "grph1243.json"
SNAPSHOT_1245 = Path(__file__).parent / "snapshots" / "grph1245.json"


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class _Count:
    def __init__(self, db):
        self.engine, self.n, self.sql = db.get_bind(), 0, []

    def _hit(self, conn, cursor, statement, *a, **k):
        self.n += 1
        self.sql.append(" ".join(statement.split())[:100])

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._hit)


@pytest.fixture()
def seeded(client, auth, db):
    proj = client.post("/api/projects", json={"name": "Counts"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "sup", "project_id": proj,
                                             "scopes": ["read", "write", "gate"]},
                      headers=auth).json()["plaintext"]
    agents = []
    for i in range(20):
        r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "register_agent", "arguments": {
                                              "label": f"a{i}",
                                              "capabilities": {"vendor": "v", "model": "m"}}}},
                        headers={"X-API-Key": key}).json()["result"]
        agents.append(r["structuredContent"]["agent_id"])
    items = [client.post("/api/items", json={"title": f"item {i}", "project_id": proj,
                                             "touchpoints": [] if i % 2 else [f"app/m{i % 7}/x.py"]},
                         headers=auth).json()["id"] for i in range(50)]

    # Each unclassified (odd) item links to its classified neighbour, so cluster_board's
    # predictions come from the pool's link map rather than from nothing.
    for i in range(1, 50, 2):
        links_svc.create_link(db, a=items[i], b=items[i - 1], type_="code", project_id=proj)

    def delegations(n, start=0):
        for i in range(start, start + n):
            db.add(Delegation(id=f"d{i}", project_id=proj, item_id=items[i % 50],
                              delegated_by=agents[0], agent_id=agents[i % 20], linked_by="seat",
                              lane="backend", requested_tier="cheap", declared_model="m",
                              declared_tier="cheap", outcome="signed_off", created_at=T0,
                              claimed_at=T0 + timedelta(minutes=1),
                              finished_at=T0 + timedelta(minutes=5), lease_seconds=600))
        db.commit()

    return proj, delegations


def test_measured_statement_count_is_flat_in_delegations(db, seeded):
    proj, delegations = seeded
    delegations(20)
    with _Count(db) as small:
        a = dsvc.measured(db, proj, window_days=10_000)
    db.expire_all()
    delegations(180, start=20)
    with _Count(db) as big:
        b = dsvc.measured(db, proj, window_days=10_000)
    assert small.n == big.n <= MEASURED_MAX, (small.n, big.n)
    assert a and b


def test_cluster_board_statement_count_is_flat_in_items(db, seeded):
    proj, _ = seeded
    with _Count(db) as c:
        board = fleet_svc.cluster_board(db, proj)
    # Per-predicted-item embedding spans (llm_call_spans, busy_timeout) are the provider
    # call's own bookkeeping and scale with items by design; the N+1 this pins is on the
    # item and link tables.
    items_q = [s for s in c.sql if s.startswith("SELECT items.")]
    links_q = [s for s in c.sql if s.startswith("SELECT links.")]
    assert board
    assert len(items_q) <= ITEM_SELECTS_MAX, (len(items_q), c.sql)
    assert len(links_q) <= LINK_SELECTS_MAX, len(links_q)


def test_finished_by_model_statement_count_is_flat_in_delegations(db, seeded):
    proj, delegations = seeded
    delegations(20)
    db.expire_all()
    with _Count(db) as small:
        a = dsvc.finished_by_model(db, proj, window_days=10_000)
    delegations(180, start=20)
    db.expire_all()
    with _Count(db) as big:
        b = dsvc.finished_by_model(db, proj, window_days=10_000)
    assert small.n == big.n <= FINISHED_BY_MODEL_MAX, (small.n, big.n)
    assert a[("v", "m")]["runs"] == 20 and b[("v", "m")]["runs"] == 200


def _register(client, auth, proj, n, start):
    """`n` more agents, a third holding a build lease, a third a review claim, every fourth
    silent past the TTL — the roster's per-agent fields all have something to say."""
    key = client.post("/api/api-keys", json={"name": f"k{start}", "project_id": proj,
                                             "scopes": ["read", "write", "gate"]},
                      headers=auth).json()["plaintext"]
    ids = []
    for i in range(start, start + n):
        r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "register_agent", "arguments": {
                                              "label": f"b{i}", "branch": f"br{i}",
                                              "capabilities": {"vendor": "v", "model": "m"}}}},
                        headers={"X-API-Key": key}).json()["result"]
        ids.append(r["structuredContent"]["agent_id"])
    return ids


def _diversify(db, proj, agent_ids, start):
    items = list(db.scalars(select(Item).where(Item.project_id == proj)
                            .order_by(Item.number)).all())
    for j, aid in enumerate(agent_ids):
        it = items[(start + j) % len(items)]
        if j % 3 == 0:
            it.claimed_by, it.status = aid, "in_progress"
        elif j % 3 == 1:
            it.review_claimed_by, it.status = aid, "review"
        if j % 4 == 0:
            db.get(Agent, aid).last_seen_at = T0
    db.commit()


def test_fleet_status_statement_count_is_flat_in_agents(client, auth, db, seeded):
    proj, delegations = seeded
    delegations(50)
    _diversify(db, proj, _register(client, auth, proj, 20, 0), 0)
    # The first call releases what the offline holders held — a write per released item, by
    # design and once. Counted from the second call on, which is the supervisor's steady tick.
    fleet_svc.fleet_status(db, proj)
    db.expire_all()
    with _Count(db) as small:
        a = fleet_svc.fleet_status(db, proj, minted_by="nobody")
    _diversify(db, proj, _register(client, auth, proj, 160, 20), 20)
    fleet_svc.fleet_status(db, proj)
    db.expire_all()
    with _Count(db) as big:
        b = fleet_svc.fleet_status(db, proj, minted_by="nobody")
    assert (a["total"], b["total"]) == (40, 200)
    assert any(x["holdings"] for x in b["agents"]), "fixture must exercise holdings"
    assert small.n == big.n <= FLEET_STATUS_MAX, (small.n, big.n)


def _seed_real_fleet(db, proj, agent_ids, minter, start):
    """Seats, enrolments, credentials and dismissals for `agent_ids`, so every per-agent and
    per-seat branch on the REST and minted_by paths has something to read. A third of the
    seats are consumed by their agent, a fifth are bound to an item, every seventh agent is
    dismissed, and the rest of the seats stay open."""
    from app.models import Enrolment

    items = list(db.scalars(select(Item).where(Item.project_id == proj)
                            .order_by(Item.number)).all())
    for j, aid in enumerate(agent_ids):
        bound = items[(start + j) % len(items)].id if j % 5 == 0 else None
        seat, _ = fleet_svc.issue_enrolment(db, project_id=proj, role="worker",
                                            wave=f"wave-{j % 3 + 1}", minted_by=minter,
                                            item_id=bound)
        fleet_svc.issue_enrolment(db, project_id=proj, role="planner", wave="wave-1",
                                  minted_by=minter)
        agent = db.get(Agent, aid)
        if j % 3 == 0:
            agent.enrolment_id = seat.id
            row = db.get(Enrolment, seat.id)
            row.consumed_at, row.consumed_by = T0, aid
        if j % 7 == 0:
            agent.dismissed_at = T0
    db.commit()


def _without_embedding(c):
    return sum(1 for q in c.sql if not q.startswith(("PRAGMA busy_timeout", "INSERT INTO llm_call_spans",
                                                     "SELECT code_nodes.")))


def _user_id(db):
    from app.models import User

    return db.scalar(select(User.id).where(User.email == "alex@ascme-labs.com"))


def _real_path_counts(client, auth, db, seeded, call):
    proj, delegations = seeded
    delegations(50)
    minter = _register(client, auth, proj, 1, 900)[0]
    first = _register(client, auth, proj, 20, 0)
    _diversify(db, proj, first, 0)
    _seed_real_fleet(db, proj, first, minter, 0)
    fleet_svc.fleet_status(db, proj)
    db.expire_all()
    with _Count(db) as small:
        a = call(proj, minter)
    more = _register(client, auth, proj, 160, 20)
    _diversify(db, proj, more, 20)
    _seed_real_fleet(db, proj, more, minter, 20)
    fleet_svc.fleet_status(db, proj)
    db.expire_all()
    with _Count(db) as big:
        b = call(proj, minter)
    return small, big, a, b


def test_fleet_status_caller_user_statement_count_is_flat_in_agents(client, auth, db, seeded):
    uid = _user_id(db)
    proj, _ = seeded
    small, big, a, b = _real_path_counts(
        client, auth, db, seeded,
        lambda proj, minter: fleet_svc.fleet_status(db, proj, caller_user_id=uid))
    assert b["total"] > a["total"] >= 20
    assert small.n == big.n <= FLEET_STATUS_REAL_MAX, (small.n, big.n, big.sql)


def test_fleet_status_minted_by_statement_count_is_flat_in_agents(client, auth, db, seeded):
    small, big, a, b = _real_path_counts(
        client, auth, db, seeded,
        lambda proj, minter: fleet_svc.fleet_status(db, proj, minted_by=minter))
    assert len(b["seats"]) > len(a["seats"]) >= 20
    assert {s["state"] for s in b["seats"]} >= {"consumed", "unused"}
    assert small.n == big.n <= FLEET_STATUS_REAL_MAX, (small.n, big.n, big.sql)


def test_get_api_fleet_statement_count_is_flat_in_agents(client, auth, db, seeded):
    def call(p, minter):
        r = client.get("/api/fleet", params={"project_id": p}, headers=auth)
        assert r.status_code == 200, r.text
        return r.json()

    small, big, a, b = _real_path_counts(client, auth, db, seeded, call)
    assert b["total"] > a["total"] >= 20
    # cluster_board's per-prediction embedding spans (llm_call_spans, busy_timeout, code_nodes)
    # are the provider call's own bookkeeping and vary with its cache, not with agents; the
    # roster, seat, credential and review-queue reads are what this pins.
    sn, bn = _without_embedding(small), _without_embedding(big)
    assert sn == bn <= FLEET_ROUTE_MAX, (sn, bn, big.sql)


def test_cluster_board_item_and_link_reads_are_flat_in_items(client, auth, db, seeded):
    proj, _ = seeded
    with _Count(db) as small:
        fleet_svc.cluster_board(db, proj)
    for i in range(50, 100):
        client.post("/api/items", json={"title": f"item {i}", "project_id": proj,
                                        "touchpoints": [] if i % 2 else [f"app/m{i % 7}/x.py"]},
                    headers=auth)
    db.expire_all()
    with _Count(db) as big:
        fleet_svc.cluster_board(db, proj)

    def reads(c, table):
        return sum(1 for s in c.sql if s.startswith(f"SELECT {table}."))

    assert reads(small, "items") == reads(big, "items") <= ITEM_SELECTS_MAX
    assert reads(small, "links") == reads(big, "links") <= LINK_SELECTS_MAX


# ---- before/after identity ---------------------------------------------------------------

_VOLATILE = {"last_seen_at", "registered_at", "expires_at", "credential", "claimed_at",
             "lease_expires_at", "created_at", "updated_at"}


def _normalise(obj, names):
    """Swap generated ids for stable names and drop wall-clock and secret-derived fields, so
    two runs of the same fixture compare equal and nothing else does."""
    if isinstance(obj, dict):
        return {names.get(k, k): _normalise(v, names) for k, v in obj.items()
                if k not in _VOLATILE}
    if isinstance(obj, (list, tuple)):
        return [_normalise(v, names) for v in obj]
    if isinstance(obj, str):
        return names.get(obj, obj)
    return obj


def _snapshot(client, auth, db, seeded):
    proj, delegations = seeded
    delegations(200)
    _diversify(db, proj, _register(client, auth, proj, 30, 0), 0)
    # Settle the offline holders' releases first, the same as the supervisor's second tick.
    fleet_svc.fleet_status(db, proj)
    db.expire_all()
    names = {proj: "PROJECT"}
    for a in db.scalars(select(Agent).where(Agent.project_id == proj)).all():
        names[a.id] = f"agent:{a.key}"
    for i in db.scalars(select(Item).where(Item.project_id == proj)).all():
        names[i.id] = f"item:{i.key}"
    names[db.get(Project, proj).id] = "PROJECT"
    status = fleet_svc.fleet_status(db, proj)
    return _normalise({
        "measured": dsvc.measured(db, proj, window_days=10_000),
        "finished_by_model": {f"{v}/{m}": c for (v, m), c in sorted(
            dsvc.finished_by_model(db, proj, window_days=10_000).items())},
        "fleet_status": status,
        "cluster_board": fleet_svc.cluster_board(db, proj),
    }, names)


def test_outputs_match_the_pre_batching_snapshot(client, auth, db, seeded):
    got = json.loads(json.dumps(_snapshot(client, auth, db, seeded), default=str))
    if os.environ.get("GRPH1243_WRITE_SNAPSHOT"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        pytest.skip("snapshot written")
    want = json.loads(SNAPSHOT.read_text())
    # The fixture must reach every path the batching changed, or equality proves nothing.
    assert want["measured"] and want["finished_by_model"]
    assert any(c["predicted"] for c in want["cluster_board"])
    assert any(a["holdings"] for a in want["fleet_status"]["agents"])
    for part in want:
        assert got[part] == want[part], part


# ---- GRPH-1245: the real caller paths --------------------------------------------------------


def _seat_rows(rows):
    """Seat ids are random uuids, so the list is compared on what a seat says, sorted."""
    return sorted(({k: v for k, v in r.items() if k not in ("id", "expires_at")} for r in rows),
                  key=lambda r: json.dumps(r, sort_keys=True, default=str))


def test_real_path_outputs_match_the_pre_batching_snapshot(client, auth, db, seeded):
    """`tests/snapshots/grph1245.json` was written by the code BEFORE `_live_reviewer_tools`
    read its seats in one query. Regenerate only on purpose:
    `GRPH1245_WRITE_SNAPSHOT=1 pytest tests/test_query_counts.py`."""
    proj, delegations = seeded
    delegations(100)
    minter = _register(client, auth, proj, 1, 900)[0]
    agent_ids = _register(client, auth, proj, 30, 0)
    _diversify(db, proj, agent_ids, 0)
    _seed_real_fleet(db, proj, agent_ids, minter, 0)
    for n, it in enumerate(db.scalars(select(Item).where(Item.project_id == proj,
                                                        Item.status == "review")
                                      .order_by(Item.number)).all()):
        it.description = "## Acceptance\n- " + ("[needs: human-only] " if n % 2 else "") + "it works"
    db.commit()
    fleet_svc.fleet_status(db, proj)
    db.expire_all()
    names = {proj: "PROJECT", minter: "agent:minter"}
    for a in db.scalars(select(Agent).where(Agent.project_id == proj)).all():
        names[a.id] = f"agent:{a.key}"
    for i in db.scalars(select(Item).where(Item.project_id == proj)).all():
        names[i.id] = f"item:{i.key}"
    for seat in db.scalars(select(fleet_svc.Enrolment)).all():
        names[seat.id] = "seat"
    route = client.get("/api/fleet", params={"project_id": proj}, headers=auth).json()
    got = _normalise({
        "caller_user": fleet_svc.fleet_status(db, proj, caller_user_id=_user_id(db)),
        "minted_by": fleet_svc.fleet_status(db, proj, minted_by=minter),
        "route": route,
    }, names)
    got["minted_by"]["seats"] = _seat_rows(got["minted_by"]["seats"])
    got["route"]["seats"] = _seat_rows(got["route"]["seats"])
    got["route"]["credentials"] = sorted(
        ({k: v for k, v in c.items() if k not in ("id", "prefix")}
         for c in got["route"]["credentials"]), key=lambda c: json.dumps(c, sort_keys=True))
    got = json.loads(json.dumps(got, default=str))
    if os.environ.get("GRPH1245_WRITE_SNAPSHOT"):
        SNAPSHOT_1245.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        pytest.skip("snapshot written")
    want = json.loads(SNAPSHOT_1245.read_text())
    assert want["minted_by"]["seats"] and want["route"]["review_queue"]
    assert any(r["needs_human"] for r in want["route"]["review_queue"])
    assert any(a["assigned"] for a in want["caller_user"]["agents"])
    for part in want:
        assert got[part] == want[part], part
