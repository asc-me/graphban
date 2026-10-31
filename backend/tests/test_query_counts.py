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
ITEM_SELECTS_MAX = 8
LINK_SELECTS_MAX = 2
SNAPSHOT = Path(__file__).parent / "snapshots" / "grph1243.json"


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
