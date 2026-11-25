"""The console team-triage WIRE, as graphban-control serves it (GBC-173; box side GRPH-1927).

`server/src/triage-routes.ts` is the other end of both calls, so the fake here refuses what the
console refuses rather than what this box happens to send:

* `GET /api/sync/triage-decisions` serves `{record, project_id, decisions: [...]}`, each decision
  `{id, target, decision, note, decided_at}` with `target` an OBJECT — `{kind: "item", item_id}`
  or `{kind: "request", request_id, title, body}`. No `status`, no `author`.
* `POST /api/sync/triage-decisions/applied` takes ONE `{decision_id, outcome, reason}` and
  answers 422 on a missing or non-uuid `decision_id`, an outcome outside `applied`/`refused`, or
  a refusal with no reason. The batch `{project, results: [...]}` the box used to send has no
  `decision_id`, so it is refused here exactly as it is in production.

A fixture written to the box's own shape is how this drifted once already: the tests were green
and no console decision had ever applied.
"""
import re

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
    r = client.post("/api/items", headers=auth,
                    json={"title": title, "project_id": "core", "status": status})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]  # `ItemOut.id` is the item's key — what the digest publishes


def _did(n):
    """A decision id in the console's shape: it validates UUID_RE on the way back."""
    return f"00000000-0000-4000-8000-{n:012d}"


def _at(key):
    return {"kind": "item", "item_id": key}


def _decision(id, target, decision, note=""):
    """Exactly the fields the console's GET serves — nothing the box would like added."""
    return {"id": id, "target": target, "decision": decision, "note": note,
            "decided_at": "2026-11-24T09:00:00.000Z"}


class _Resp:
    def __init__(self, code=200, body=None):
        self.status_code, self._body = code, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(str(self.status_code), request=None, response=None)


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                      re.IGNORECASE)


class _Console:
    """The console's two box-facing triage routes, with the console's own refusals."""

    def __init__(self, decisions_by_project, *, post=None, missing=()):
        self.decisions = decisions_by_project
        self.post_override = post
        self.missing = missing
        self.gets, self.posts, self.refused = [], [], []

    def install(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "get", self.get)
        monkeypatch.setattr(httpx, "post", self.post)
        return self

    def get(self, url, params=None, **k):
        name = (params or {})["project"]
        self.gets.append((url, name))
        if name in self.missing:
            return _Resp(404)
        return _Resp(200, {"record": "triage_decisions", "project_id": name,
                           "decisions": self.decisions.get(name, [])})

    def post(self, url, params=None, json=None, **k):
        if self.post_override is not None:
            return self.post_override(url, params=params, json=json, **k)
        body = json if isinstance(json, dict) else {}
        detail = self._invalid(body)
        if detail:
            self.refused.append((url, body, detail))
            return _Resp(422, {"detail": detail})
        self.posts.append((url, body))
        return _Resp(200, {"record": "triage_decision", "id": body["decision_id"],
                           "status": body["outcome"]})

    @staticmethod
    def _invalid(body):
        did = body.get("decision_id")
        if not isinstance(did, str) or not _UUID_RE.match(did):
            return "decision_id is required"
        if body.get("outcome") not in ("applied", "refused"):
            return "outcome must be applied or refused"
        reason = body.get("reason")
        if body["outcome"] == "refused" and not (isinstance(reason, str) and reason.strip()):
            return "a refusal needs a reason"
        return ""


def _project_name(db, pid="core"):
    from app.models import Project
    return db.get(Project, pid).name


# ---- the fake refuses what the console refuses (so the tests below mean something) ----

def test_the_batch_report_the_box_used_to_send_is_refused(db, monkeypatch):
    import httpx
    console = _Console({}).install(monkeypatch)
    resp = httpx.post("https://c/api/sync/triage-decisions/applied",
                      json={"project": "core",
                            "results": [{"id": _did(1), "result": "applied"}]})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "decision_id is required"
    assert console.posts == [] and len(console.refused) == 1


def test_a_refusal_with_no_reason_is_refused(db, monkeypatch):
    import httpx
    console = _Console({}).install(monkeypatch)
    for body in ({"decision_id": _did(1), "outcome": "refused"},
                 {"decision_id": _did(1), "outcome": "refused", "reason": "   "}):
        assert httpx.post("https://c/api/sync/triage-decisions/applied",
                          json=body).status_code == 422
    assert console.posts == [] and len(console.refused) == 2


# ---- one POST per decision, carrying the console's body ----

def test_each_decision_is_reported_by_its_own_post(client, auth, db, monkeypatch):
    ok = _item(client, auth, "Pull ok", "backlog")
    gone = _item(client, auth, "Pull gone", "backlog")
    name = _project_name(db)
    console = _Console({name: [
        _decision(_did(1), _at(ok), "accept"),
        # An item the console still lists but this box no longer has: refused, and the console
        # is told, so it stops showing the decision as pending.
        _decision(_did(2), _at("GRPH-99999"), "defer"),
    ]}).install(monkeypatch)

    out = triage.pull_decisions(db, cloud_url="https://console.test/", api_key="k")
    assert out == {"applied": 1, "refused": 1, "errors": []}
    assert ("https://console.test/api/sync/triage-decisions", name) in console.gets
    assert console.refused == []
    applied = "https://console.test/api/sync/triage-decisions/applied"
    assert console.posts == [
        (applied, {"decision_id": _did(1), "outcome": "applied", "reason": ""}),
        (applied, {"decision_id": _did(2), "outcome": "refused", "reason": "not_found"}),
    ]
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Pull ok").one().status == "next"


def test_defer_parks_the_item_and_accept_queues_it(client, auth, db, monkeypatch):
    """`defer` is the console's name for the box's `park`; `accept` is the queue."""
    deferred = _item(client, auth, "Defer me", "next")
    accepted = _item(client, auth, "Accept me", "backlog")
    name = _project_name(db)
    console = _Console({name: [
        _decision(_did(1), _at(deferred), "defer", note="after the release"),
        _decision(_did(2), _at(accepted), "accept"),
    ]}).install(monkeypatch)

    assert triage.pull_decisions(db, cloud_url="https://c", api_key="k") == {
        "applied": 2, "refused": 0, "errors": []}
    assert console.refused == []
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Defer me").one().status == "backlog"
    assert db.query(Item).filter(Item.title == "Accept me").one().status == "next"


def test_a_decline_carries_the_consoles_note(client, auth, db, monkeypatch):
    """The console's free text is `note`; a decline that lost it would record no reason."""
    req = Request(id="req_9", project_id="core", number=909, type="feature",
                  title="R9", status="new")
    db.add(req); db.commit()
    name = _project_name(db)
    _Console({name: [_decision(_did(1), _at(req.key), "decline", note="not for us")]}).install(
        monkeypatch)

    assert triage.pull_decisions(db, cloud_url="https://c", api_key="k")["refused"] == 0
    db.expire_all()
    assert db.get(Request, "req_9").status == "declined"
    ev = db.scalars(select(Event).where(Event.action == "request_decline")).one()
    assert (ev.meta or {}).get("reason") == "not for us"


# ---- an accepted public request is routed here as a NEW item ----

def test_an_accepted_console_request_lands_as_a_new_item(client, auth, db, monkeypatch):
    name = _project_name(db)
    target = {"kind": "request", "request_id": "11111111-2222-4333-8444-555555555555",
              "title": "Please add a CSV export", "body": "Every month, by hand."}
    console = _Console({name: [_decision(_did(7), target, "accept")]}).install(monkeypatch)

    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out == {"applied": 1, "refused": 0, "errors": []}
    assert console.refused == []
    db.expire_all()
    made = db.query(Item).filter(Item.title == "Please add a CSV export").one()
    assert made.description == "Every month, by hand."
    assert made.project_id == "core" and made.status == "next"
    assert console.posts == [("https://c/api/sync/triage-decisions/applied",
                              {"decision_id": _did(7), "outcome": "applied", "reason": ""})]


def test_a_routed_request_is_not_created_twice_when_the_report_failed(client, auth, db,
                                                                     monkeypatch):
    """The console still holds it pending, so the next sync serves it again."""
    import httpx
    name = _project_name(db)
    target = {"kind": "request", "request_id": "22222222-3333-4333-8444-555555555555",
              "title": "Routed once", "body": ""}

    def down(*a, **k):
        raise httpx.ConnectError("down")

    _Console({name: [_decision(_did(8), target, "accept")]}, post=down).install(monkeypatch)
    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out["applied"] == 1 and out["errors"] == [
        {"project": name, "decision_id": _did(8), "error": "ConnectError: down"}]

    console = _Console({name: [_decision(_did(8), target, "accept")]}).install(monkeypatch)
    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out == {"applied": 1, "refused": 0, "errors": []}
    assert [b["decision_id"] for _, b in console.posts] == [_did(8)]
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Routed once").count() == 1
    assert len(db.scalars(select(Event).where(
        Event.action == triage.APPLIED_ACTION)).all()) == 1


def test_a_routed_request_nothing_was_pulled_for_is_refused_with_a_reason(client, auth, db):
    """No project to create it on: refused with a reason, never an item on a guessed project."""
    out = triage.apply_decision(db, _decision(
        _did(9), {"kind": "request", "request_id": "33333333-4444-4333-8444-555555555555",
                  "title": "Orphan", "body": ""}, "accept"))
    assert out == {"id": _did(9), "result": "refused", "reason": "no_project"}
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Orphan").count() == 0


# ---- the pull's own failure modes, unchanged by the wire fix ----

def test_pull_on_a_console_without_the_feature_is_not_pulled(client, auth, db, monkeypatch):
    from app.models import Project
    names = {p.name for p in db.query(Project)}
    console = _Console({}, missing=names).install(monkeypatch)
    assert triage.pull_decisions(db, cloud_url="https://c", api_key="k")["pulled"] is False
    assert console.posts == []


def test_a_later_404_keeps_the_counts_already_applied(client, auth, db, monkeypatch):
    from app.models import Project
    key = _item(client, auth, "Before404", "backlog")
    ordered = [p.name for p in db.query(Project).order_by(Project.name, Project.id)
               if (p.name or "").strip()]
    name = _project_name(db)
    assert len(ordered) > 1 and ordered.index(name) < len(ordered) - 1, ordered
    _Console({name: [_decision(_did(3), _at(key), "accept")]},
             missing=set(ordered[ordered.index(name) + 1:])).install(monkeypatch)
    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out.get("pulled") is not False and out["applied"] == 1


def test_a_failed_report_is_retried_next_sync_without_reapplying(client, auth, db, monkeypatch):
    import httpx
    key = _item(client, auth, "Retry", "backlog")
    name = _project_name(db)
    dec = _decision(_did(4), _at(key), "accept")

    def down(*a, **k):
        raise httpx.ConnectError("down")

    _Console({name: [dec]}, post=down).install(monkeypatch)
    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out["applied"] == 1 and out["errors"] and out["errors"][0]["project"] == name
    # The console still holds it pending; the next sync delivers it again and the report lands.
    console = _Console({name: [dec]}).install(monkeypatch)
    triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert console.posts == [("https://c/api/sync/triage-decisions/applied",
                              {"decision_id": _did(4), "outcome": "applied", "reason": ""})]
    assert len(db.scalars(select(Event).where(Event.action == triage.APPLIED_ACTION)).all()) == 1
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Retry").one().status == "next"
