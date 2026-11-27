"""GRPH-1978 (GBC-P23 S1/S2, graphban-control #339): holders, non-home item records, `self`.

Every test runs against ``FakeConsole``, which refuses what #339 says the console refuses:
holder fields or a non-home item push unless the console's STORED effective level for this
box is ``full`` (422), an unknown holder key (422), more than one ``self`` or a ``self``
before ``assignable_self`` (422), a bad ``effective_sync`` (422), and a non-home ``prd``,
``comment`` or ``wait`` (403 ``not_home``). A box that sends holders before reporting
``full`` is refused there, not here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    Agent, ApiKey, Membership, Prd, Project, SyncLink, SyncProject, User,
)
from app.security import secrets
from app.services import box_push, code_sync, digest_publish
from app.services import items as items_svc
from app.services import project_sync_client
from app.services import project_sync_flush as flush
from app.services import project_sync_outbox as outbox
from app.services import triage
from tests.project_sync_fake import FakeConsole

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
DUE = T0 + timedelta(seconds=120)
CONSOLE = "https://console.example"
TOKEN = "gb_dep_HOLDERS_TOKEN_MUST_NOT_LEAK_91c2"
OTHER = "gb_dep_HOME_BOX_TOKEN_4410"
AGENT_ID = "CORE-A77"
KEY_ID = "key_holders_7781"
KEY_PREFIX = "gb_sk_hold"
CLAIM_TOKEN = "claimtok_SECRET_0091"
ENROLMENT = "WORKER-Z9Q4XK"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@pytest.fixture()
def db(client):
    box_push.reset_refusal()
    digest_publish.reset()
    flush.reset_runtime()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        box_push.reset_refusal()
        digest_publish.reset()


class Box:
    """A box linked to the fake console, with `core` mapped and two writers on it."""

    def __init__(self, db, monkeypatch, *, home: bool = True, sync_level: str = "sync"):
        self.db = db
        self.console = FakeConsole().install(monkeypatch)
        self.console.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
        self.console.add_deployment(OTHER, engineer="eng-bo", label="Bo")
        self.name = db.get(Project, "core").name
        db.get(Project, "core").publish_item_digests = True
        db.add(SyncLink(id="instance", console_url=CONSOLE, cloud_url=CONSOLE,
                        token_enc=secrets.encrypt(TOKEN), api_key_enc=secrets.encrypt(TOKEN),
                        linked_email="ada@example.test"))
        db.commit()
        # The replica's home is this box, or another deployment that shares it with us.
        if home:
            created = project_sync_client.register_project(
                db, tag="CORE", name=self.name, transport=self.console.transport())
        else:
            db_link = db.get(SyncLink, "instance")
            db_link.token_enc = secrets.encrypt(OTHER)
            db.commit()
            created = project_sync_client.register_project(
                db, tag="CORE", name=self.name, transport=self.console.transport())
            db_link.token_enc = secrets.encrypt(TOKEN)
            db.commit()
        assert created.status == "ok"
        self.cloud = created.cloud_project_id
        db.add(SyncProject(local_project_id="core", cloud_project_id=self.cloud,
                           sync_level=sync_level))
        self._people()
        db.commit()
        self.state = flush.FlushState()

    def _people(self):
        db = self.db
        for uid, name, email in (("u_ada", "Ada Lovelace", "ada@example.test"),
                                 ("u_grace", "Grace Hopper", "grace@example.test")):
            db.add(User(id=uid, name=name, handle=uid.replace("_", "-"), email=email,
                        password_hash="x"))
            db.add(Membership(user_id=uid, project_id="core", role="member", access="write"))
        db.flush()
        db.add(ApiKey(id=KEY_ID, user_id="u_grace", prefix=KEY_PREFIX, hashed_key="h"))
        db.flush()
        db.add(Agent(id=AGENT_ID, project_id="core", number=77, api_key_id=KEY_ID,
                     label="opus @ grace-laptop",
                     capabilities={"vendor": "anthropic", "model": "opus",
                                   "enrolment_code": ENROLMENT}))

    def choose(self, sync, **kw):
        if kw.pop("shared", False):
            self.console.share(self.cloud, TOKEN, sync=sync)
        else:
            self.console.set_choice(self.cloud, TOKEN, sync, **kw)

    def tail(self):
        """The sync tail's GBC-P23 step: read the choice, report the effective level."""
        return code_sync._sync_choice_on_sync(self.db)

    def item(self, title, *, assignee="", claimed_by=None):
        item = items_svc.create_item(self.db, project_id="core", title=title, status="next")
        item.assignee = assignee
        item.claimed_by = claimed_by
        self.db.commit()
        outbox.capture(self.db, project_id="core", entity_type="item", entity_id=item.key,
                       change_class=outbox.CHANGE_STATUS, now=T0)
        self.db.commit()
        return item

    def flush(self):
        return flush.flush_once(self.db, now=DUE, state=self.state, rng=lambda: 0)

    def digests(self):
        return digest_publish.publish_digests(self.db, cloud_url=CONSOLE, api_key=TOKEN)

    def mapping(self):
        self.db.expire_all()
        return self.db.get(SyncProject, "core")

    def reports(self):
        return [level for _, _, level in self.console.effective_reports]

    def paths(self):
        return [r["path"] for r in self.console.requests]


def _item_entity(box, key):
    rows = box.console.entities(box.cloud) + box.console.box_entities(box.cloud, TOKEN)
    return next(e for e in rows if e["type"] == "item" and e["id"] == key)


def _digest_row(box, key):
    return next(r for r in box.console.digests[(TOKEN, box.name)] if r["id"] == key)


# ---- the fake is the console, not the box ------------------------------------------------

def test_the_fake_refuses_holders_and_bad_levels_the_way_339_does(db, monkeypatch):
    box = Box(db, monkeypatch)
    box.choose("full")
    item = box.item("Held", assignee="u-ada")
    # Holders before any report: 422, and nothing stored.
    box.mapping().console_sync = "full"
    db.commit()
    import httpx
    body = {"record": "item_digests", "project": box.name,
            "items": [{"id": item.key, "assignee": {"id": "u_ada", "name": "Ada"}}]}
    assert httpx.post(f"{CONSOLE}/api/sync/item-digests", json=body,
                      headers={"X-API-Key": TOKEN}).status_code == 422
    assert (TOKEN, box.name) not in box.console.digests
    bad = project_sync_client._call(
        db, "POST", "/api/sync/project-sync", json_body={"effective_sync": "sync"},
        extra_headers={"X-Cloud-Project": box.cloud})[0]
    assert bad.status == "refused" and bad.field == "effective_sync"
    assert project_sync_client.report_effective_sync(
        db, cloud_project_id=box.cloud, effective_sync="full").status == "ok"
    body["items"][0]["assignee"]["email"] = "ada@example.test"
    assert httpx.post(f"{CONSOLE}/api/sync/item-digests", json=body,
                      headers={"X-API-Key": TOKEN}).status_code == 422
    people = f"{CONSOLE}/api/sync/assignable-people"
    one_self = {"people": [{"id": "u", "name": "U", "self": True}]}
    assert httpx.post(people, params={"project": box.name}, json=one_self,
                      headers={"X-API-Key": TOKEN}).status_code == 422  # no assignable_self
    box.console.receipt_capabilities = ["assignable_self"]
    two = {"people": [{"id": "u", "name": "U", "self": True},
                      {"id": "v", "name": "V", "self": True}]}
    assert httpx.post(people, params={"project": box.name}, json=two,
                      headers={"X-API-Key": TOKEN}).status_code == 422


# ---- effective level ----------------------------------------------------------------------

def test_full_and_sync_reports_full_before_the_first_holder_push(db, monkeypatch):
    box = Box(db, monkeypatch)
    box.choose("full")
    item = box.item("Agent holds it", assignee="u-ada", claimed_by=AGENT_ID)
    box.tail()
    assert box.reports() == ["full"]
    assert box.flush() == 1
    assert box.digests()["errors"] == []
    paths = box.paths()
    assert paths.index("/api/sync/project-sync") < paths.index("/api/sync/project-entities")
    assert paths.index("/api/sync/project-sync") < paths.index("/api/sync/item-digests")
    assert box.console.refusals == []

    body = _item_entity(box, item.key)["body"]
    row = _digest_row(box, item.key)
    for holder in (body, row):
        assert holder["assignee"] == {"id": "u_ada", "name": "Ada Lovelace"}
        assert holder["held_by"] == {
            "kind": "agent", "label": "opus @ grace-laptop",
            "owner": {"id": "u_grace", "name": "Grace Hopper"}, "agent_kind": "anthropic",
        }


def test_nothing_secret_is_in_any_body_or_digest_under_full(db, monkeypatch):
    box = Box(db, monkeypatch)
    box.choose("full")
    item = box.item("Leak check", assignee="u-ada", claimed_by=AGENT_ID)
    # A lease under a name the box cannot place: the string itself must not go out.
    box.item("Unknown lease", claimed_by=CLAIM_TOKEN)
    box.tail()
    box.flush()
    box.digests()
    box.console.receipt_capabilities = ["assignable_self"]
    box_push.push_box_view(db, cloud_url=CONSOLE, api_key=TOKEN)
    triage.push_assignable_people(db, cloud_url=CONSOLE, api_key=TOKEN)

    wire = json.dumps(box.console.accepted_bodies + list(box.console.people.values()))
    assert box.console.accepted_bodies and "held_by" in wire
    assert EMAIL_RE.search(wire) is None
    for secret in (AGENT_ID, KEY_ID, KEY_PREFIX, CLAIM_TOKEN, ENROLMENT, TOKEN):
        assert secret not in wire, secret


@pytest.mark.parametrize("console_sync, box_level", [
    ("full", "digests"), (None, "sync"), ("summaries", "sync")])
def test_anything_short_of_both_reports_summaries_and_sends_no_holder(
        db, monkeypatch, console_sync, box_level):
    box = Box(db, monkeypatch, sync_level=box_level)
    box.choose(console_sync)
    item = box.item("Not full", assignee="u-ada", claimed_by=AGENT_ID)
    box.tail()
    assert box.reports() == ["summaries"]
    box.flush()
    box.digests()
    assert box.console.refusals == []
    row = _digest_row(box, item.key)
    assert "assignee" not in row and "held_by" not in row
    if box_level == "sync":
        body = _item_entity(box, item.key)["body"]
        assert "assignee" not in body and "held_by" not in body


def test_a_change_to_either_input_reports_the_new_effective_level(db, monkeypatch):
    box = Box(db, monkeypatch)
    box.choose("summaries")
    box.tail()
    assert box.reports() == ["summaries"]
    box.choose("full")  # the console widens its choice; the box is at sync
    box.tail()
    assert box.reports() == ["summaries", "full"]
    box.tail()  # nothing moved: no new report
    assert box.reports() == ["summaries", "full"]
    mapping = box.mapping()
    mapping.sync_level = outbox.SYNC_LEVEL_DIGESTS  # the box admin narrows
    db.commit()
    box.tail()
    assert box.reports() == ["summaries", "full", "summaries"]
    assert box.mapping().reported_sync == "summaries"


def test_the_admin_panel_shows_console_box_and_effective_levels(db, monkeypatch):
    from app.services import project_sync_admin

    box = Box(db, monkeypatch, sync_level="digests")
    box.choose("full")
    box.tail()
    view = project_sync_admin.project_view(db, "core")
    assert (view["console_sync"], view["sync_level"], view["effective_sync"]) == (
        "full", "digests", "summaries")
    assert view["console_home"] is True


# ---- non-home item records ------------------------------------------------------------------

def _prd_dirty(box):
    prd = box.db.scalars(select(Prd).where(Prd.project_id == "core")).first()
    if prd is None:
        from app.services import prds as prds_svc
        prd = prds_svc.create_prd(box.db, project_id="core", title="Spec", body="## A\nx")
    outbox.capture(box.db, project_id="core", entity_type="prd", entity_id=prd.key,
                   change_class=outbox.CHANGE_EDIT, now=T0)
    box.db.commit()
    return prd


def test_a_non_home_box_at_full_pushes_only_its_items(db, monkeypatch):
    box = Box(db, monkeypatch, home=False)
    box.choose("full", shared=True)
    item = box.item("Mine", assignee="u-ada")
    _prd_dirty(box)
    box.tail()
    assert box.mapping().console_home is False and box.reports() == ["full"]
    assert box.flush() == 1
    assert box.console.refusals == []
    pushed = box.console.box_entities(box.cloud, TOKEN)
    assert [e["type"] for e in pushed] == ["item"] and pushed[0]["id"] == item.key
    assert pushed[0]["body"]["status_history"] is not None
    assert box.console.entities(box.cloud) == []  # the home's rows are untouched
    # The prd row is not left due forever on a box that can never send it.
    assert [r for r in outbox.due(db, DUE) if r.entity_type == "prd"] == []


def test_a_non_home_box_at_summaries_pushes_no_entities(db, monkeypatch):
    box = Box(db, monkeypatch, home=False)
    box.choose("summaries", shared=True)
    box.item("Mine")
    _prd_dirty(box)
    box.tail()
    box.flush()
    assert "/api/sync/project-entities" not in box.paths()
    assert box.console.box_entities(box.cloud, TOKEN) == []


def test_a_missing_home_pushes_as_today(db, monkeypatch):
    box = Box(db, monkeypatch)
    box.choose("full", omit_home=True)
    item = box.item("Item")
    prd = _prd_dirty(box)
    box.tail()
    assert box.mapping().console_home is None
    box.flush()
    stored = {(e["type"], e["id"]) for e in box.console.entities(box.cloud)}
    assert ("item", item.key) in stored and ("prd", prd.key) in stored


def test_a_not_home_refusal_still_decides_and_shows_in_link_status(db, monkeypatch):
    from app.services import link_device

    box = Box(db, monkeypatch, home=False)
    box.choose("full", shared=True)
    box.console._by_id[box.cloud].home_omitted.add(TOKEN)  # the console leaves `home` out
    _prd_dirty(box)
    box.tail()
    box.flush()
    assert ("/api/sync/project-entities", 403, "not_home", None) in box.console.refusals
    view = link_device.project_view(box.mapping())
    assert view["last_push_result"] == "refused" and "not_home" in view["last_push_reason"]


# ---- refusals -------------------------------------------------------------------------------

def test_a_holder_422_is_shown_then_rereads_rereports_and_retries(db, monkeypatch):
    from app.services import link_device

    box = Box(db, monkeypatch)
    box.choose("full")
    item = box.item("Held", assignee="u-ada")
    box.tail()
    # The console lost its stored level (another box, a console restore).
    box.console._by_id[box.cloud].effective.pop(TOKEN)
    out = box.digests()
    assert out["errors"] == []
    assert ("/api/sync/item-digests", 422, "holder fields need effective_sync full",
            "assignee") in box.console.refusals
    assert box.reports() == ["full", "full"]  # re-reported after the 422
    assert _digest_row(box, item.key)["assignee"]["id"] == "u_ada"  # the retry landed
    view = link_device.project_view(box.mapping())
    assert view["last_push_result"] == "refused" and "item-digests 422" in view["last_push_reason"]


def test_a_holder_422_on_entities_retries_under_the_level_now_held(db, monkeypatch):
    box = Box(db, monkeypatch)
    box.choose("full")
    item = box.item("Held", assignee="u-ada")
    box.tail()
    # The console narrowed and dropped this box's stored level; the box has not re-read.
    box.console.set_choice(box.cloud, TOKEN, "summaries")
    box.console._by_id[box.cloud].effective[TOKEN] = "summaries"
    assert box.flush() == 1
    assert any(r[1] == 422 and r[3] == "assignee" for r in box.console.refusals)
    assert box.reports()[-1] == "summaries"
    body = _item_entity(box, item.key)["body"]
    assert "assignee" not in body


# ---- self -----------------------------------------------------------------------------------

def test_self_marks_only_the_linked_engineer_and_only_after_assignable_self(db, monkeypatch):
    box = Box(db, monkeypatch)
    triage.push_assignable_people(db, cloud_url=CONSOLE, api_key=TOKEN)
    before = box.console.people[(TOKEN, box.name)]
    assert all("self" not in p for p in before)

    box.console.receipt_capabilities = ["box_build"]  # a receipt without it grants nothing
    box_push.push_box_view(db, cloud_url=CONSOLE, api_key=TOKEN)
    triage.push_assignable_people(db, cloud_url=CONSOLE, api_key=TOKEN)
    assert all("self" not in p for p in box.console.people[(TOKEN, box.name)])

    box.console.receipt_capabilities = ["box_build", "assignable_self"]
    box_push.push_box_view(db, cloud_url=CONSOLE, api_key=TOKEN)
    out = triage.push_assignable_people(db, cloud_url=CONSOLE, api_key=TOKEN)
    assert out["errors"] == [] and box.console.refusals == []
    people = box.console.people[(TOKEN, box.name)]
    assert [p["id"] for p in people if p.get("self")] == ["u_ada"]
    assert all(set(p) <= {"id", "name", "self"} for p in people)


def test_nobody_is_self_when_no_one_matches_the_linked_email(db, monkeypatch):
    box = Box(db, monkeypatch)
    db.get(SyncLink, "instance").linked_email = "someone-else@example.test"
    db.commit()
    box.console.receipt_capabilities = ["assignable_self"]
    box_push.push_box_view(db, cloud_url=CONSOLE, api_key=TOKEN)
    triage.push_assignable_people(db, cloud_url=CONSOLE, api_key=TOKEN)
    assert all("self" not in p for p in box.console.people[(TOKEN, box.name)])


def test_no_holder_goes_out_while_the_report_has_no_answer(db, monkeypatch):
    """The console chose `full` and the box is at `sync`, but no 200 has stored it: the
    bodies go out without holders, and the console therefore refuses nothing."""
    box = Box(db, monkeypatch)
    box.choose("full")
    item = box.item("Held", assignee="u-ada", claimed_by=AGENT_ID)
    monkeypatch.setattr(
        project_sync_client, "report_effective_sync",
        lambda *a, **k: project_sync_client.SyncResult(status="unknown", reason="timeout"))
    box.tail()
    assert box.mapping().console_sync == "full" and box.mapping().reported_sync is None
    assert box.flush() == 1
    box.digests()
    assert box.console.refusals == []
    assert "held_by" not in _item_entity(box, item.key)["body"]
    assert "held_by" not in _digest_row(box, item.key)
