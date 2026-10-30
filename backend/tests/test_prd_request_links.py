"""PRD <-> request links and similarity suggestions (GRPH-1140 / PRD-60 D10)."""
import pytest

from app.models import Prd
from app.services import prds as prd_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _req(client, auth, title, type_="feature", project="core"):
    r = client.post("/api/requests", headers=auth, json={"type": type_, "title": title, "project_id": project})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _prd(client, auth, body="# T\n\n## Dark mode\nDark mode theme toggle for the app.\n"):
    r = client.post("/api/prds", headers=auth, json={"title": "Theming", "project_id": "core", "body": body})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _summary(client, auth, pid):
    return next(p for p in client.get("/api/prds?project_id=core", headers=auth).json() if p["id"] == pid)


def test_link_unlink_updates_summary(client, auth):
    pid = _prd(client, auth)
    a, b = _req(client, auth, "Dark mode"), _req(client, auth, "Crash", "bug")
    client.post(f"/api/requests/{a}/vote", headers=auth, json={"delta": 1})
    for r in (a, b):
        assert client.post(f"/api/prds/{pid}/requests", headers=auth, json={"request_id": r}).status_code == 200
    s = _summary(client, auth, pid)
    assert s["request_count"] == 2 and s["request_types"] == {"feature": 1, "bug": 1}
    assert s["request_votes"] >= 1
    assert len(client.get(f"/api/prds/{pid}/requests", headers=auth).json()) == 2
    assert client.delete(f"/api/prds/{pid}/requests/{a}", headers=auth).status_code == 200
    s = _summary(client, auth, pid)
    assert s["request_count"] == 1 and s["request_types"] == {"bug": 1}


def test_closed_prd_refuses_link(client, auth, db):
    pid = _prd(client, auth)
    rid = _req(client, auth, "Dark mode")
    prd = prd_svc.get_prd(db, pid)
    prd.close_record = {"closed": True}
    db.commit()
    assert client.post(f"/api/prds/{pid}/requests", headers=auth, json={"request_id": rid}).status_code == 409


def test_other_projects_request_refused(client, auth, db):
    from app.models import Project

    db.add(Project(id="other", name="Other", tag="OTH"))
    db.commit()
    from app.models import Request
    from app.services import keys

    rid, number = keys.mint(db, "other", "request")
    db.add(Request(id=rid, number=number, project_id="other", type="feature", title="Dark mode"))
    db.commit()
    pid = _prd(client, auth)
    r = client.post(f"/api/prds/{pid}/requests", headers=auth, json={"request_id": rid})
    assert r.status_code == 404
    assert _summary(client, auth, pid)["request_count"] == 0


def test_suggestions_exclude_linked_and_respect_threshold(client, auth):
    pid = _prd(client, auth)
    a = _req(client, auth, "Dark mode theme toggle")
    _req(client, auth, "Dark mode theme toggle for the app")
    client.post(f"/api/prds/{pid}/requests", headers=auth, json={"request_id": a})
    body = client.get(f"/api/prds/{pid}/request-suggestions", headers=auth).json()
    assert body["available"] is True
    ids = [s["request_id"] for s in body["suggestions"]]
    assert a not in ids and ids
    assert all(s["score"] >= prd_svc.SUGGESTION_THRESHOLD for s in body["suggestions"])


def test_embedder_failure_is_unavailable_not_empty(client, auth, monkeypatch):
    # Sabotage: a dead embedder must not look like "no matches".
    pid = _prd(client, auth)
    _req(client, auth, "Dark mode theme toggle")

    class Dead:
        def embed(self, text):
            raise RuntimeError("down")

    monkeypatch.setattr("app.providers.get_embedder", lambda: Dead())
    body = client.get(f"/api/prds/{pid}/request-suggestions", headers=auth).json()
    assert body["available"] is False


def test_best_prd_for_request(client, auth, db):
    from app.models import Request

    pid = _prd(client, auth)
    rid = _req(client, auth, "Dark mode theme toggle for the app")
    hit = prd_svc.best_prd_for_request(db, db.get(Request, rid))
    assert hit and hit["prd_id"] == prd_svc.get_prd(db, pid).key


def test_suggestions_embed_each_section_once(client, auth, db, monkeypatch):
    # Sections are embedded once per PRD, not once per candidate request.
    from app.providers import get_embedder

    pid = _prd(client, auth, body="# T\n\n## Dark mode\nTheme toggle.\n\n## Light mode\nOther theme.\n")
    for i in range(4):
        _req(client, auth, f"Dark mode {i}")
    real, calls = get_embedder(), []

    class Counting:
        def embed(self, text):
            calls.append(text)
            return real.embed(text)

    monkeypatch.setattr("app.providers.get_embedder", lambda: Counting())
    assert client.get(f"/api/prds/{pid}/request-suggestions", headers=auth).json()["available"] is True
    from app.models import Request

    n_requests = db.query(Request).filter(Request.project_id == "core").count()
    assert n_requests >= 4
    assert len(calls) == 2 + n_requests  # two sections once, plus one per candidate request


def test_list_carries_by_created_at_and_linked_item_key(client, auth, db):
    from app.models import Request

    pid = _prd(client, auth)
    a, b = _req(client, auth, "Dark mode"), _req(client, auth, "Crash", "bug")
    item = client.post("/api/items", headers=auth, json={"title": "Do it", "project_id": "core"}).json()["id"]
    ra = db.get(Request, a) or db.query(Request).filter(Request.title == "Dark mode").one()
    ra.by = "dana"
    db.commit()
    assert client.post(f"/api/requests/{a}/link", headers=auth, json={"item_id": item}).status_code == 200
    for r in (a, b):
        assert client.post(f"/api/prds/{pid}/requests", headers=auth, json={"request_id": r}).status_code == 200
    # Retag so the item's frozen id and rendered key DIVERGE; otherwise sending the id instead of
    # the key passes this test (as it did on the first attempt at GRPH-1216).
    from app.models import Item
    from app.services import projects as proj_svc

    proj_svc.retag_project(db, "core", "ZZ")
    db.commit()
    row = db.query(Item).filter(Item.title == "Do it").one()
    db.refresh(row)
    key, frozen = row.key, row.id
    db.commit()  # end the read so the request below is not blocked by this session
    assert key != frozen, f"the fixture did not diverge: id={frozen!r} key={key!r}"
    # Request keys re-render under the new tag too, so match rows by title.
    rows = {r["title"]: r for r in client.get(f"/api/prds/{pid}/requests", headers=auth).json()}
    assert rows["Dark mode"]["by"] == "dana" and rows["Dark mode"]["created_at"]
    assert rows["Dark mode"]["linked_to"] == key != frozen
    assert rows["Crash"]["linked_to"] is None
