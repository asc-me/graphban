"""Console team-triage decisions applied on the box (GRPH-1679)."""
import pytest
from sqlalchemy import select

from app.models import Event, Item, Request
from app.services import triage


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(client, auth, title, status="backlog"):
    r = client.post("/api/items", headers=auth, json={"title": title, "project_id": "core", "status": status})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _d(**kw):
    return {"id": "d1", "author": {"name": "Mo Manager"}, **kw}


def test_accept_item_moves_to_next_and_logs_console_author(client, auth, db):
    key = _item(client, auth, "Thing", "backlog")
    out = triage.apply_decision(db, _d(target=key, decision="accept", status="backlog"))
    assert out == {"id": "d1", "result": "applied"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Thing").one().status == "next"
    ev = db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()
    assert len(ev) == 1 and ev[0].actor_label == "Mo Manager"


def test_park_item_to_backlog(client, auth, db):
    key = _item(client, auth, "Parkme", "next")
    assert triage.apply_decision(db, _d(target=key, decision="park", status="next"))["result"] == "applied"
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Parkme").one().status == "backlog"


def test_moved_item_refused_and_unchanged(client, auth, db):
    key = _item(client, auth, "Moved", "next")
    out = triage.apply_decision(db, _d(target=key, decision="park", status="backlog"))
    assert out == {"id": "d1", "result": "refused", "reason": "item_moved"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Moved").one().status == "next"
    assert not db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()


def test_applying_twice_changes_nothing_second_time(client, auth, db):
    key = _item(client, auth, "Twice", "backlog")
    dec = _d(target=key, decision="accept", status="backlog")
    assert triage.apply_decision(db, dec)["result"] == "applied"
    assert triage.apply_decision(db, dec)["result"] == "applied"
    assert len(db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()) == 1


def test_assign_team_and_duplicate_links(client, auth, db):
    a = _item(client, auth, "A one")
    b = _item(client, auth, "B two")
    assert triage.apply_decision(db, _d(id="t", target=a, decision="assign_team", team="core", status="backlog"))["result"] == "applied"
    db.expire_all()
    assert "team:core" in db.query(Item).filter(Item.title == "A one").one().tags
    assert triage.apply_decision(db, _d(id="u", target=a, decision="duplicate_of", duplicate_of={"id": b},
                                        status="backlog"))["result"] == "applied"
    assert triage.apply_decision(db, _d(id="v", target=b, decision="duplicate_of", status="backlog",
                                        duplicate_of={"deployment": "other-box", "id": "X-9"}))["result"] == "applied"
    from app.models import Link
    assert {(l.a, l.b) for l in db.query(Link).filter(Link.reason.like("duplicate_of%"))} >= {(a, b)}
    assert any(l.b == "other-box:X-9" for l in db.query(Link))


def test_request_accept_and_decline(client, auth, db):
    from app.services import requests as req_svc

    r1 = Request(id="req_1", project_id="core", number=901, type="feature", title="R1", status="new")
    r2 = Request(id="req_2", project_id="core", number=902, type="bug", title="R2", status="new")
    db.add_all([r1, r2]); db.commit()
    assert triage.apply_decision(db, _d(id="a", target=r1.key, decision="accept", status="new"))["result"] == "applied"
    assert triage.apply_decision(db, _d(id="b", target=r2.key, decision="decline", status="new", reason="no"))["result"] == "applied"
    db.expire_all()
    assert db.get(Request, "req_1").status == "linked"
    assert db.get(Request, "req_1").linked_to
    assert db.get(Request, "req_2").status == "declined"
    assert db.query(Item).filter(Item.id == db.get(Request, "req_1").linked_to).one().status == "next"


def test_a_decision_without_status_is_refused_not_applied_blind(client, auth, db):
    key = _item(client, auth, "Nostatus", "backlog")
    out = triage.apply_decision(db, _d(target=key, decision="accept"))
    assert out == {"id": "d1", "result": "refused", "reason": "no_status"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Nostatus").one().status == "backlog"


def test_a_target_outside_the_pulled_project_is_refused(client, auth, db):
    key = _item(client, auth, "Elsewhere", "backlog")
    out = triage.apply_decision(db, _d(target=key, decision="accept", status="backlog"), "some-other-project")
    assert out == {"id": "d1", "result": "refused", "reason": "wrong_project"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Elsewhere").one().status == "backlog"
    assert triage.apply_decision(db, _d(target=key, decision="accept", status="backlog"), "core")["result"] == "applied"


# ---- the sync half: GET per project, POST /applied ----

class _Resp:
    def __init__(self, code=200, body=None):
        self.status_code, self._body = code, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("bad", request=None, response=None)


def _project_name(db, pid="core"):
    from app.models import Project
    return db.get(Project, pid).name


def _console(monkeypatch, decisions_by_project, *, post=None, missing=()):
    import httpx
    gets, posts = [], []

    def get(url, params=None, **k):
        gets.append((url, params["project"]))
        if params["project"] in missing:
            return _Resp(404)
        return _Resp(200, {"decisions": decisions_by_project.get(params["project"], [])})

    def default_post(url, json=None, **k):
        posts.append((url, json))
        return _Resp(200)

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post or default_post)
    return gets, posts


def test_pull_applies_and_reports_applied_and_refused(client, auth, db, monkeypatch):
    ok = _item(client, auth, "Pull ok", "backlog")
    moved = _item(client, auth, "Pull moved", "next")
    name = _project_name(db)
    gets, posts = _console(monkeypatch, {name: [
        _d(id="p1", target=ok, decision="accept", status="backlog"),
        _d(id="p2", target=moved, decision="park", status="backlog"),
    ]})
    out = triage.pull_decisions(db, cloud_url="https://console.test/", api_key="k")
    assert out["applied"] == 1 and out["refused"] == 1 and out["errors"] == []
    assert ("https://console.test/api/sync/triage-decisions", name) in gets
    assert len(posts) == 1
    url, body = posts[0]
    assert url == "https://console.test/api/sync/triage-decisions/applied"
    assert body["project"] == name
    assert {r["id"]: (r["result"], r.get("reason")) for r in body["results"]} == {
        "p1": ("applied", None), "p2": ("refused", "item_moved")}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Pull ok").one().status == "next"
    assert db.query(Item).filter(Item.title == "Pull moved").one().status == "next"


def test_pull_on_a_console_without_the_feature_is_not_pulled(client, auth, db, monkeypatch):
    from app.models import Project
    names = {p.name for p in db.query(Project)}
    _, posts = _console(monkeypatch, {}, missing=names)
    assert triage.pull_decisions(db, cloud_url="https://c", api_key="k")["pulled"] is False
    assert posts == []


def test_a_later_404_keeps_the_counts_already_applied(client, auth, db, monkeypatch):
    from app.models import Project
    key = _item(client, auth, "Before404", "backlog")
    ordered = [p.name for p in db.query(Project).order_by(Project.name, Project.id) if (p.name or "").strip()]
    name = _project_name(db)
    assert len(ordered) > 1 and ordered.index(name) < len(ordered) - 1, ordered
    _console(monkeypatch, {name: [_d(id="q1", target=key, decision="accept", status="backlog")]},
             missing=set(ordered[ordered.index(name) + 1:]))
    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out.get("pulled") is not False and out["applied"] == 1


def test_a_failed_report_is_retried_next_sync_without_reapplying(client, auth, db, monkeypatch):
    import httpx
    key = _item(client, auth, "Retry", "backlog")
    name = _project_name(db)
    dec = _d(id="r1", target=key, decision="accept", status="backlog")

    def down(*a, **k):
        raise httpx.ConnectError("down")

    _console(monkeypatch, {name: [dec]}, post=down)
    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out["applied"] == 1 and out["errors"] and out["errors"][0]["project"] == name
    # The console still holds it pending; the next sync delivers it again and the report lands.
    _, posts = _console(monkeypatch, {name: [dec]})
    triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert posts and posts[0][1]["results"] == [{"id": "r1", "result": "applied"}]
    assert len(db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()) == 1
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Retry").one().status == "next"
