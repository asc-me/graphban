"""Pull cloud proposals and apply them on the box (GRPH-1737).

Permission, the never-from-cloud rules, conflict, and exactly-once are all
asserted against the fake console. The interval uses one fake clock.
"""
from __future__ import annotations

import importlib.util
import pathlib
from datetime import timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text

from app.db import SessionLocal
from app.models import Event, Item, Project, SyncLink, SyncProject, SyncProposal
from app.security import secrets
from app.services import items as items_svc
from app.services import project_sync_client
from app.services import project_sync_outbox as outbox
from app.services import project_sync_payload as payload
from app.services import project_sync_proposals as proposals
from app.services import waits
from app.services.project_sync_client import SyncResult
from tests.project_sync_fake import FakeConsole
from tests.test_project_sync_outbox import T0

CONSOLE = "https://console.example"
TOKEN = "gb_dep_PROJECT_SYNC_TOKEN_MUST_NOT_LEAK_7f3a"


@pytest.fixture()
def db(client):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _no_outcome_backoff():
    """The outcome backoff is process state. One test's refusal must not hold the next."""
    proposals._outcome_backoff.clear()
    yield
    proposals._outcome_backoff.clear()


@pytest.fixture()
def fake():
    console = FakeConsole()
    console.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
    return console


@pytest.fixture()
def linked(db, fake):
    created = _link_and_register(db, fake)
    return created


def _link_and_register(db, fake):
    row = db.get(SyncLink, "instance")
    if row is None:
        db.add(SyncLink(id="instance", console_url=CONSOLE, cloud_url=CONSOLE,
                        token_enc=secrets.encrypt(TOKEN)))
    else:
        row.console_url = CONSOLE
        row.cloud_url = CONSOLE
        row.token_enc = secrets.encrypt(TOKEN)
    db.commit()
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=fake.transport())
    assert created.status == "ok" and created.cloud_project_id
    return created


def _mapping(db, cloud_project_id, *, permission=outbox.CLOUD_PERMISSION_PROPOSALS,
             level=outbox.SYNC_LEVEL_SYNC, project_id="core"):
    row = SyncProject(
        local_project_id=project_id, cloud_project_id=cloud_project_id,
        sync_level=level, cloud_permission=permission,
    )
    db.add(row)
    db.commit()
    return row


def _item(db, title="Before", project_id="core", **kw):
    return items_svc.create_item(db, title=title, project_id=project_id, **kw)


def _version(db, item):
    db.expire_all()
    return payload.entity(db, "item", item.key)["version"]


def _target(item):
    return {"type": "item", "id": item.key}


def _enqueue(fake, cloud_id, **kw):
    kw.setdefault("target", None)
    kw.setdefault("base_version", None)
    kw.setdefault("payload", {})
    kw.setdefault("proposer", {"label": "Ada", "role": "member"})
    return fake.enqueue_proposal(cloud_id, **kw)


def _comments(db, item):
    db.expire_all()
    return list(db.scalars(select(Event).where(
        Event.action == "comment",
        Event.target_id.in_([item.id, item.key]),
    )).all())


def _history(db, item, action):
    db.expire_all()
    return list(db.scalars(select(Event).where(
        Event.action == action,
        Event.target_id.in_([item.id, item.key]),
    )).all())


def _outcome_posts(fake):
    return [row for row in fake.requests if row["path"] == "/api/sync/proposals/outcomes"]


def test_read_only_refuses_a_comment_comments_refuse_an_edit_proposals_apply_one(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id, permission=outbox.CLOUD_PERMISSION_READ_ONLY)
    item = _item(db)
    comment = _enqueue(
        fake, mapping.cloud_project_id, kind="comment", target=_target(item),
        payload={"text": "from the cloud"})
    refused = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())
    assert refused[0]["id"] == comment["id"]
    assert refused[0]["status"] == "refused"
    assert refused[0]["reason"] == proposals.REASON_READ_ONLY
    assert _comments(db, item) == []
    assert fake.outcome_for(comment["id"])["status"] == "refused"
    assert fake.outcome_for(comment["id"])["reason"] == proposals.REASON_READ_ONLY

    mapping = db.get(SyncProject, "core")
    mapping.cloud_permission = outbox.CLOUD_PERMISSION_COMMENTS
    db.commit()
    edit = _enqueue(
        fake, mapping.cloud_project_id, kind="edit", target=_target(item),
        base_version=_version(db, item), payload={"title": "Nope"})
    held = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())
    assert held[-1]["status"] == "refused"
    assert held[-1]["reason"] == proposals.REASON_COMMENTS
    db.expire_all()
    assert db.get(Item, item.id).title == "Before"

    mapping = db.get(SyncProject, "core")
    mapping.cloud_permission = outbox.CLOUD_PERMISSION_PROPOSALS
    db.commit()
    applied_proposal = _enqueue(
        fake, mapping.cloud_project_id, kind="edit", target=_target(item),
        base_version=_version(db, item), payload={"title": "Now"})
    applied = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())
    assert applied[-1]["status"] == "applied"
    assert applied[-1]["id"] == applied_proposal["id"]
    db.expire_all()
    assert db.get(Item, item.id).title == "Now"
    assert fake.outcome_for(applied_proposal["id"]) == {"status": "applied"}


def test_status_done_is_refused_at_proposals_even_when_the_version_matches(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    item = _item(db)
    current = _version(db, item)
    proposal = _enqueue(
        fake, mapping.cloud_project_id, kind="edit", target=_target(item),
        base_version=current, payload={"status": "done", "title": "Nope"})
    outcome = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())[-1]
    assert outcome["id"] == proposal["id"]
    assert outcome["status"] == "refused"
    assert outcome["reason"] == proposals.REASON_DONE
    db.expire_all()
    stored = db.get(Item, item.id)
    assert stored.status != "done"
    assert stored.title == "Before"

    stale = _enqueue(
        fake, mapping.cloud_project_id, kind="edit", target=_target(item),
        base_version=current ^ 1, payload={"status": "done"})
    again = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())[-1]
    assert again["id"] == stale["id"]
    assert again["status"] == "refused"
    assert again["reason"] == proposals.REASON_DONE
    assert "current_version" not in again


def test_a_stale_base_version_conflicts_and_leaves_the_item_unchanged(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    item = _item(db)
    current = _version(db, item)
    proposal = _enqueue(
        fake, mapping.cloud_project_id, kind="edit", target=_target(item),
        base_version=current ^ 1, payload={"title": "Nope"})
    outcome = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())[-1]
    assert outcome["status"] == "conflict"
    assert outcome["current_version"] == current
    assert outcome["id"] == proposal["id"]
    db.expire_all()
    assert db.get(Item, item.id).title == "Before"
    assert fake.outcome_for(proposal["id"])["status"] == "conflict"
    assert fake.outcome_for(proposal["id"])["current_version"] == current


def test_current_version_column_holds_a_63_bit_version():
    """The column stays BIGINT. It once stored a 63-bit hash, and Postgres int4
    refused that (CI shard 1/3). SQLite does not check width, so the column
    type is asserted here. New wire versions fit in 2^53-1."""
    from sqlalchemy import BigInteger
    assert isinstance(SyncProposal.__table__.c.current_version.type, BigInteger)
    source = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "alembic", "versions", "0212_sync_proposals.py").read_text()
    assert 'sa.Column("current_version", sa.BigInteger()' in source


def test_the_same_comment_proposal_twice_creates_one_comment_and_reports_applied_twice(
        db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    item = _item(db)
    proposal = _enqueue(
        fake, mapping.cloud_project_id, kind="comment", target=_target(item),
        payload={"text": "once"})
    first = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())
    assert first[0]["status"] == "applied"
    second = proposals.apply_proposal(
        db, db.get(SyncProject, "core"), proposal, transport=fake.transport())
    assert second["status"] == "applied"
    assert len(_comments(db, item)) == 1
    assert len(_outcome_posts(fake)) == 2
    assert fake.outcome_for(proposal["id"])["status"] == "applied"


def test_an_open_wait_drops_the_interval_to_10s_and_closing_it_restores_60s(db, fake, linked):
    """The interval is read by ``due``, from the last poll, on the caller's clock."""
    db.add(Project(id="syncbox", name="Sync Box", tag="SBOX"))
    db.commit()
    _link = linked
    mapping = _mapping(db, linked.cloud_project_id, project_id="syncbox")
    proposals.run_due(db, T0, transport=fake.transport())
    assert proposals.due(db, T0 + timedelta(seconds=9)) == []
    assert [row.local_project_id for row in proposals.due(db, T0 + timedelta(seconds=60))] == [
        "syncbox"]

    wait = _item(
        db, title="Decide", project_id="syncbox", status="blocked",
        question="Ship it?", options=["yes", "no"])
    assert proposals.due(db, T0 + timedelta(seconds=9)) == []
    assert [row.local_project_id for row in proposals.due(db, T0 + timedelta(seconds=10))] == [
        "syncbox"]

    waits.answer_wait(db, wait.id, choice="yes", actor=type("U", (), {"handle": "ada"})())
    assert proposals.due(db, T0 + timedelta(seconds=10)) == []
    assert [row.local_project_id for row in proposals.due(db, T0 + timedelta(seconds=60))] == [
        "syncbox"]
    assert waits.open_decision_waits(db, "syncbox") == []


def test_an_applied_edit_is_attributed_to_the_cloud_proposer(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    item = _item(db)
    proposal = _enqueue(
        fake, mapping.cloud_project_id, kind="edit", target=_target(item),
        base_version=_version(db, item), payload={"title": "From Ada"},
        proposer={"label": "Ada", "role": "member"})
    proposals.pull_project(db, mapping, now=T0, transport=fake.transport())
    events = _history(db, item, "update_item")
    assert len(events) == 1
    assert events[0].actor_type == "cloud"
    assert events[0].actor_label == "cloud:Ada"
    assert events[0].meta["proposal_id"] == proposal["id"]
    assert events[0].surface == "sync"


def test_a_failed_report_stays_queued_and_the_next_pull_does_not_reapply(db, fake, linked, monkeypatch):
    mapping = _mapping(db, linked.cloud_project_id)
    item = _item(db)
    proposal = _enqueue(
        fake, mapping.cloud_project_id, kind="comment", target=_target(item),
        payload={"text": "keep me"})

    def _unknown(db, *, project, outcomes, transport=None):
        return SyncResult(status="unknown", reason="down")

    monkeypatch.setattr(project_sync_client, "report_outcomes", _unknown)
    proposals.pull_project(db, mapping, now=T0, transport=fake.transport())
    assert len(_comments(db, item)) == 1
    receipt = db.get(SyncProposal, proposal["id"])
    assert receipt is not None and receipt.status == "applied" and receipt.reported is False
    assert fake.outcome_for(proposal["id"]) is None

    monkeypatch.undo()
    proposals.pull_project(
        db, db.get(SyncProject, "core"), now=T0 + timedelta(seconds=60),
        transport=fake.transport())
    assert len(_comments(db, item)) == 1
    db.expire_all()
    assert db.get(SyncProposal, proposal["id"]).reported is True
    assert fake.outcome_for(proposal["id"])["status"] == "applied"


def test_after_push_makes_the_project_due_immediately(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    proposals.pull_project(db, mapping, now=T0, transport=fake.transport())
    assert proposals.due(db, T0) == []
    assert proposals.after_push(db, "core") is True
    assert [row.local_project_id for row in proposals.due(db, T0)] == ["core"]


def test_a_304_keeps_the_cursor(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    proposals.pull_project(db, mapping, now=T0, transport=fake.transport())
    db.expire_all()
    stored = db.get(SyncProject, "core")
    cursor, etag = stored.proposal_cursor, stored.proposal_etag
    assert cursor not in (None, "")
    assert etag
    proposals.pull_project(db, stored, now=T0 + timedelta(seconds=60), transport=fake.transport())
    db.expire_all()
    again = db.get(SyncProject, "core")
    assert again.proposal_cursor == cursor
    assert again.proposal_etag == etag
    assert fake.requests[-1]["headers"].get("if-none-match") == etag


def test_an_answer_is_attributed_to_the_cloud_proposer(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    wait = _item(db, title="Decide", status="blocked", question="Ship it?", options=["yes", "no"])
    proposal = _enqueue(
        fake, mapping.cloud_project_id, kind="answer", target=_target(wait),
        payload={"choice": "yes"}, proposer={"label": "Ada", "role": "member"})
    outcome = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())[-1]
    assert outcome["status"] == "applied"
    db.expire_all()
    stored = db.get(Item, wait.id)
    assert stored.decision["answer"]["choice"] == "yes"
    assert stored.decision["answer"]["by"]["user"] == "cloud:Ada"
    events = _history(db, wait, "blocker.answer")
    assert len(events) == 1
    assert events[0].actor_type == "cloud"
    assert events[0].actor_label == "cloud:Ada"
    assert events[0].meta["proposal_id"] == proposal["id"]


def test_sign_off_waiver_prd_close_and_rules_are_refused_by_name(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    item = _item(db)
    current = _version(db, item)
    sign_off = _enqueue(
        fake, mapping.cloud_project_id, kind="edit", target=_target(item),
        base_version=current, payload={"title": "Nope", "sign_off": True})
    waiver = _enqueue(
        fake, mapping.cloud_project_id, kind="edit", target=_target(item),
        base_version=current,
        payload={"evidence": [{"kind": "attestation", "adapter": "ci"}]})
    closing = _enqueue(
        fake, mapping.cloud_project_id, kind="edit",
        target={"type": "prd", "id": "GRPH-P1"}, payload={"status": "done"})
    rules = _enqueue(
        fake, mapping.cloud_project_id, kind="edit",
        target={"type": "rule", "id": "rule-1"}, payload={})
    outcomes = {
        row["id"]: row for row in proposals.pull_project(
            db, mapping, now=T0, transport=fake.transport())
    }
    assert outcomes[sign_off["id"]]["reason"] == proposals.REASON_SIGN_OFF
    assert outcomes[waiver["id"]]["reason"] == proposals.REASON_WAIVER
    assert outcomes[closing["id"]]["reason"] == proposals.REASON_PRD_CLOSE
    assert outcomes[rules["id"]]["reason"] == proposals.REASON_RULES
    db.expire_all()
    assert db.get(Item, item.id).title == "Before"


def test_a_create_lands_in_the_mapped_project_and_an_empty_title_is_refused(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    created = _enqueue(
        fake, mapping.cloud_project_id, kind="create",
        payload={"title": "From cloud", "description": "via a proposal"})
    empty = _enqueue(
        fake, mapping.cloud_project_id, kind="create", payload={"title": "  "})
    done = _enqueue(
        fake, mapping.cloud_project_id, kind="create",
        payload={"title": "Finished", "status": "done"})
    outcomes = {
        row["id"]: row for row in proposals.pull_project(
            db, mapping, now=T0, transport=fake.transport())
    }
    assert outcomes[created["id"]]["status"] == "applied"
    assert outcomes[empty["id"]]["status"] == "refused"
    assert outcomes[done["id"]]["status"] == "refused"
    assert outcomes[done["id"]]["reason"] == proposals.REASON_DONE
    db.expire_all()
    rows = list(db.scalars(select(Item).where(Item.title == "From cloud")).all())
    assert len(rows) == 1 and rows[0].project_id == "core"
    assert db.scalars(select(Item).where(Item.title == "Finished")).all() == []
    events = _history(db, rows[0], "create_item")
    assert events[0].actor_label == "cloud:Ada"
    assert events[0].meta["proposal_id"] == created["id"]


def test_an_unknown_permission_refuses_every_proposal(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id, permission="admin")
    item = _item(db)
    proposal = _enqueue(
        fake, mapping.cloud_project_id, kind="comment", target=_target(item),
        payload={"text": "no"})
    outcome = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())[-1]
    assert outcome["id"] == proposal["id"]
    assert outcome["status"] == "refused"
    assert outcome["reason"] == "cloud permission 'admin' refuses every proposal"
    assert _comments(db, item) == []


def test_a_project_at_digests_is_not_due(db, fake, linked):
    _mapping(db, linked.cloud_project_id, level=outbox.SYNC_LEVEL_DIGESTS)
    assert proposals.due(db, T0) == []
    assert proposals.after_push(db, "core") is False


def test_existing_sync_project_rows_gain_a_cursor_and_sync_proposal_appears():
    """A row written before 0212 keeps its mapping and gains an empty cursor."""
    path = pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0212_sync_proposals.py"
    spec = importlib.util.spec_from_file_location("mig_0212", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sync_project ("
            "local_project_id VARCHAR PRIMARY KEY, cloud_project_id VARCHAR NOT NULL, "
            "sync_level VARCHAR NOT NULL, cloud_permission VARCHAR NOT NULL)"))
        conn.execute(text(
            "INSERT INTO sync_project (local_project_id, cloud_project_id, sync_level, "
            "cloud_permission) VALUES ('core', 'cloud-1', 'sync', 'comments')"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()
        row = conn.execute(text(
            "SELECT proposal_cursor, proposal_etag, proposal_polled_at, proposal_pull_now, "
            "sync_level, cloud_permission FROM sync_project WHERE local_project_id = 'core'"
        )).one()
        tables = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table'")).all()}
    assert row[0] == ""
    assert row[1] is None and row[2] is None
    assert row[3] in (0, False)
    assert row[4] == "sync" and row[5] == "comments"
    assert "sync_proposal" in tables

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.downgrade()
        tables = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table'")).all()}
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(sync_project)")).all()}
        kept = conn.execute(text(
            "SELECT cloud_project_id FROM sync_project WHERE local_project_id = 'core'")).one()
    assert "sync_proposal" not in tables
    assert "proposal_cursor" not in cols and "proposal_pull_now" not in cols
    assert kept[0] == "cloud-1"


# ---- GRPH-1842: the real console's proposal wire ------------------------------------------

#: graphban-control docs/contract.md, Proposals: the pull row. Optional keys are
#: omitted when null, so this is the full set when every one is present.
CONSOLE_PULL_KEYS = {"id", "kind", "target_type", "target_id", "base_version", "payload",
                     "proposer_label", "created_at"}


def _register(db, fake, tag, name):
    created = project_sync_client.register_project(
        db, tag=tag, name=name, transport=fake.transport())
    assert created.status == "ok"
    return created.cloud_project_id


def test_the_fake_serves_exactly_the_console_pull_row(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    item = _item(db)
    _enqueue(fake, mapping.cloud_project_id, kind="edit", target=_target(item),
             base_version=1, payload={"title": "x"})
    _enqueue(fake, mapping.cloud_project_id, kind="create", payload={"title": "y"})
    page = fake.handle(_pull_request(mapping.cloud_project_id)).json()["proposals"]
    assert set(page[0]) == CONSOLE_PULL_KEYS
    assert page[0]["target_type"] == "item" and page[0]["target_id"] == item.key
    assert page[0]["proposer_label"] == "Ada"
    # A create has no target and no base version: the console omits them.
    assert set(page[1]) == CONSOLE_PULL_KEYS - {"target_type", "target_id", "base_version"}
    assert "target" not in page[0] and "proposer" not in page[0]


def _pull_request(cloud_id):
    import httpx

    return httpx.Request(
        "GET", f"{CONSOLE}/api/sync/proposals",
        params={"project": cloud_id, "after": ""},
        headers={"Authorization": f"Bearer {TOKEN}", "X-Cloud-Project": cloud_id},
    )


def test_a_flat_comment_reaches_its_item_and_the_outcome_names_the_project(db, fake, linked):
    mapping = _mapping(db, linked.cloud_project_id)
    item = _item(db)
    proposal = _enqueue(fake, mapping.cloud_project_id, kind="comment",
                        target=_target(item), payload={"text": "from the cloud"})
    outcome = proposals.pull_project(db, mapping, now=T0, transport=fake.transport())[-1]
    assert outcome == {"id": proposal["id"], "status": "applied"}
    comments = _comments(db, item)
    assert len(comments) == 1 and comments[0].actor_label == "cloud:Ada"
    posts = _outcome_posts(fake)
    assert posts, "no outcome was posted"
    assert all(p["params"].get("project") == mapping.cloud_project_id for p in posts)
    assert all(p["headers"].get("x-cloud-project") == mapping.cloud_project_id for p in posts)
    assert fake.outcome_for(proposal["id"]) == {"status": "applied"}
    db.expire_all()
    assert db.get(SyncProposal, proposal["id"]).reported is True


def test_the_fake_refuses_an_outcome_post_that_names_no_project(db, fake, linked):
    result = project_sync_client.report_outcomes(
        db, project="", outcomes=[{"id": "pr1", "status": "applied"}],
        transport=fake.transport())
    assert fake.last_status == 422
    assert result.status == "refused" and result.reason == "project is required"
    assert result.field == "project"


def test_outcomes_for_two_projects_go_as_two_calls_each_naming_its_own(db, fake, linked):
    first = _mapping(db, linked.cloud_project_id)
    db.add(Project(id="ops", name="Ops", tag="OPS"))
    db.commit()
    second = _mapping(db, _register(db, fake, "OPS", "Ops"), project_id="ops")
    a = _item(db)
    b = _item(db, project_id="ops")
    pa = _enqueue(fake, first.cloud_project_id, kind="comment", target=_target(a),
                  payload={"text": "a"})
    pb = _enqueue(fake, second.cloud_project_id, kind="comment", target=_target(b),
                  payload={"text": "b"})

    def _down(db, *, project, outcomes, transport=None):
        return SyncResult(status="unknown", reason="down")

    real = project_sync_client.report_outcomes
    project_sync_client.report_outcomes = _down
    try:
        proposals.pull_project(db, first, now=T0, transport=fake.transport())
        proposals.pull_project(db, second, now=T0, transport=fake.transport())
    finally:
        project_sync_client.report_outcomes = real
    before = len(_outcome_posts(fake))
    proposals.pull_project(db, db.get(SyncProject, "core"), now=T0 + timedelta(seconds=11),
                           transport=fake.transport())
    proposals.pull_project(db, db.get(SyncProject, "ops"), now=T0 + timedelta(seconds=11),
                           transport=fake.transport())
    posts = _outcome_posts(fake)[before:]
    assert [p["params"]["project"] for p in posts] == [first.cloud_project_id,
                                                        second.cloud_project_id]
    assert fake.outcome_for(pa["id"]) == {"status": "applied"}
    assert fake.outcome_for(pb["id"]) == {"status": "applied"}
    db.expire_all()
    assert db.get(SyncProposal, pa["id"]).reported is True
    assert db.get(SyncProposal, pb["id"]).reported is True


def test_a_refused_outcome_post_backs_off_instead_of_going_every_poll(db, fake, linked, monkeypatch):
    mapping = _mapping(db, linked.cloud_project_id)
    item = _item(db)
    _enqueue(fake, mapping.cloud_project_id, kind="comment", target=_target(item),
             payload={"text": "held"})
    calls = []

    def _refused(db, *, project, outcomes, transport=None):
        calls.append(project)
        return SyncResult(status="refused", reason="project is required", field="project")

    monkeypatch.setattr(project_sync_client, "report_outcomes", _refused)
    proposals.pull_project(db, mapping, now=T0, transport=fake.transport())
    assert len(calls) == 1
    # The next ~10 s poll: still inside the backoff, so no post.
    proposals.pull_project(db, db.get(SyncProject, "core"), now=T0 + timedelta(seconds=10),
                           transport=fake.transport())
    assert len(calls) == 1
    # Past the first step: one more try, which doubles the wait.
    after = T0 + proposals.OUTCOME_BACKOFF_START + timedelta(seconds=1)
    proposals.pull_project(db, db.get(SyncProject, "core"), now=after,
                           transport=fake.transport())
    assert len(calls) == 2
    proposals.pull_project(db, db.get(SyncProject, "core"),
                           now=after + proposals.OUTCOME_BACKOFF_START + timedelta(seconds=1),
                           transport=fake.transport())
    assert len(calls) == 2
    db.expire_all()
    assert all(r.reported is False for r in db.scalars(select(SyncProposal)).all())

