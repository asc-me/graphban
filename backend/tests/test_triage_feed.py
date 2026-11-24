"""Unified triage feed (GRPH-1135 / PRD-60 D5)."""
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

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
    assert col["recommendation"] == (
        "Running them together is what produces the conflict this check exists to predict."
    )


def test_collision_recommendation_blocked_when_a_member_is_held(client, auth):
    held = _item(client, auth, "Held member", touchpoints=["app/shared.py"], status="in_progress")
    _item(client, auth, "Waiting behind", touchpoints=["app/shared.py"])
    f = _feed(client, auth)
    col = next(r for r in f["rows"] if r["kind"] == "collision"
               and held in {m["id"] for m in r["members"]})
    assert col["risk"] == "blocked"
    assert col["recommendation"] == (
        "the overlap is already held, so a second claim would be refused rather than queued"
    )


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


def test_decision_wait_rows_carry_question_state_and_only_response_needed_counts(
        client, auth, db):
    from app.services import items as items_svc
    from app.services import links as links_svc

    ticket = items_svc.create_item(db, project_id="core", title="needs an answer",
                                   status="in_progress")
    wait = items_svc.create_item(db, project_id="core", title="which store?", status="blocked",
                                 question="Which store?", options=["a", "b"])
    gate = items_svc.create_item(db, project_id="core", title="merge it", status="blocked",
                                 tags=["wait:merge"])
    links_svc.create_link(db, a=ticket.id, b=wait.id, type_="dependency", project_id="core")
    items_svc.update_item(db, ticket.id, status="blocked")
    f = _feed(client, auth)
    rows = {r["id"]: r for r in f["rows"]}
    assert rows[wait.key]["question_state"] == "checking"
    assert "question_state" not in rows[gate.key]
    assert f["waiting_on_person"] == 0
    wait.decision = {**wait.decision, "needs_person_at": "2026-11-11T10:00:00+00:00"}
    db.commit()
    f = _feed(client, auth)
    rows = {r["id"]: r for r in f["rows"]}
    assert rows[wait.key]["question_state"] == "response_needed"
    assert f["waiting_on_person"] == 1


# --- GRPH-1529: an answered decision shows who answered and whether a drain is live ---------


def _answered_pair(db, title="which store?"):
    from app.services import items as items_svc
    from app.services import links as links_svc
    from app.services import prds as prds_svc

    prd = prds_svc.create_prd(db, project_id="core", title="Store", body="## A\n\nx\n")
    ticket = items_svc.create_item(db, project_id="core", title="needs an answer",
                                   status="in_progress", prd_id=prd.id)
    ticket.claimed_by = "core-A1"
    wait = items_svc.create_item(db, project_id="core", title=title, status="blocked",
                                 question="Which store?", options=["redis", "postgres"])
    links_svc.create_link(db, a=ticket.id, b=wait.id, type_="dependency", project_id="core")
    items_svc.update_item(db, ticket.id, status="blocked")
    db.refresh(ticket)
    return prd, ticket, wait


def test_agent_answer_shows_source_quote_and_restart_command_on_the_row(client, auth, db):
    from app.services import waits as waits_svc

    _, ticket, wait = _answered_pair(db)
    waits_svc.answer_wait(db, wait.id, choice="redis", agent="core-A7",
                          source={"kind": "session", "quote": "the user said redis"})
    f = _feed(client, auth)
    row = next(r for r in f["rows"] if r["id"] == wait.key)
    assert row["kind"] == "answered" and row["answer"] == "redis"
    assert row["answered_by"] == "answered from session by core-A7"
    assert row["by"]["agent"] == "core-A7" and row["source"]["kind"] == "session"
    assert row["quote"] == "the user said redis"
    key = ticket.prd_key
    assert row["drain"] == {"status": "not_live", "prd": key,
                            "message": f"No drain is running for {key}.",
                            "command": f"gban fleet until --project core --prd {key}"}
    # Listed, but not waiting on anyone: it adds to no tab's count.
    assert f["counts"]["all"] == sum(r["kind"] != "answered" for r in f["rows"])
    assert f["waiting_on_person"] == 0


def test_person_answer_shows_the_person_and_a_live_drain(client, auth, db):
    from app.models import Agent, Enrolment, utcnow
    from app.services import waits as waits_svc

    prd, _, wait = _answered_pair(db)
    db.add(Enrolment(id="enr-tri-live", project_id="core", code_hash="x", role="worker",
                     prd_id=prd.id, expires_at=utcnow() + timedelta(hours=1)))
    db.flush()
    db.add(Agent(id="core-A88", project_id="core", number=88, enrolment_id="enr-tri-live",
                 state="working", last_seen_at=utcnow()))
    db.commit()
    r = client.post(f"/api/items/{wait.key}/answer", headers=auth, json={"option": "postgres"})
    assert r.status_code == 200, r.text
    row = next(r for r in _feed(client, auth)["rows"] if r["id"] == wait.key)
    assert row["answered_by"].startswith("answered by ") and row["by"]["agent"] is None
    assert row["quote"] is None and row["source"] is None
    assert row["drain"]["status"] == "live"
    assert row["drain"]["message"].startswith("A drain is running for ")


def test_answered_row_leaves_the_feed_after_the_window(client, auth, db):
    from app.services import triage as triage_svc
    from app.services import waits as waits_svc

    _, _, wait = _answered_pair(db)
    waits_svc.answer_wait(db, wait.id, choice="redis")
    assert any(r["id"] == wait.key for r in _feed(client, auth)["rows"])
    db.refresh(wait)
    wait.done_at = wait.done_at - timedelta(seconds=triage_svc.ANSWERED_WINDOW_SECONDS + 60)
    db.commit()
    assert all(r["id"] != wait.key for r in _feed(client, auth)["rows"])


# --- GRPH-1571: one row per question ---------------------------------------------------------


def _ticket_and_wait(db, extra_gate=None):
    from app.services import items as items_svc
    from app.services import links as links_svc

    ticket = items_svc.create_item(db, project_id="core", title="needs an answer",
                                   status="in_progress")
    wait = items_svc.create_item(db, project_id="core", title="which store?", status="blocked",
                                 question="Which store?", options=["a", "b"])
    links_svc.create_link(db, a=ticket.id, b=wait.id, type_="dependency", project_id="core")
    if extra_gate:
        gate = items_svc.create_item(db, project_id="core", title="merge it", status="blocked",
                                     tags=[extra_gate])
        links_svc.create_link(db, a=ticket.id, b=gate.id, type_="dependency", project_id="core")
    items_svc.update_item(db, ticket.id, status="blocked")
    return ticket, wait


def test_question_only_ticket_row_is_dropped_and_wait_names_it(client, auth, db):
    before = _feed(client, auth)["counts"]["blockers"]
    ticket, wait = _ticket_and_wait(db)
    f = _feed(client, auth)
    ids = [r["id"] for r in f["rows"]]
    assert ticket.key not in ids and wait.key in ids
    row = next(r for r in f["rows"] if r["id"] == wait.key)
    assert row["blocks"]["id"] == ticket.key and row["blocks"]["title"] == ticket.title
    assert f["counts"]["blockers"] == before + 1


def test_ticket_with_a_gate_keeps_its_row_and_names_the_question(client, auth, db):
    ticket, wait = _ticket_and_wait(db, extra_gate="wait:merge")
    rows = {r["id"]: r for r in _feed(client, auth)["rows"]}
    assert wait.key in rows and ticket.key in rows
    assert rows[ticket.key]["also_questions"] == [{"id": wait.key, "question": "Which store?"}]
    assert rows[wait.key]["blocks"]["id"] == ticket.key


def test_answered_row_carries_the_ticket_it_unblocks(client, auth, db):
    from app.services import waits as waits_svc

    ticket, wait = _ticket_and_wait(db)
    waits_svc.answer_wait(db, wait.id, choice="a")
    row = next(r for r in _feed(client, auth)["rows"] if r["id"] == wait.key)
    assert row["kind"] == "answered"
    assert row["blocks"] == {"id": ticket.key, "title": ticket.title}


# --- GRPH-1606: an open question on an unblocked item is in the feed --------------------------

def test_open_question_on_unblocked_item_is_in_feed(client, auth, db):
    from app.services import items as items_svc

    it = items_svc.create_item(db, project_id="core", title="backlog q", status="backlog",
                               tags=["wait:decision"], question="Which store?", options=["a", "b"])
    it.decision = {**it.decision, "needs_person_at": "2026-11-11T10:00:00+00:00"}
    db.commit()
    rows = {r["id"]: r for r in _feed(client, auth)["rows"]}
    assert rows[it.key]["decision"]["question"] == "Which store?"
    assert rows[it.key]["question_state"] == "response_needed"


def test_blocked_open_decision_appears_once(client, auth, db):
    from app.services import items as items_svc

    it = items_svc.create_item(db, project_id="core", title="blocked q", status="blocked",
                               question="Which store?", options=["a", "b"])
    ids = [r["id"] for r in _feed(client, auth)["rows"]]
    assert ids.count(it.key) == 1


# --- GRPH-1649: a recorded service drain between cycles is scheduled, not absent -------------


def _supervisor(db, num, last_seen, *, every=None, prd=None, service="P75W-P1",
                stopped=False, state="idle"):
    """A `gbfleet until` planner registration: no seat, and — when it runs as a service — the
    `capabilities.drain` record the supervisor declares (service name, interval, PRD)."""
    from app.models import Agent

    caps = {}
    if every is not None:
        caps["drain"] = {"service": service, "every": every, "prd": prd,
                         **({"stopped": True} if stopped else {})}
    db.add(Agent(id=f"core-A{num}", project_id="core", number=num, label="gbfleet until",
                 active_role="planner", capabilities=caps, state=state,
                 last_seen_at=last_seen, registered_at=last_seen))
    db.commit()


def _seat_agent(db, prd, last_seen, num):
    from app.models import Agent, Enrolment, utcnow

    db.add(Enrolment(id=f"enr-sch-{num}", project_id="core", code_hash="x", role="worker",
                     prd_id=prd.id, expires_at=utcnow() + timedelta(hours=1)))
    db.flush()
    db.add(Agent(id=f"core-A{num}", project_id="core", number=num, enrolment_id=f"enr-sch-{num}",
                 state="idle", last_seen_at=last_seen))
    db.commit()


def test_recorded_service_drain_between_cycles_reads_scheduled_with_no_command(db):
    from app.models import utcnow
    from app.services import fleet as fleet_svc
    from app.services import waits as waits_svc

    prd, _, wait = _answered_pair(db)
    # Exited idle-with-waits 200s ago; installed with --every 300, scoped to this PRD by key.
    _supervisor(db, 91, utcnow() - timedelta(seconds=200), every=300, prd=prd.key)
    assert fleet_svc.drain_status(db, "core", prd.id) == "scheduled"
    note = waits_svc.drain_note(db, wait)
    assert note["status"] == "scheduled"
    assert "No drain is running" not in note["message"] and "next cycle" in note["message"]
    assert "command" not in note


def test_the_window_is_the_recorded_interval_not_a_constant(db):
    from app.models import utcnow
    from app.services import fleet as fleet_svc

    prd, _, _ = _answered_pair(db)
    # --every 900: 600s after its last cycle it is still scheduled.
    _supervisor(db, 92, utcnow() - timedelta(seconds=600), every=900, service="slow")
    assert fleet_svc.drain_status(db, "core", prd.id) == "scheduled"
    # --every 60: 600s of silence is far past 60 + margin — that service is gone.
    from app.models import Agent
    db.delete(db.get(Agent, "core-A92"))
    db.commit()
    _supervisor(db, 93, utcnow() - timedelta(seconds=600), every=60, service="fast")
    assert fleet_svc.drain_status(db, "core", prd.id) == "not_live"


def test_unrecorded_or_stopped_drains_are_not_live_with_the_command(db):
    from app.models import Agent, utcnow
    from app.services import fleet as fleet_svc
    from app.services import waits as waits_svc

    prd, _, wait = _answered_pair(db)
    recent = utcnow() - timedelta(seconds=200)
    # A one-off `until` that exited (no service record) and a finished child on a seat for
    # this PRD, both seen recently: nothing will restart either of them.
    _supervisor(db, 94, recent)
    _seat_agent(db, prd, recent, 95)
    assert fleet_svc.drain_status(db, "core", prd.id) == "not_live"
    note = waits_svc.drain_note(db, wait)
    assert note["message"].startswith("No drain is running")
    assert note["command"] == f"gban fleet until --project core --prd {prd.key}"

    # A service whose newest record says stopped is stopped, though an older cycle is recent.
    _supervisor(db, 96, recent - timedelta(seconds=30), every=300)
    assert fleet_svc.drain_status(db, "core", prd.id) == "scheduled"
    _supervisor(db, 97, recent, every=300, stopped=True)
    assert fleet_svc.drain_status(db, "core", prd.id) == "not_live"
    db.delete(db.get(Agent, "core-A97"))
    db.commit()

    # Past its interval plus margin (booted out by hand: no record since) -> not_live.
    db.get(Agent, "core-A96").last_seen_at = utcnow() - timedelta(
        seconds=300 + fleet_svc.DRAIN_RESTART_MARGIN_SECONDS + 5)
    db.commit()
    assert fleet_svc.drain_status(db, "core", prd.id) == "not_live"


def test_a_service_drain_for_another_prd_does_not_count_an_unscoped_one_does(db):
    from app.models import Agent, utcnow
    from app.services import fleet as fleet_svc
    from app.services import prds as prds_svc

    prd, _, _ = _answered_pair(db)
    other = prds_svc.create_prd(db, project_id="core", title="Other", body="## A\n\nx\n")
    recent = utcnow() - timedelta(seconds=100)
    _supervisor(db, 98, recent, every=300, prd=other.key, service="other")
    assert fleet_svc.drain_status(db, "core", prd.id) == "not_live"
    assert fleet_svc.drain_status(db, "core", other.id) == "scheduled"
    _supervisor(db, 99, recent, every=300, prd=None, service="whole-project")
    assert fleet_svc.drain_status(db, "core", prd.id) == "scheduled"
    # A quarantined supervisor's record does not count.
    db.get(Agent, "core-A99").state = "quarantined"
    db.commit()
    assert fleet_svc.drain_status(db, "core", prd.id) == "not_live"


def test_a_live_seat_still_wins_and_a_failed_read_is_unknown(db):
    from app.models import utcnow
    from app.services import fleet as fleet_svc
    from app.services import waits as waits_svc

    prd, _, wait = _answered_pair(db)
    _supervisor(db, 100, utcnow() - timedelta(seconds=100), every=300)
    _seat_agent(db, prd, utcnow(), 101)
    assert fleet_svc.drain_status(db, "core", prd.id) == "live"

    assert waits_svc.drain_note(db, wait, status="unknown")["status"] == "unknown"

    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    assert fleet_svc.drain_status(Boom(), "core", prd.id) == "unknown"


def test_register_agent_keeps_the_drain_record(client, auth, db):
    """The record arrives on the supervisor's own `register_agent` and is stored as sent."""
    from app.models import Agent
    from app.services import fleet as fleet_svc

    key = client.post("/api/api-keys", json={"name": "drain-sup", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    r = client.post("/api/mcp", headers={"X-API-Key": key}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "register_agent", "arguments": {
            "label": "gbfleet until", "role_hint": "planner", "project_id": "core",
            "capabilities": {"drain": {"service": "P75W-P1", "every": 900, "prd": "CORE-P1"}}}}})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert not res.get("isError"), res
    agent = db.get(Agent, res["structuredContent"]["agent_id"])
    assert fleet_svc.drain_record(agent) == {
        "service": "P75W-P1", "every": 900, "prd": "CORE-P1", "stopped": False}


# --- GRPH-1901: the blocker feed must not cost a statement per blocked row --------------------
#
# Counts are asserted as "does not grow", never as a magic number — the absolute figure differs
# by engine. The shape is the one `tests/test_items_list_perf.py` and `tests/test_query_counts.py`
# use.

SMALL_UNITS = 10
BIG_UNITS = 100


class _Count:
    """Statements the engine runs, in order."""

    def __init__(self, db):
        self.engine, self.n, self.sql = db.get_bind(), 0, []

    def _hit(self, conn, cursor, statement, *a, **k):
        self.n += 1
        self.sql.append(" ".join(statement.split())[:110])

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._hit)


@pytest.fixture()
def perf_project(client, auth):
    """A project of its own: the seeded `core` one carries rows this test does not count."""
    return client.post("/api/projects", json={"name": "Triage Perf"}, headers=auth).json()["id"]


def _seed_blockers(db, project, units, start=0):
    """`units` more blocker triples, in every shape whose rendering used to cost statements.

    Each unit is a decision wait, a ticket blocked behind it AND behind a gate wait (so the
    ticket keeps its own row instead of being folded into the wait's), and the gate. Every
    third wait needs a person, every fifth was handed off with no one to alert, and every
    seventh has a second ticket depending on it — the shapes `question_state`,
    `_nobody_alerted` and `_blocks` each read out of the database.

    Every item declares a touchpoint of its own, which is what an item nobody has touched yet
    does not do. It matters here: the feed's collision source PREDICTS the areas of an item
    that declares none — one embedding and one code-node search each — so a touchpoint-less
    fixture would drown the blocker count this test is about in statements from a different
    source. Unique paths also keep the triples out of each other's clusters.
    """
    from app.services import items as items_svc
    from app.services import links as links_svc

    made = []
    for i in range(start, start + units):
        question = f"q{i}?"
        wait = items_svc.create_item(db, project_id=project, title=question, status="blocked",
                                     question=question, options=["a", "b"],
                                     touchpoints=[f"app/u{i}/wait.py"])
        if i % 3 == 0:
            wait.decision = {**wait.decision, "needs_person_at": "2026-11-24T00:00:00+00:00"}
        alerted = i % 5 == 0
        if alerted:
            wait.decision = {**wait.decision, "nobody_alerted": True}
        gate = items_svc.create_item(db, project_id=project, title=f"gate {i}",
                                     status="blocked", tags=["wait:merge"],
                                     touchpoints=[f"app/u{i}/gate.py"])
        ticket = items_svc.create_item(db, project_id=project, title=f"ticket {i}",
                                       status="in_progress", touchpoints=[f"app/u{i}/work.py"])
        links_svc.create_link(db, a=ticket.id, b=wait.id, type_="dependency", project_id=project)
        links_svc.create_link(db, a=ticket.id, b=gate.id, type_="dependency", project_id=project)
        items_svc.update_item(db, ticket.id, status="blocked")
        extra = None
        if i % 7 == 0:
            extra = items_svc.create_item(db, project_id=project, title=f"extra {i}",
                                          status="in_progress",
                                          touchpoints=[f"app/u{i}/extra.py"])
            links_svc.create_link(db, a=extra.id, b=wait.id, type_="dependency",
                                  project_id=project)
            items_svc.update_item(db, extra.id, status="blocked")
        made.append({
            "i": i, "question": question, "wait": wait.key, "ticket": ticket.key,
            "gate": gate.key, "extra": extra.key if extra else None, "alerted": alerted,
            "state": "response_needed" if i % 3 == 0 else "checking",
        })
    db.commit()
    return made


def _blockers(client, auth, project):
    return {r["id"]: r for r in _feed(client, auth, project)["rows"] if r["kind"] == "blocker"}


def test_the_blocker_feed_statement_count_does_not_grow_with_the_row_count(
        client, auth, db, perf_project):
    _seed_blockers(db, perf_project, SMALL_UNITS)
    db.expire_all()
    with _Count(db) as small:
        little = _blockers(client, auth, perf_project)

    _seed_blockers(db, perf_project, BIG_UNITS - SMALL_UNITS, start=SMALL_UNITS)
    db.expire_all()
    with _Count(db) as big:
        rows = _blockers(client, auth, perf_project)

    assert len(little) == SMALL_UNITS * 3 and len(rows) == BIG_UNITS * 3
    # Equality, not a ceiling: a per-row lookup puts the big count an order of magnitude above
    # the small one, and any bound loose enough to accept that accepts the defect.
    assert small.n == big.n, (small.n, big.n, Counter(big.sql) - Counter(small.sql))
    # Flat because the lookups are batched, not because the rows that need them are absent:
    # a flat count over a feed with no waiting row, no dependant and no unalerted handoff
    # would prove nothing. One floor per lookup the batching serves.
    assert sum(1 for r in rows.values() if r.get("question_state")) >= BIG_UNITS * 2
    assert sum(1 for r in rows.values() if r.get("blocks")) >= BIG_UNITS
    assert sum(1 for r in rows.values() if r.get("blocks_more")) >= BIG_UNITS // 7
    assert sum(1 for r in rows.values() if r.get("nobody_alerted")) >= BIG_UNITS // 5 * 2
    assert sum(1 for r in rows.values() if r.get("also_questions")) >= BIG_UNITS
    assert sum(1 for r in rows.values() if r.get("waiting_on")) >= BIG_UNITS


def test_the_blocker_rows_are_the_ones_a_row_at_a_time_render_builds(
        client, auth, db, perf_project, monkeypatch):
    """Batching changes what the feed COSTS, not what it says.

    Compared against the same render with nothing warmed: `question_states` returning an empty
    map sends every row back to `question_state`'s own link query, which is the per-row read
    this replaced. The two `*_many` helpers have no second implementation to differ from, so
    their values are asserted against what the seed MEANS — a batch wrong in the same way twice
    would otherwise agree with itself.
    """
    from app.services import waits as waits_svc

    units = _seed_blockers(db, perf_project, SMALL_UNITS)
    batched = _blockers(client, auth, perf_project)

    monkeypatch.setattr(waits_svc, "question_states", lambda db, items: {})
    one_at_a_time = _blockers(client, auth, perf_project)

    assert batched == one_at_a_time
    assert len(batched) == SMALL_UNITS * 3
    # The seed really does contain every shape asserted below, so none of them is vacuous.
    assert sum(1 for u in units if u["state"] == "response_needed") >= 1
    assert sum(1 for u in units if u["alerted"]) >= 1
    assert sum(1 for u in units if u["extra"]) >= 1

    for u in units:
        wait, ticket, gate = batched[u["wait"]], batched[u["ticket"]], batched[u["gate"]]
        assert wait["question_state"] == u["state"]
        assert ticket["question_state"] == u["state"]
        assert wait["decision"] == {"question": u["question"], "options": ["a", "b"]}
        # The wait names the ticket that depends on it, the lower-numbered one first, and how
        # many more there are.
        assert wait["blocks"] == {"id": u["ticket"], "title": f"ticket {u['i']}"}
        assert wait.get("blocks_more") == (1 if u["extra"] else None)
        assert wait.get("nobody_alerted", False) is u["alerted"]
        assert ticket.get("nobody_alerted", False) is u["alerted"]
        assert ticket["also_questions"] == [{"id": u["wait"], "question": u["question"]}]
        # Which of the two dependencies is named first is `ctx.deps` order, which no query here
        # sorts — so this asserts that it names one of them, not which.
        assert ticket["waiting_on"]["id"] in {u["wait"], u["gate"]}
        # A gate wait is plain blocked: no state, nothing depending on it, nothing it waits on.
        assert "question_state" not in gate and "blocks" not in gate
        assert "waiting_on" not in gate and "nobody_alerted" not in gate
