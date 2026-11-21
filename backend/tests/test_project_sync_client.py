"""GRPH-1732: the project-sync client against the in-process contract fake.

A timeout must come back ``unknown``. Mapping it to ``refused`` fails
``test_a_timeout_is_unknown_and_not_refused``.
"""
import json
import logging
from dataclasses import asdict

import httpx
import pytest

from app.db import SessionLocal
from app.models import SyncLink
from app.security import secrets
from app.services import project_sync_client
from app.services.project_sync_client import CONTRACT, MAX_ENTITY_BYTES
from tests.project_sync_fake import FakeConsole

CONSOLE = "https://console.example"
TOKEN = "gb_dep_PROJECT_SYNC_TOKEN_MUST_NOT_LEAK_7f3a"
SAME = "gb_dep_SAME_ENGINEER_RELINK_TOKEN_4b11"
OTHER = "gb_dep_OTHER_ENGINEER_TOKEN_9c2e"
LEGACY = "gb_sync_LEGACY_KEY_MUST_NOT_LEAK_88e1"

HASH = "sha256:abc123"


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def fake():
    console = FakeConsole()
    console.add_deployment(TOKEN, engineer="eng-ada", label="Ada")
    console.add_deployment(SAME, engineer="eng-ada", label="Ada")
    console.add_deployment(OTHER, engineer="eng-bea", label="Bea")
    console.add_deployment(LEGACY, engineer="eng-ada", label="Ada")
    return console


@pytest.fixture()
def linked(db):
    db.add(SyncLink(id="instance", console_url=CONSOLE, cloud_url=CONSOLE,
                    token_enc=secrets.encrypt(TOKEN)))
    db.commit()


def _retoken(db, token):
    row = db.get(SyncLink, "instance")
    row.token_enc = secrets.encrypt(token)
    row.legacy_key = ""
    db.commit()


def _entity(ident, *, body=None, type_="item", version=1):
    return {"type": type_, "id": ident, "version": version, "hash": HASH,
            "body": body if body is not None else {"title": ident}}


def _wire(fake):
    return fake.transport()


def test_get_contract_and_register_a_project(db, fake, linked):
    contract = project_sync_client.get_contract(db, transport=_wire(fake))
    assert contract.status == "ok" and contract.version == "1"
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    assert created.status == "ok" and created.adopted is False
    assert created.cloud_project_id and created.tag == "GRPH"
    sent = fake.requests
    assert sent[0]["headers"]["x-sync-contract"] == CONTRACT
    assert sent[0]["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert sent[1]["path"] == "/api/sync/projects"


def test_a_live_tag_is_refused_with_the_owner(db, fake, linked):
    project_sync_client.register_project(db, tag="GRPH", name="Graphban", transport=_wire(fake))
    _retoken(db, OTHER)
    taken = project_sync_client.register_project(
        db, tag="GRPH", name="Other", transport=_wire(fake))
    assert taken.status == "refused"
    assert taken.reason == "tag_taken" and taken.owner_label == "Ada"


def test_a_revoked_home_is_adopted_by_the_same_engineer_only(db, fake, linked):
    first = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    fake.revoke(TOKEN)
    _retoken(db, SAME)
    adopted = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    assert adopted.status == "ok" and adopted.adopted is True
    assert adopted.cloud_project_id == first.cloud_project_id
    _retoken(db, OTHER)
    taken = project_sync_client.register_project(
        db, tag="GRPH", name="Bea", transport=_wire(fake))
    assert taken.status == "refused" and taken.reason == "tag_taken"
    assert taken.owner_label == "Ada"


def test_every_entity_push_sends_max_delay_and_the_contract(db, fake, linked):
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    pushed = project_sync_client.push_entities(
        db, cloud_project_id=created.cloud_project_id, entities=[_entity("GRPH-1")],
        transport=_wire(fake))
    assert pushed.status == "ok"
    assert pushed.stored == [{"type": "item", "id": "GRPH-1", "version": 1}]
    assert pushed.refused == []
    call = fake.requests[-1]
    assert call["headers"]["x-sync-max-delay"] == "60"
    assert call["headers"]["x-sync-contract"] == "1"
    assert call["headers"]["x-cloud-project"] == created.cloud_project_id
    assert call["headers"]["content-encoding"] == "gzip"
    assert len(fake.entities(created.cloud_project_id)) == 1

    again = project_sync_client.push_entities(
        db, cloud_project_id=created.cloud_project_id, entities=[_entity("GRPH-1")],
        max_delay=15, transport=_wire(fake))
    assert again.status == "ok" and again.stored[0]["id"] == "GRPH-1"
    assert fake.requests[-1]["headers"]["x-sync-max-delay"] == "15"
    assert len(fake.entities(created.cloud_project_id)) == 1
    newer = project_sync_client.push_entities(
        db, cloud_project_id=created.cloud_project_id,
        entities=[_entity("GRPH-1", version=2)], transport=_wire(fake))
    assert newer.stored == [{"type": "item", "id": "GRPH-1", "version": 2}]
    # One row per entity. A greater version replaces the body; it does not
    # keep the previous version beside it.
    stored = fake.entities(created.cloud_project_id)
    assert len(stored) == 1 and stored[0]["version"] == 2


def test_a_version_above_the_safe_integer_is_invalid_and_a_lower_one_keeps_the_body(
        db, fake, linked):
    """The console refuses a non-safe-integer version, and an older or equal
    version is acknowledged without replacing the stored body.

    Deleting the range check in ``_one_entity`` stores 2^53. Replacing the body
    on a lower version fails the title assertion.
    """
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    cloud = created.cloud_project_id
    huge = _entity("GRPH-1", version=2**53, body={"title": "too big"})
    refused = project_sync_client.push_entities(
        db, cloud_project_id=cloud, entities=[huge], transport=_wire(fake))
    assert refused.status == "ok"
    assert refused.refused == [
        {"type": "item", "id": "GRPH-1", "version": 2**53, "reason": "invalid"}]
    assert refused.stored == []
    assert fake.entities(cloud) == []

    negative = _entity("GRPH-1", version=-1, body={"title": "negative"})
    refused_neg = project_sync_client.push_entities(
        db, cloud_project_id=cloud, entities=[negative], transport=_wire(fake))
    assert refused_neg.refused[0]["reason"] == "invalid"
    assert fake.entities(cloud) == []

    top = _entity("GRPH-1", version=2**53 - 1, body={"title": "current"})
    stored = project_sync_client.push_entities(
        db, cloud_project_id=cloud, entities=[top], transport=_wire(fake))
    assert stored.stored == [{"type": "item", "id": "GRPH-1", "version": 2**53 - 1}]

    older = _entity("GRPH-1", version=2, body={"title": "stale"})
    acknowledged = project_sync_client.push_entities(
        db, cloud_project_id=cloud, entities=[older], transport=_wire(fake))
    assert acknowledged.status == "ok" and acknowledged.refused == []
    assert acknowledged.stored == [{"type": "item", "id": "GRPH-1", "version": 2}]
    replica = fake.entities(cloud)
    assert len(replica) == 1
    assert replica[0]["version"] == 2**53 - 1
    assert replica[0]["body"] == {"title": "current"}

    same = _entity("GRPH-1", version=2**53 - 1, body={"title": "also stale"})
    replay = project_sync_client.push_entities(
        db, cloud_project_id=cloud, entities=[same], transport=_wire(fake))
    assert replay.stored[0]["version"] == 2**53 - 1
    assert fake.entities(cloud)[0]["body"] == {"title": "current"}


def test_an_over_cap_entity_is_refused_and_the_rest_are_stored(db, fake, linked):
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    small = _entity("GRPH-1", body={"t": "a"})
    large = _entity("GRPH-2", body={"t": "x" * 400})
    small_n = len(json.dumps(small, separators=(",", ":"), sort_keys=True).encode())
    large_n = len(json.dumps(large, separators=(",", ":"), sort_keys=True).encode())
    assert small_n < large_n
    # The cap sits on the small entity, so only the neighbour is over it.
    fake.max_entity_bytes = small_n
    pushed = project_sync_client.push_entities(
        db, cloud_project_id=created.cloud_project_id, entities=[large, small],
        transport=_wire(fake))
    # The call succeeded. The over-cap entity is parked; it does not refuse the batch.
    assert pushed.status == "ok"
    assert pushed.stored == [{"type": "item", "id": "GRPH-1", "version": 1}]
    assert pushed.refused == [
        {"type": "item", "id": "GRPH-2", "version": 1, "reason": "too_large"}]
    assert FakeConsole().max_entity_bytes == MAX_ENTITY_BYTES == 1024 * 1024


def test_a_forbidden_type_and_an_invalid_entity_are_named(db, fake, linked):
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    bad_type = _entity("GRPH-9", type_="secret")
    bad_hash = _entity("GRPH-8")
    bad_hash["hash"] = "md5:nope"
    good = _entity("GRPH-1")
    pushed = project_sync_client.push_entities(
        db, cloud_project_id=created.cloud_project_id,
        entities=[bad_type, bad_hash, good], transport=_wire(fake))
    assert pushed.status == "ok"
    assert {(row["id"], row["reason"]) for row in pushed.refused} == {
        ("GRPH-9", "forbidden_type"), ("GRPH-8", "invalid")}
    assert pushed.stored == [{"type": "item", "id": "GRPH-1", "version": 1}]


def test_not_home_and_an_unknown_contract_are_refusals(db, fake, linked, monkeypatch):
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    _retoken(db, OTHER)
    refused = project_sync_client.push_entities(
        db, cloud_project_id=created.cloud_project_id, entities=[_entity("GRPH-1")],
        transport=_wire(fake))
    assert refused.status == "refused" and refused.reason == "not_home"
    _retoken(db, TOKEN)
    monkeypatch.setattr(project_sync_client, "CONTRACT", "99")
    unknown = project_sync_client.get_contract(db, transport=_wire(fake))
    assert unknown.status == "refused"
    assert unknown.reason == "contract" and unknown.field == "X-Sync-Contract"


def test_a_timeout_is_unknown_and_not_refused(db, fake, linked):
    fake.timeout_once()
    result = project_sync_client.get_contract(db, transport=_wire(fake))
    assert result.status == "unknown"
    assert result.status != "refused"
    assert result.reason == "could not reach the console: ReadTimeout"
    fake.fail_once(httpx.ConnectError("connection refused"))
    down = project_sync_client.push_entities(
        db, cloud_project_id="cp1", entities=[_entity("GRPH-1")], transport=_wire(fake))
    assert down.status == "unknown" and "ConnectError" in down.reason


def test_a_429_returns_its_retry_after_seconds(db, fake, linked):
    fake.retry_after_once(30)
    result = project_sync_client.push_entities(
        db, cloud_project_id="cp1", entities=[_entity("GRPH-1")], transport=_wire(fake))
    assert result.status == "unknown"
    assert result.retry_after == 30
    assert result.status != "refused"


def test_a_5xx_is_unknown(db, fake, linked):
    fake.status_once(503)
    result = project_sync_client.get_contract(db, transport=_wire(fake))
    assert result.status == "unknown"
    assert result.reason == "the console answered 503"


def test_purge_deletes_and_closes_proposals_as_project_unsynced(db, fake, linked):
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    project_sync_client.push_entities(
        db, cloud_project_id=created.cloud_project_id,
        entities=[_entity("GRPH-1"), _entity("GRPH-2")], transport=_wire(fake))
    proposal = fake.enqueue_proposal(
        created.cloud_project_id, kind="comment", target={"type": "item", "id": "GRPH-1"},
        base_version=1, payload={"text": "hi"}, proposer={"label": "Ada", "role": "member"})
    purged = project_sync_client.purge_entities(
        db, project=created.cloud_project_id, transport=_wire(fake))
    assert purged.status == "ok" and purged.deleted == 2
    assert fake.entities(created.cloud_project_id) == []
    assert fake.outcome_for(proposal["id"]) == {"status": "refused", "reason": "project unsynced"}
    assert fake.requests[-1]["params"]["project"] == created.cloud_project_id
    assert fake.requests[-1]["headers"]["x-cloud-project"] == created.cloud_project_id


def test_proposal_get_is_304_when_the_etag_matches(db, fake, linked):
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    proposal = fake.enqueue_proposal(
        created.cloud_project_id, kind="edit", target={"type": "item", "id": "GRPH-1"},
        base_version=3, payload={"title": "now"}, proposer={"label": "Ada", "role": "member"})
    page = project_sync_client.pull_proposals(
        db, project=created.cloud_project_id, transport=_wire(fake))
    assert page.status == "ok" and page.not_modified is False
    assert page.etag and page.cursor
    assert page.proposals[0]["id"] == proposal["id"]
    assert page.proposals[0]["kind"] == "edit"
    assert page.proposals[0]["target"] == {"type": "item", "id": "GRPH-1"}
    idle = project_sync_client.pull_proposals(
        db, project=created.cloud_project_id, after=page.cursor, etag=page.etag,
        transport=_wire(fake))
    assert fake.last_status == 304
    assert idle.status == "ok" and idle.not_modified is True
    assert idle.proposals == [] and idle.etag == page.etag
    fake.enqueue_proposal(
        created.cloud_project_id, kind="create", target=None, base_version=None,
        payload={"title": "new"}, proposer={"label": "Bea", "role": "admin"})
    nxt = project_sync_client.pull_proposals(
        db, project=created.cloud_project_id, after=page.cursor, transport=_wire(fake))
    assert [row["id"] for row in nxt.proposals] == ["pr2"]
    assert nxt.proposals[0]["target"] is None


def test_the_same_outcome_is_a_no_op_and_a_different_one_is_already_resolved(db, fake, linked):
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    proposal = fake.enqueue_proposal(
        created.cloud_project_id, kind="answer", target={"type": "wait", "id": "W-1"},
        base_version=1, payload={"answer": "yes"}, proposer={"label": "Ada", "role": "member"})
    first = project_sync_client.report_outcomes(
        db, outcomes=[{"id": proposal["id"], "status": "applied"}], transport=_wire(fake))
    assert first.status == "ok"
    again = project_sync_client.report_outcomes(
        db, outcomes=[{"id": proposal["id"], "status": "applied"}], transport=_wire(fake))
    assert again.status == "ok"
    changed = project_sync_client.report_outcomes(
        db, outcomes=[{"id": proposal["id"], "status": "conflict", "current_version": 4}],
        transport=_wire(fake))
    assert changed.status == "refused" and changed.reason == "already_resolved"
    assert fake.outcome_for(proposal["id"])["status"] == "applied"


def test_revoking_a_deployment_closes_proposals_as_box_unlinked(db, fake, linked):
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    proposal = fake.enqueue_proposal(
        created.cloud_project_id, kind="comment", target=None, base_version=None,
        payload={}, proposer={"label": "Ada", "role": "member"})
    fake.revoke(TOKEN)
    assert fake.outcome_for(proposal["id"]) == {"status": "refused", "reason": "box unlinked"}


def test_the_token_is_not_in_any_result_or_log(db, fake, linked, caplog):
    caplog.set_level(logging.DEBUG)
    created = project_sync_client.register_project(
        db, tag="GRPH", name="Graphban", transport=_wire(fake))
    fake.timeout_once()
    timed = project_sync_client.get_contract(db, transport=_wire(fake))
    fake.retry_after_once(12)
    limited = project_sync_client.pull_proposals(
        db, project=created.cloud_project_id, transport=_wire(fake))
    _retoken(db, OTHER)
    taken = project_sync_client.register_project(
        db, tag="GRPH", name="Other", transport=_wire(fake))
    blob = caplog.text + "\n".join(json.dumps(asdict(row)) for row in (created, timed, limited, taken))
    assert TOKEN not in blob
    assert SAME not in blob and OTHER not in blob
    assert timed.status == "unknown" and limited.retry_after == 12
    assert taken.reason == "tag_taken"


def test_a_legacy_key_is_sent_as_x_api_key_and_not_returned(db, fake, linked):
    row = db.get(SyncLink, "instance")
    row.token_enc = ""
    row.legacy_key = secrets.encrypt(LEGACY)
    db.commit()
    result = project_sync_client.get_contract(db, transport=_wire(fake))
    assert result.status == "ok" and result.version == "1"
    headers = fake.requests[-1]["headers"]
    assert headers.get("x-api-key") == LEGACY
    assert "authorization" not in headers
    assert LEGACY not in json.dumps(asdict(result))


def test_not_linked_is_unknown(db, fake):
    result = project_sync_client.get_contract(db, transport=_wire(fake))
    assert result.status == "unknown" and result.reason == "not_linked"
    assert fake.requests == []
