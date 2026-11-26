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
        # `params` of every report POST, in order: the report must name its project.
        self.post_params = []
        # `POST /api/sync/assignable-people`, by project: the last set accepted.
        self.people, self.people_refused = {}, []

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
        if url.endswith("/api/sync/assignable-people"):
            return self._people(params, json)
        if self.post_override is not None:
            return self.post_override(url, params=params, json=json, **k)
        self.post_params.append(dict(params or {}))
        body = json if isinstance(json, dict) else {}
        detail = self._invalid(body)
        if detail:
            self.refused.append((url, body, detail))
            return _Resp(422, {"detail": detail})
        self.posts.append((url, body))
        return _Resp(200, {"record": "triage_decision", "id": body["decision_id"],
                           "status": body["outcome"]})

    def _people(self, params, body):
        """The console's `validatePeople`: exactly `{people: [{id, name}]}`, anything else 422."""
        name = (params or {}).get("project")
        detail = ""
        if not isinstance(body, dict) or not isinstance(body.get("people"), list):
            detail = "people must be a list"
        else:
            extra = [k for k in body if k != "people"]
            if extra:
                detail = f"{extra[0]} is not a people field"
            for p in body["people"]:
                if detail:
                    break
                if not isinstance(p, dict):
                    detail = "each person is {id, name}"
                    break
                bad = [k for k in p if k not in ("id", "name")]
                if bad:
                    detail = f"{bad[0]} is not a person field"
                elif not all(isinstance(p.get(k), str) and p[k].strip() for k in ("id", "name")):
                    detail = "each person needs id and name"
        if detail:
            self.people_refused.append((name, body, detail))
            return _Resp(422, {"detail": detail})
        self.people[name] = list(body["people"])
        return _Resp(200, {"record": "assignable_people", "count": len(body["people"])})

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
    # Each report names the project it was pulled for, exactly as the GET did.
    assert console.post_params == [{"project": name}, {"project": name}]
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


# ---- `duplicate` and `assign` (GBC-277 / GRPH-1722) ----

def _dup(id, key, of, note=""):
    return {**_decision(id, _at(key), "duplicate", note), "duplicate_of": {"item_id": of}}


def _assign(id, key, person):
    return {**_decision(id, _at(key), "assign"), "assignee": person}


def _dup_links(db, a):
    from app.models import Link
    db.expire_all()
    return [(l.a, l.b) for l in db.scalars(select(Link).where(
        Link.a == a, Link.reason == "duplicate_of"))]


def test_a_console_duplicate_links_the_item_and_reports_applied(client, auth, db, monkeypatch):
    dup = _item(client, auth, "Dup copy")
    orig = _item(client, auth, "Dup original")
    name = _project_name(db)
    console = _Console({name: [_dup(_did(20), dup, orig)]}).install(monkeypatch)

    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out == {"applied": 1, "refused": 0, "errors": []}
    assert console.refused == []
    assert console.posts == [("https://c/api/sync/triage-decisions/applied",
                              {"decision_id": _did(20), "outcome": "applied", "reason": ""})]
    assert _dup_links(db, dup) == [(dup, orig)]


def test_a_console_duplicate_of_an_item_the_box_does_not_hold_is_refused(client, auth, db,
                                                                        monkeypatch):
    from app.models import Project
    dup = _item(client, auth, "Dup orphan")
    from app.services import items as items_svc
    db.add(Project(id="elsewhere", name="Elsewhere", tag="ELSE"))
    db.commit()
    other_key = items_svc.create_item(db, title="Other project", project_id="elsewhere").key
    name = _project_name(db)
    console = _Console({name: [_dup(_did(21), dup, "GRPH-99999"),
                               _dup(_did(22), dup, other_key)]}).install(monkeypatch)

    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out["applied"] == 0 and out["refused"] == 2
    assert console.refused == []
    assert [b for _, b in console.posts] == [
        {"decision_id": _did(21), "outcome": "refused", "reason": "not_found"},
        {"decision_id": _did(22), "outcome": "refused", "reason": "wrong_project"},
    ]
    assert _dup_links(db, dup) == []


def _writer(db, uid, name, project="core", access="write"):
    from app.models import Membership, User
    db.add(User(id=uid, name=name, handle=uid.replace("_", "-"), email=f"{uid}@example.test",
                password_hash="x"))
    db.add(Membership(user_id=uid, project_id=project, role="member", access=access))
    db.commit()


def test_a_console_assign_sets_the_assignee_and_reports_applied(client, auth, db, monkeypatch):
    key = _item(client, auth, "Assign me")
    _writer(db, "u_ada", "Ada Lovelace")
    name = _project_name(db)
    console = _Console({name: [_assign(_did(30), key, {"id": "u_ada", "name": "Ada Lovelace"})]}
                       ).install(monkeypatch)

    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out == {"applied": 1, "refused": 0, "errors": []}
    assert [b for _, b in console.posts] == [
        {"decision_id": _did(30), "outcome": "applied", "reason": ""}]
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Assign me").one().assignee == "u-ada"


def test_a_console_assign_to_someone_not_assignable_is_refused_and_unchanged(client, auth, db,
                                                                           monkeypatch):
    key = _item(client, auth, "Keep my assignee")
    db.expire_all()
    db.query(Item).filter(Item.title == "Keep my assignee").one().assignee = "before"
    db.commit()
    _writer(db, "u_reader", "Read Only", access="read")
    name = _project_name(db)
    console = _Console({name: [
        _assign(_did(31), key, {"id": "u_nobody", "name": "Nobody"}),
        _assign(_did(32), key, {"id": "u_reader", "name": "Read Only"}),
    ]}).install(monkeypatch)

    out = triage.pull_decisions(db, cloud_url="https://c", api_key="k")
    assert out["applied"] == 0 and out["refused"] == 2
    assert console.refused == []
    assert [b["reason"] for _, b in console.posts] == ["not_assignable", "not_assignable"]
    db.expire_all()
    assert db.query(Item).filter(Item.title == "Keep my assignee").one().assignee == "before"


# ---- the assignable-people push ----

def test_the_people_fake_refuses_any_field_but_id_and_name(db, monkeypatch):
    import httpx
    console = _Console({}).install(monkeypatch)
    url = "https://c/api/sync/assignable-people"
    assert httpx.post(url, params={"project": "p"},
                      json={"people": [{"id": "u", "name": "U", "email": "u@x"}]}
                      ).status_code == 422
    assert httpx.post(url, params={"project": "p"},
                      json={"people": [], "project": "p"}).status_code == 422
    assert console.people == {} and len(console.people_refused) == 2


def test_each_project_pushes_exactly_id_and_name_and_an_empty_one_pushes_none(client, auth, db,
                                                                           monkeypatch):
    from app.models import Project
    _writer(db, "u_grace", "Grace Hopper")
    _writer(db, "u_viewer", "Just Looking", access="read")
    db.add(Project(id="empty", name="Nobody Here", tag="NOBO"))
    db.commit()
    name = _project_name(db)
    console = _Console({}).install(monkeypatch)

    out = triage.push_assignable_people(db, cloud_url="https://c", api_key="k")
    assert out["errors"] == [] and console.people_refused == []
    assert {"id": "u_grace", "name": "Grace Hopper"} in console.people[name]
    assert all(set(p) == {"id", "name"} for p in console.people[name])
    assert "u_viewer" not in {p["id"] for p in console.people[name]}
    assert console.people["Nobody Here"] == []
    assert set(out["pushed"]) == set(console.people)


def test_a_console_without_the_people_route_is_not_pushed(client, auth, db, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(404))
    assert triage.push_assignable_people(db, cloud_url="https://c", api_key="k") is None


def test_the_sync_tail_pushes_people_before_it_pulls_decisions(client, auth, db, monkeypatch):
    from app.services import code_sync
    calls = []
    monkeypatch.setattr(triage, "push_assignable_people",
                        lambda db, **k: calls.append("people") or {"pushed": [], "errors": []})
    monkeypatch.setattr(triage, "pull_decisions",
                        lambda db, **k: calls.append("decisions") or {"applied": 0})
    out = code_sync._sync_tail(db, "https://c", "k")
    assert calls == ["people", "decisions"]
    assert out["assignable_people"] == {"pushed": [], "errors": []}
