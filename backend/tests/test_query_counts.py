"""GRPH-1243 — read paths whose statement count must not grow with the data.

`measured()` used to `session.get` an agent and an item per delegation row; `cluster_board`
re-listed the project's items per unclassified item and `db.get` each cluster member. The
counts are pinned at two sizes so a per-row query comes back as a failure, not a slow page.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.models import Delegation
from app.services import delegation as dsvc
from app.services import fleet as fleet_svc

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
MEASURED_MAX = 8
ITEM_SELECTS_MAX = 8
LINK_SELECTS_MAX = 2


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
    import datetime as _dt
    dsvc._now = dsvc._now  # keep the module clock as is
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
