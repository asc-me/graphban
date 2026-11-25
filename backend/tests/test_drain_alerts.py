"""Drain outcome notices become alerts (GRPH-1913, P99 S3).

Sabotage: drop the `row.title == title and row.body == body` early `continue` in
`alerts.post_drain_notice` and the identical-post test fails (a second push). Drop the
`DRAIN_PUSH_GAP` test and a reason change pushes again. Drop the `prd.project_id not in
writable` check in the route and the stranger test fails.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import Alert, Membership, Prd, Project, utcnow
from app.services import prds as prds_svc
from app.services import push as push_svc


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Drains"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "k", "project_id": proj,
                                              "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def pushes(monkeypatch):
    monkeypatch.setattr(settings, "vapid_public_key", "BPUB")
    monkeypatch.setattr(settings, "vapid_private_key", "PRIV")
    calls = []

    def fake(*, subscription_info, data=None, **kw):
        calls.append(json.loads(data))
        return SimpleNamespace(status_code=201)

    monkeypatch.setattr(push_svc, "webpush", fake)
    return calls


@pytest.fixture()
def prd(db, proj):
    p = prds_svc.create_prd(db, title="Drain me", project_id=proj)
    owner = db.query(Membership).filter(Membership.project_id == proj,
                                        Membership.role == "owner").first().user_id
    p.created_by = owner
    db.commit()
    push_svc.subscribe(db, owner, "https://push.test/a", "p256", "auth")
    return p


def _post(client, key, prd, outcome, items=(), **extra):
    r = client.post("/api/fleet/drain-notices", headers={"X-API-Key": key},
                    json={"prd": prd.id, "outcome": outcome,
                          "open_items": [{"item": i, "reason": why} for i, why in items], **extra})
    return r


def _rows(db, prd, live=True):
    db.expire_all()
    q = db.query(Alert).filter(Alert.prd_id == prd.id)
    if live:
        q = q.filter(Alert.cleared_at.is_(None))
    return q.all()


def test_drained_writes_one_alert_listed_and_pushed(client, auth, key, prd, db, pushes):
    assert _post(client, key, prd, "drained").status_code == 200
    [row] = _rows(db, prd)
    assert row.kind == "drain_drained" and row.item_id == ""
    listed = client.get("/api/me/alerts", headers=auth).json()["alerts"]
    assert [a["kind"] for a in listed] == ["drain_drained"] and listed[0]["wait"] is None
    assert listed[0]["prd"]
    assert len(pushes) == 1 and "drained" in pushes[0]["title"]


def test_the_push_links_to_the_prd_page(client, key, prd, db, proj, pushes, monkeypatch):
    _post(client, key, prd, "drained")
    assert pushes[0]["url"] == f"/prds/{prd.id}"
    [row] = _rows(db, prd)
    tag = db.get(Project, proj).tag
    monkeypatch.setattr(settings, "hosted_mode", True)
    assert json.loads(push_svc._payload(db, row))["url"] == f"/p/{tag}/prds/{prd.id}"


def test_a_drain_alert_carries_no_wait_drain_field(client, auth, key, prd, pushes):
    _post(client, key, prd, "drained")
    [listed] = client.get("/api/me/alerts", headers=auth).json()["alerts"]
    assert "drain" not in listed and listed["impact"] == "drain"


def test_stalled_body_lists_each_item_and_reason(client, key, prd, db, pushes):
    _post(client, key, prd, "stalled", [("GRPH-1", "rework hold"), ("GRPH-2", "no PR")])
    [row] = _rows(db, prd)
    assert row.kind == "drain_stalled"
    assert "GRPH-1: rework hold" in row.body and "GRPH-2: no PR" in row.body


def test_same_stalled_twice_is_one_alert_one_push(client, key, prd, db, pushes):
    for _ in range(2):
        _post(client, key, prd, "stalled", [("GRPH-1", "hold")])
    assert len(_rows(db, prd, live=False)) == 1 and len(pushes) == 1


def test_changed_reason_updates_in_place_without_a_push(client, key, prd, db, pushes):
    _post(client, key, prd, "stalled", [("GRPH-1", "hold")])
    _post(client, key, prd, "stalled", [("GRPH-1", "dependency link")])
    [row] = _rows(db, prd)
    assert "dependency link" in row.body and len(pushes) == 1
    row.pushed_at = utcnow() - timedelta(minutes=31)
    db.commit()
    _post(client, key, prd, "stalled", [("GRPH-1", "salvage branch")])
    assert len(pushes) == 2 and len(_rows(db, prd, live=False)) == 1


def test_stalled_then_drained_replaces_the_row_and_pushes(client, key, prd, db, pushes):
    _post(client, key, prd, "stalled", [("GRPH-1", "hold")])
    _post(client, key, prd, "drained")
    assert [r.kind for r in _rows(db, prd)] == ["drain_drained"]
    assert len(_rows(db, prd, live=False)) == 2 and len(pushes) == 2


def test_working_clears_the_live_alert(client, key, prd, db, pushes):
    _post(client, key, prd, "stalled", [("GRPH-1", "hold")])
    _post(client, key, prd, "working")
    assert _rows(db, prd) == [] and len(_rows(db, prd, live=False)) == 1


def test_a_prd_with_no_owner_alerts_the_project_owners(client, key, prd, db, proj, pushes):
    prd.created_by = None
    db.commit()
    _post(client, key, prd, "drained")
    owners = {m.user_id for m in db.query(Membership).filter(
        Membership.project_id == proj, Membership.role == "owner")}
    assert owners and {r.user_id for r in _rows(db, prd)} == owners


def test_a_key_from_another_project_cannot_post(client, auth, prd, db, pushes):
    other = client.post("/api/projects", json={"name": "Other"}, headers=auth).json()["id"]
    stranger = client.post("/api/api-keys", json={"name": "s", "project_id": other,
                                                  "scopes": ["read", "write"]},
                           headers=auth).json()["plaintext"]
    assert _post(client, stranger, prd, "drained").status_code == 404
    assert _post(client, stranger, prd, "drained", project_id=prd.project_id).status_code == 404
    assert _rows(db, prd, live=False) == []


def test_an_unknown_outcome_is_422(client, key, prd):
    assert _post(client, key, prd, "bogus").status_code == 422


def test_reply_says_whether_the_notice_changed_anything(client, key, prd, db, pushes):
    """GRPH-1912: the drain's hook runs only on `changed`."""
    changed = [_post(client, key, prd, "stalled", [("GRPH-1", "hold")]).json()["changed"],
               _post(client, key, prd, "stalled", [("GRPH-1", "hold")]).json()["changed"],
               _post(client, key, prd, "stalled", [("GRPH-1", "other")]).json()["changed"],
               _post(client, key, prd, "working").json()["changed"],
               _post(client, key, prd, "working").json()["changed"]]
    assert changed == [True, False, True, True, False]
