"""Unified triage feed (GRPH-1135 / PRD-60 D5)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Item, Request


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _feed(client, auth, pid="core"):
    r = client.get(f"/api/projects/{pid}/triage", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _req(client, auth, type_="bug", title="Crash on save"):
    r = client.post("/api/requests", headers=auth,
                    json={"type": type_, "title": title, "project_id": "core"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _item(client, auth, title, status="backlog", **kw):
    r = client.post("/api/items", headers=auth, json={"title": title, "project_id": "core", **kw})
    assert r.status_code in (200, 201), r.text
    key = r.json()["id"]
    if status != "backlog":
        client.patch(f"/api/items/{key}", headers=auth, json={"status": status})
    return key


def test_all_kinds_blockers_first_with_counts(client, auth, db):
    _req(client, auth, "bug", "Crash on save")
    _req(client, auth, "feature", "Dark mode")
    a = _item(client, auth, "Edit router", touchpoints=["app/routers/x.py"])
    b = _item(client, auth, "Edit router too", touchpoints=["app/routers/x.py"])
    blocked = _item(client, auth, "Needs a call")
    client.patch(f"/api/items/{blocked}", headers=auth,
                 json={"status": "blocked", "blocker": "waiting on legal"})
    f = _feed(client, auth)
    kinds = [r["kind"] for r in f["rows"]]
    assert kinds[0] == "blocker"
    assert {"bug", "feature", "collision"} <= set(kinds)
    assert f["failed_sources"] == []
    c = f["counts"]
    for tab, kinds_ in (("blockers", {"blocker"}), ("bugs", {"bug"}),
                        ("requests", {"feature", "enhancement"}), ("collisions", {"collision"})):
        assert c[tab] == sum(r["kind"] in kinds_ for r in f["rows"]) >= 1
    assert c["all"] == len(f["rows"])
    blocker = next(r for r in f["rows"] if r["id"] == blocked)
    assert blocker["reason"] == "waiting on legal"
    assert all(r["kind"] == "blocker" for r in f["rows"][:c["blockers"]])
    col = next(r for r in f["rows"] if r["kind"] == "collision"
               and {a, b} <= {m["id"] for m in r["members"]})
    assert col["risk"] == "serialize"


def test_failing_source_is_named_not_empty(client, auth, monkeypatch):
    before = _feed(client, auth)
    _req(client, auth)
    from app.services import collision

    def boom(*a, **k):
        raise RuntimeError("clustering down")

    monkeypatch.setattr(collision, "triage", boom)
    f = _feed(client, auth)
    assert f["failed_sources"] == ["collisions"]
    assert not any(r["kind"] == "collision" for r in f["rows"])
    assert len(f["rows"]) > 0 and f["counts"]["bugs"] == before["counts"]["bugs"] + 1


def test_snoozed_request_returns_after_its_date(client, auth, db):
    rid = _req(client, auth)
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert client.post(f"/api/requests/{rid}/snooze", headers=auth, json={"until": until}).status_code == 200
    assert rid not in {r["id"] for r in _feed(client, auth)["rows"]}
    req = db.get(Request, rid)
    req.snoozed_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert rid in {r["id"] for r in _feed(client, auth)["rows"]}


def test_collision_id_stable_until_member_set_changes(client, auth):
    _item(client, auth, "One", touchpoints=["app/a.py"])
    _item(client, auth, "Two", touchpoints=["app/a.py"])
    def ids():
        return {r["id"] for r in _feed(client, auth)["rows"] if r["kind"] == "collision"}
    first = ids()
    assert first and ids() == first
    _item(client, auth, "Three", touchpoints=["app/a.py"])
    assert ids() != first


def test_other_projects_key_is_refused(client, auth):
    r = client.get("/api/projects/does-not-exist/triage", headers=auth)
    assert r.status_code == 404
