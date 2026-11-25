"""`fleet_status` carries the server's unready line list (GRPH-1839).

`items.unready_line_items` was returned only on a line seat's `claim_cluster`, which a planner
and a supervisor never call, so a line item stuck for want of an acceptance clause or a
touchpoint was invisible on every read they make. `fleet_status` now carries it as
`line_unready`: a list (empty = looked, found none) when the project has a line, and no key
at all when it has none.
"""
from __future__ import annotations

import pytest

from app.services import items as items_svc
from app.services import lines as lines_svc

ACCEPT = "Fix it.\n\n## Acceptance\n- the thing works\n"


def _mcp(client, key, name, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Lined"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "planner", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _fix(db, proj, title, *, line, desc=ACCEPT, tp=("a/x.py",)):
    return items_svc.create_item(db, title=title, project_id=proj, status="next", kind="fix",
                                 description=desc, line_id=line, touchpoints=list(tp))


def test_fleet_status_names_unready_line_items(client, key, db, proj):
    line = lines_svc.create_line(db, proj)
    no_acc = _fix(db, proj, "no acceptance", line=line.id, desc="just prose", tp=("a/1.py",))
    no_tp = _fix(db, proj, "no touchpoint", line=line.id, tp=())
    ready = _fix(db, proj, "ready", line=line.id, tp=("a/2.py",))
    got = _mcp(client, key, "fleet_status")
    assert got["line_unready"] == [
        {"id": no_acc.key, "missing": ["acceptance"]},
        {"id": no_tp.key, "missing": ["touchpoints"]},
    ]
    assert ready.key not in {u["id"] for u in got["line_unready"]}


def test_fleet_status_line_unready_empty_when_all_ready(client, key, db, proj):
    line = lines_svc.create_line(db, proj)
    _fix(db, proj, "ready one", line=line.id, tp=("a/1.py",))
    _fix(db, proj, "ready two", line=line.id, tp=("a/2.py",))
    assert _mcp(client, key, "fleet_status")["line_unready"] == []


def test_fleet_status_without_a_line_has_no_line_unready(client, key, db, proj):
    got = _mcp(client, key, "fleet_status")
    assert "line_unready" not in got
