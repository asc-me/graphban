"""Team-triage digests published from the box (GRPH-1678)."""
import json
from unittest import mock
import pytest

from app.models import Item
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


class _Calls(list):
    console: dict


@pytest.fixture()
def calls(monkeypatch):
    digest_publish.reset()
    monkeypatch.setattr(code_sync, "link_status", lambda _db: {"linked": True})
    sent = _Calls()
    console = {"features": ["item_digests"], "status": 200}

    class Resp:
        def __init__(self, status=200, body=None):
            self.status_code = status
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    def get(url, **kw):
        sent.append(("GET", url, None))
        return Resp(200, {"features": console["features"]})

    def post(url, json=None, **kw):
        sent.append(("POST", url, json))
        return Resp(console["status"])

    def request(method, url, **kw):
        sent.append((method, url, kw.get("params")))
        return Resp(console["status"])

    monkeypatch.setattr(digest_publish.httpx, "get", get)
    monkeypatch.setattr(digest_publish.httpx, "post", post)
    monkeypatch.setattr(digest_publish.httpx, "request", request)
    sent.console = console
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
    assert d not in ids  # every non-done item and open request, and nothing else
    row_a = next(x for x in rows if x["id"] == a)
    assert set(row_a) == {"id", "kind", "title", "status", "item_kind", "touchpoints",
                          "created_at", "hints"}
    assert row_a["hints"][0]["id"] == b
    assert row_a["hints"][0]["evidence"]["shared_touchpoints"] == ["app/routers/x.py"]
    blob = json.dumps(rows)
    assert SECRET not in blob
    for word in FORBIDDEN:
        assert all(word not in x for x in rows)


def test_second_sync_replaces_the_first(client, auth, db, calls):
    k = _item(client, auth, "Soon closed")
    other = _item(client, auth, "Still open")
    _publish(db)
    assert k in {x["id"] for x in _digest(calls)}
    row = db.query(Item).filter(Item.title == "Soon closed").one()
    row.status = "done"  # closed just now: no backdating
    db.commit()
    calls.clear()
    _publish(db)
    ids = {x["id"] for x in _digest(calls)}
    assert k not in ids and other in ids


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


def test_older_console_is_sent_nothing_and_sync_result_is_unchanged(client, auth, db, calls):
    _item(client, auth, "Thing")
    calls.console["features"] = []  # an older console: no `item_digests` advertised
    assert _publish(db) is None
    assert [c[0] for c in calls] == ["GET"]  # the probe only: no POST, no DELETE
    with mock.patch.object(code_sync, "_pull_rules_on_sync", return_value={}), \
            mock.patch.object(code_sync, "_push_box_on_sync", return_value={}):
        assert "digests" not in code_sync._sync_tail(db, "http://c", "k")


def test_a_404_from_the_console_is_not_an_error(client, auth, db, calls):
    _item(client, auth, "Thing")
    calls.console["status"] = 404  # advertised, then not served
    assert _publish(db) is None


def test_each_text_is_embedded_once_per_sync(client, auth, db, calls, monkeypatch):
    from app import providers

    real = providers.get_embedder()
    n = {"texts": 0}

    class Counting:
        dim = getattr(real, "dim", 0)

        def embed(self, text):
            n["texts"] += 1
            return real.embed(text)

    monkeypatch.setattr(providers, "get_embedder", lambda: Counting())
    for t in ("One", "Two", "Three"):
        _item(client, auth, t)
    for t in ("Crash on save", "Crash on load", "Slow page"):
        assert client.post("/api/requests", headers=auth, json={
            "type": "bug", "title": t, "project_id": "core"}).status_code == 201
    n["texts"] = 0
    _publish(db)
    reqs = [x for x in _digest(calls) if x["kind"] == "request"]
    items = [x for x in _digest(calls) if x["kind"] == "item"]
    assert len(reqs) >= 3
    assert n["texts"] == len(reqs) + len(items)  # R + I, not R x (R + I)
