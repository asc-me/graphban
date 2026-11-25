"""The box publishes its specs (PRDs) to the console (GRPH-1676)."""
import pytest
from sqlalchemy import select

from app.models import Prd, SpecPublish
from app.services import code_sync, spec_publish
from app.services import prds as prd_svc

URL, KEY = "https://console.example", "k"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture()
def console(monkeypatch):
    """Record every spec call; linked box. `console.status` sets the next answers."""
    calls: list[tuple[str, dict, dict]] = []
    state = {"status": 200, "config": {"features": ["specs"]}}

    def get(url, timeout=None):
        assert url == f"{URL}/api/config"
        return _Resp(200, state["config"])

    def post(url, json=None, headers=None, timeout=None):
        assert url == f"{URL}/api/sync/specs"
        calls.append(("POST", json, headers))
        return _Resp(state["status"])

    def request(method, url, json=None, headers=None, timeout=None):
        assert method == "DELETE" and url == f"{URL}/api/sync/specs"
        calls.append(("DELETE", json, headers))
        return _Resp(state["status"])

    monkeypatch.setattr(spec_publish.httpx, "get", get)
    monkeypatch.setattr(spec_publish.httpx, "post", post)
    monkeypatch.setattr(spec_publish.httpx, "request", request)
    monkeypatch.setattr(code_sync, "link_status", lambda _db: {"linked": True})

    class C:
        def mine(self, name="Specs One"):
            return [(op, body, hdr) for op, body, hdr in calls if body["project"] == name]

        def clear(self):
            calls.clear()

    c = C()
    c.status = state
    return c


def _setup(client, auth, name="Specs One"):
    p = client.post("/api/projects", headers=auth, json={"name": name}).json()
    prd = client.post("/api/prds", headers=auth,
                      json={"title": "Spec A", "project_id": p["id"], "template": "standard"}).json()
    return p, prd


def _sync(db):
    db.expire_all()
    return spec_publish.publish_specs(db, cloud_url=URL, api_key=KEY)


def _flag(client, auth, pid, on):
    r = client.put(f"/api/projects/{pid}/publish-to-org", headers=auth, json={"specs": on})
    assert r.status_code == 200


def test_flag_on_publishes_then_an_unchanged_prd_is_not_resent(client, auth, db, console):
    p, prd = _setup(client, auth)
    item = client.post("/api/items", headers=auth,
                       json={"title": "t", "project_id": p["id"], "prd_id": prd["id"]}).json()
    _sync(db)
    sent = console.mine()
    assert [op for op, _, _ in sent] == ["POST"]
    _, body, headers = sent[0]
    row = db.get(Prd, prd["id"])
    assert body["prd_id"] == prd["id"] and body["title"] == "Spec A"
    assert body["status"] == row.status and body["body_markdown"] == row.body
    assert body["base_hash"] == prd_svc.body_hash(row.body)
    assert body["grill"] == {"complete": False, "outstanding": len(prd_svc.DIMENSIONS)}
    assert body["items"] == [item["id"]]
    assert headers["Idempotency-Key"] == spec_publish.idempotency_key(prd["id"], body["base_hash"])
    assert headers["X-API-Key"] == KEY

    console.clear()
    _sync(db)
    assert console.mine() == []


def test_an_edit_publishes_exactly_one_new_version(client, auth, db, console):
    _, prd = _setup(client, auth)
    _sync(db)
    old = console.mine()[0][1]["base_hash"]
    console.clear()
    row = db.get(Prd, prd["id"])
    row.body = (row.body or "") + "\n\n## Added\nmore intent\n"
    db.commit()
    _sync(db)
    sent = console.mine()
    assert len(sent) == 1 and sent[0][0] == "POST"
    assert sent[0][1]["base_hash"] == prd_svc.body_hash(row.body) != old
    assert sent[0][2]["Idempotency-Key"] != spec_publish.idempotency_key(prd["id"], old)
    console.clear()
    _sync(db)
    assert console.mine() == []


def test_a_project_with_the_flag_off_publishes_nothing(client, auth, db, console):
    p, _ = _setup(client, auth)
    _flag(client, auth, p["id"], False)
    _sync(db)
    assert console.mine() == []


def test_flag_off_purges_and_flag_on_republishes(client, auth, db, console):
    p, prd = _setup(client, auth)
    _sync(db)
    console.clear()
    _flag(client, auth, p["id"], False)
    _sync(db)
    assert [(op, b["prd_id"]) for op, b, _ in console.mine()] == [("DELETE", prd["id"])]
    assert db.get(SpecPublish, prd["id"]) is None
    console.clear()
    _sync(db)
    assert console.mine() == []  # purged once, not on every sync
    _flag(client, auth, p["id"], True)
    _sync(db)
    assert [(op, b["prd_id"]) for op, b, _ in console.mine()] == [("POST", prd["id"])]


def test_deleting_a_prd_sends_the_purge(client, auth, db, console):
    _, prd = _setup(client, auth)
    _sync(db)
    console.clear()
    db.delete(db.get(Prd, prd["id"]))
    db.commit()
    _sync(db)
    assert [(op, b["prd_id"]) for op, b, _ in console.mine()] == [("DELETE", prd["id"])]


def test_a_refused_publish_is_retried_on_the_next_sync(client, auth, db, console):
    _, prd = _setup(client, auth)
    console.status["status"] = 503
    out = _sync(db)
    assert any(f["prd_id"] == prd["id"] for f in out["failed"])
    assert db.get(SpecPublish, prd["id"]) is None
    console.status["status"] = 200
    console.clear()
    _sync(db)
    assert [op for op, _, _ in console.mine()] == ["POST"]


def test_an_unlinked_box_neither_publishes_nor_purges(client, auth, db, console, monkeypatch):
    _setup(client, auth)
    _sync(db)
    console.clear()
    monkeypatch.setattr(code_sync, "link_status", lambda _db: {"linked": False})
    out = _sync(db)
    assert out["reason"] == "not linked" and console.mine() == []
    assert db.scalars(select(SpecPublish)).first() is not None


def test_the_sync_tail_carries_the_spec_publish(client, auth, db, console, monkeypatch):
    _, prd = _setup(client, auth)
    monkeypatch.setattr(code_sync, "_pull_rules_on_sync", lambda *a: {})
    monkeypatch.setattr(code_sync, "_push_box_on_sync", lambda *a: {})
    out = code_sync._sync_tail(db, URL, KEY)
    assert prd["id"] in out["specs"]["published"]


def test_a_console_that_does_not_advertise_specs_gets_nothing(client, auth, db, console,
                                                             monkeypatch):
    """An older console keeps working unchanged: no POST, no DELETE, no specs key."""
    _setup(client, auth)
    console.status["config"] = {"features": ["item_digests"]}
    assert _sync(db) is None
    assert console.mine() == [] and db.scalars(select(SpecPublish)).first() is None
    monkeypatch.setattr(code_sync, "_pull_rules_on_sync", lambda *a: {})
    monkeypatch.setattr(code_sync, "_push_box_on_sync", lambda *a: {})
    monkeypatch.setattr(code_sync, "_publish_digests_on_sync", lambda *a: None)
    monkeypatch.setattr(code_sync, "_pull_triage_on_sync", lambda *a: None)
    out = code_sync._sync_tail(db, URL, KEY)
    assert out["rules"] == {} and out["box"] == {}
    assert "specs" not in out and "digests" not in out


def test_a_probe_failure_reads_as_not_served(client, auth, db, console, monkeypatch):
    _setup(client, auth)

    def boom(url, timeout=None):
        raise spec_publish.httpx.ConnectError("down")

    monkeypatch.setattr(spec_publish.httpx, "get", boom)
    assert _sync(db) is None and console.mine() == []


def test_a_404_from_the_ingest_is_not_served_not_a_failure(client, auth, db, console):
    """Advertised, then rolled back: the 404 is not retried as a failure every sync."""
    _, prd = _setup(client, auth)
    console.status["status"] = 404
    assert _sync(db) is None
    assert db.get(SpecPublish, prd["id"]) is None
    console.status["status"] = 200
    out = _sync(db)
    assert prd["id"] in out["published"] and "failed" not in out


def test_a_404_on_purge_keeps_the_row_for_a_console_that_serves_specs(client, auth, db,
                                                                     console):
    _, prd = _setup(client, auth)
    _sync(db)
    db.delete(db.get(Prd, prd["id"]))
    db.commit()
    console.status["status"] = 404
    assert _sync(db) is None
    assert db.get(SpecPublish, prd["id"]) is not None  # purged once the console serves it


def test_the_version_label_rides_with_the_spec(client, auth, db, console):
    _, prd = _setup(client, auth)
    row = db.get(Prd, prd["id"])
    row.version = "v1.0"
    db.commit()
    _sync(db)
    assert console.mine()[0][1]["version"] == "v1.0"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_version_sends_no_version_key(client, auth, db, console, blank):
    _, prd = _setup(client, auth)
    row = db.get(Prd, prd["id"])
    row.version = blank
    db.commit()
    _sync(db)
    assert "version" not in console.mine()[0][1]


def test_a_version_change_alone_does_not_resend(client, auth, db, console):
    _, prd = _setup(client, auth)
    _sync(db)
    sent = console.mine()[0]
    assert sent[2]["Idempotency-Key"] == spec_publish.idempotency_key(
        prd["id"], sent[1]["base_hash"])
    console.clear()
    row = db.get(Prd, prd["id"])
    row.version = "v9.9"
    db.commit()
    _sync(db)
    assert console.mine() == []
