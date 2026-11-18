"""Line-scoped seats on the server (GRPH-1685).

A seat stores `line_id` the way it stores `prd_id`; `claim_cluster` filters the pool by it
before partitioning; a scoped seat takes clusters in blocking-fix, security, age order; and a
line item with no acceptance clause or no touchpoint is reported, not claimed.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import Link, utcnow
from app.services import fleet as fleet_svc
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
    return r.json()["result"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Lined"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
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


def _seat_agent(client, key, db, proj, label, **scope) -> str:
    _row, code = fleet_svc.issue_enrolment(db, project_id=proj, role="worker", **scope)
    res = _mcp(client, key, "register_agent", {"label": label, "enrolment_code": code})
    assert not res.get("isError"), res
    return res["structuredContent"]["agent_id"]


def _fix(db, proj, title, *, line=None, prd=None, tags=None, desc=ACCEPT, tp=("a/x.py",),
         age_days=0, kind="fix"):
    it = items_svc.create_item(db, title=title, project_id=proj, status="next", kind=kind,
                               tags=tags or [], description=desc, line_id=line, prd_id=prd,
                               touchpoints=list(tp))
    if age_days:
        it.created_at = utcnow() - timedelta(days=age_days)
        db.commit()
    return it


def test_line_seat_claims_line_item_not_prd_item(client, key, db, proj):
    line = lines_svc.create_line(db, proj)
    _fix(db, proj, "line fix", line=line.id, tp=("l/one.py",))
    _fix(db, proj, "feature", kind="feature", tp=("p/two.py",))
    seat = _seat_agent(client, key, db, proj, "line", line_id=line.id)
    got = fleet_svc.claim_cluster(db, agent_id=seat, project_id=proj)
    assert got["claimed"] and [i["title"] for i in got["items"]] == ["line fix"]
    again = fleet_svc.claim_cluster(db, agent_id=seat, project_id=proj)
    assert again["claimed"] is False


def test_prd_seat_does_not_claim_line_item(client, auth, key, db, proj):
    line = lines_svc.create_line(db, proj)
    prd = client.post("/api/prds", json={"title": "p", "project_id": proj}, headers=auth).json()
    _fix(db, proj, "line fix", line=line.id)
    seat = _seat_agent(client, key, db, proj, "prd", prd_id=prd["id"])
    got = fleet_svc.claim_cluster(db, agent_id=seat, project_id=proj)
    assert got["claimed"] is False


def test_a_seat_cannot_hold_both_scopes(db, proj):
    line = lines_svc.create_line(db, proj)
    with pytest.raises(ValueError, match="both"):
        fleet_svc.issue_enrolment(db, project_id=proj, role="worker",
                                  prd_id="x", line_id=line.id)


def test_security_one_item_cluster_before_larger_cluster(client, key, db, proj):
    line = lines_svc.create_line(db, proj)
    _fix(db, proj, "ord1", line=line.id, tp=("m/a.py",), age_days=9)
    _fix(db, proj, "ord2", line=line.id, tp=("m/a.py",), age_days=9)
    _fix(db, proj, "sec", line=line.id, tp=("s/b.py",), tags=["security"])
    seat = _seat_agent(client, key, db, proj, "line", line_id=line.id)
    got = fleet_svc.claim_cluster(db, agent_id=seat, project_id=proj)
    assert [i["title"] for i in got["items"]] == ["sec"]


def test_blocking_fix_before_security_and_age_breaks_ties(client, auth, key, db, proj):
    line = lines_svc.create_line(db, proj)
    prd = client.post("/api/prds", json={"title": "p", "project_id": proj}, headers=auth).json()
    stored = prd["id"]
    _fix(db, proj, "sec", line=line.id, tp=("s/b.py",), tags=["security"])
    _fix(db, proj, "old", line=line.id, tp=("o/c.py",), age_days=30)
    feat = _fix(db, proj, "feat", prd=stored, kind="feature", tp=("f/d.py",))
    block = _fix(db, proj, "blocker", prd=stored, tp=("b/e.py",))
    db.add(Link(type="dependency", a=feat.id, b=block.id, project_id=proj))
    db.commit()
    seat = _seat_agent(client, key, db, proj, "prd", prd_id=stored)
    got = fleet_svc.claim_cluster(db, agent_id=seat, project_id=proj, max_items=1)
    assert [i["title"] for i in got["items"]] == ["blocker"]

    line_seat = _seat_agent(client, key, db, proj, "line", line_id=line.id)
    got = fleet_svc.claim_cluster(db, agent_id=line_seat, project_id=proj)
    assert [i["title"] for i in got["items"]] == ["sec"]
    other = _seat_agent(client, key, db, proj, "line2", line_id=line.id)
    got = fleet_svc.claim_cluster(db, agent_id=other, project_id=proj)
    assert [i["title"] for i in got["items"]] == ["old"]


def test_unscoped_order_is_unchanged(client, key, db, proj):
    line = lines_svc.create_line(db, proj)
    _fix(db, proj, "big1", line=line.id, tp=("m/a.py",))
    _fix(db, proj, "big2", line=line.id, tp=("m/a.py",))
    _fix(db, proj, "sec", line=line.id, tp=("s/b.py",), tags=["security"])
    seat = _seat_agent(client, key, db, proj, "free")
    got = fleet_svc.claim_cluster(db, agent_id=seat, project_id=proj)
    assert len(got["items"]) == 2


def test_unready_line_items_are_unclaimable_and_listed(client, key, db, proj):
    line = lines_svc.create_line(db, proj)
    no_acc = _fix(db, proj, "no acceptance", line=line.id, desc="just prose", tp=("a/1.py",))
    no_tp = _fix(db, proj, "no touchpoint", line=line.id, tp=())
    both = _fix(db, proj, "neither", line=line.id, desc="", tp=())
    seat = _seat_agent(client, key, db, proj, "line", line_id=line.id)
    got = fleet_svc.claim_cluster(db, agent_id=seat, project_id=proj)
    assert got["claimed"] is False
    assert items_svc.claim_next(db, agent_id=seat, project_id=proj) is None
    assert items_svc.unready_line_items(db, proj) == [
        {"id": no_acc.key, "missing": ["acceptance"]},
        {"id": no_tp.key, "missing": ["touchpoints"]},
        {"id": both.key, "missing": ["acceptance", "touchpoints"]},
    ]
    res = _mcp(client, key, "claim_cluster", {"agent_id": seat})
    assert len(res["structuredContent"]["unready"]) == 3


def test_unready_list_empty_when_none(client, key, db, proj):
    line = lines_svc.create_line(db, proj)
    _fix(db, proj, "ready", line=line.id)
    seat = _seat_agent(client, key, db, proj, "line", line_id=line.id)
    res = _mcp(client, key, "claim_cluster", {"agent_id": seat})
    assert res["structuredContent"]["unready"] == []
