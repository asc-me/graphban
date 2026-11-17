"""Team-triage digests published from the box (GRPH-1678)."""
import json
from datetime import timedelta

import pytest

from app.models import Item, utcnow
from app.services import code_sync, digest_publish

FORBIDDEN = ("description", "body", "detail", "comment", "memory")
SECRET = "SECRET-BODY-TEXT"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def calls(monkeypatch):
    digest_publish.reset()
    monkeypatch.setattr(code_sync, "link_status", lambda _db: {"linked": True})
    sent = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

    def post(url, json=None, **kw):
        sent.append(("POST", url, json))
        return Resp()

    def request(method, url, **kw):
        sent.append((method, url, kw.get("params")))
        return Resp()

    monkeypatch.setattr(digest_publish.httpx, "post", post)
    monkeypatch.setattr(digest_publish.httpx, "request", request)
    return sent


def _item(client, auth, title, status="backlog", **kw):
    r = client.post("/api/items", headers=auth, json={
        "title": title, "project_id": "core", "description": SECRET, **kw})
    assert r.status_code in (200, 201), r.text
    key = r.json()["id"]
    if status != "backlog":
        from app.db import SessionLocal

        with SessionLocal() as s:  # the API refuses a bare move to done
            s.query(Item).filter(Item.title == title).one().status = status
            s.commit()
    return key


def _publish(db):
    return digest_publish.publish_digests(db, cloud_url="http://c", api_key="k")



def _digest(calls):
    for method, url, body in reversed(calls):
        if method == "POST" and url.endswith("/api/sync/item-digests") and body["items"]:
            return body["items"]
    return []


def test_digest_lists_open_work_without_any_body_text(client, auth, db, calls):
    a = _item(client, auth, "Edit router", touchpoints=["app/routers/x.py"])
    b = _item(client, auth, "Edit router too", touchpoints=["app/routers/x.py"])
    d = _item(client, auth, "Finished", status="done")
    r = client.post("/api/requests", headers=auth, json={
        "type": "bug", "title": "Crash on save", "detail": SECRET, "project_id": "core"})
    assert r.status_code == 201
    _publish(db)
    rows = _digest(calls)
    ids = {x["id"] for x in rows}
    assert {a, b, r.json()["id"]} <= ids
    assert d in ids  # done within 14 days is kept
    row_a = next(x for x in rows if x["id"] == a)
    assert set(row_a) == {"id", "kind", "title", "status", "item_kind", "touchpoints",
                          "created_at", "hints"}
    assert row_a["hints"][0]["id"] == b
    assert row_a["hints"][0]["evidence"]["shared_touchpoints"] == ["app/routers/x.py"]
    blob = json.dumps(rows)
    assert SECRET not in blob
    for word in FORBIDDEN:
        assert all(word not in x for x in rows)


def test_old_done_items_are_left_out(client, auth, db, calls):
    old = _item(client, auth, "Ancient", status="done")
    live = _item(client, auth, "Live")
    db.expire_all()
    row = db.query(Item).filter(Item.title == "Ancient").one()
    row.done_at = utcnow() - timedelta(days=15)
    db.commit()
    _publish(db)
    ids = {x["id"] for x in _digest(calls)}
    assert live in ids and old not in ids


def test_second_sync_replaces_the_first(client, auth, db, calls):
    k = _item(client, auth, "Soon closed")
    _publish(db)
    assert k in {x["id"] for x in _digest(calls)}
    row = db.query(Item).filter(Item.title == "Soon closed").one()
    row.status = "done"
    row.done_at = utcnow() - timedelta(days=20)
    db.commit()
    calls.clear()
    _publish(db)
    assert k not in {x["id"] for x in _digest(calls)}


def test_flag_off_purges_and_on_republishes(client, auth, db, calls):
    _item(client, auth, "Thing")
    client.put("/api/projects/core/publish-to-org", headers=auth, json={"item_digests": False})
    res = _publish(db)
    assert "Core" in " ".join(res["purged"]) or res["purged"]
    assert any(c[0] == "DELETE" for c in calls)
    assert not any(c[0] == "POST" and c[2]["items"] for c in calls)
    calls.clear()
    _publish(db)  # already purged: no repeat DELETE
    assert not any(c[0] == "DELETE" for c in calls)
    client.put("/api/projects/core/publish-to-org", headers=auth, json={"item_digests": True})
    calls.clear()
    _publish(db)
    assert _digest(calls)
