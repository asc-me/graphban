"""GRPH-1734: the sync payload is an allow-list, and the seeded secrets stay off it.

Replacing ``_item_body``'s ``allow(...)`` with ``items_svc.item_dict(item)``
fails ``test_backfill_bytes_contain_none_of_the_seeded_secrets``. ``item_dict``
renders ``assignee`` and ``claimed_by``, and the fixture puts the box email
and the seat id there.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import (
    ApiKey, AssistantMessage, AssistantThread, CodeNode, Credential, Enrolment,
    Event, GrillTurn, Link, MemoryShard, RequestComment, User,
)
from app.services import items as items_svc
from app.services import prds as prds_svc
from app.services import project_sync_outbox as outbox
from app.services import project_sync_payload as payload
from app.services import projects as projects_svc
from app.services import requests as requests_svc

API_KEY = "gb_sk_P89_SYNC_PAYLOAD_KEY_MUST_NOT_LEAK"
ENROL_CODE = "WORKER-P89LEAKCODE"
SEAT_ID = "seat_p89_must_not_leak"
EMAIL = "box-user-p89-must-not-leak@example.test"
USER_ID = "user_p89_box_id_must_not_leak"
TRANSCRIPT = "ASSISTANT_TRANSCRIPT_p89_must_not_leak"
GRILL_TEXT = "GRILL_TRANSCRIPT_p89_must_not_leak"
SHARD_TEXT = "MEMORY_SHARD_p89_must_not_leak"
CODE_TEXT = "CODE_NODE_p89_must_not_leak"
BOX_SETTING = "BOX_SETTING_p89_must_not_leak"
EMBED = 0.424242424242

# Pinned here, not only read back from the constant, so editing the constant
# to match a wider body fails this file rather than moving both together.
ITEM_KEYS = (
    "title", "description", "acceptance", "evidence", "blocker", "links",
    "tags", "touchpoints", "status", "kind", "prd_id", "line_id", "status_history",
)
PRD_KEYS = (
    "body", "sections", "grill_state", "baseline_hash", "version", "coverage",
)
NOTE_KEYS = ("text", "answer", "timestamps", "actor")


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _secrets() -> tuple[str, ...]:
    vector = json.dumps([EMBED])
    return (
        API_KEY, ENROL_CODE, SEAT_ID, EMAIL, USER_ID, TRANSCRIPT, GRILL_TEXT,
        SHARD_TEXT, CODE_TEXT, BOX_SETTING, vector,
    )


def _world(db):
    db.add(User(
        id=USER_ID, name="Box User", handle="boxuser", email=EMAIL, password_hash="x",
    ))
    db.commit()
    project = projects_svc.create_project(
        db, name="Payload", owner_user_id=USER_ID, tag="PAYL",
    )
    project.setup_decisions = {"note": BOX_SETTING}
    db.commit()

    item = items_svc.create_item(
        db, title="Allow listed work", project_id=project.id,
        description="The work.\n\n## Acceptance\n- the payload is allow-listed\n",
        tags=["sync"], touchpoints=["backend/app/services/project_sync_payload.py"],
        status="next",
    )
    item.assignee = EMAIL
    item.claimed_by = SEAT_ID
    db.commit()

    wait = items_svc.create_item(
        db, title="Need a decision", project_id=project.id, status="blocked",
        tags=["wait:decision"], question="Ship it?", options=["yes", "no"],
    )
    wait.decision = {
        "question": "Ship it?",
        "options": ["yes", "no"],
        "answer": {
            "choice": "yes", "text": "ship the allow-list",
            "by": {"user": "boxuser", "agent": SEAT_ID}, "at": 1700000000,
        },
        "notify": [SEAT_ID],
    }
    db.commit()
    db.add(Link(
        project_id=project.id, a=item.id, b=wait.id, type="dependency",
        reason="the decision gates the work",
    ))
    db.add(Event(
        actor_type="user", actor_id=USER_ID, actor_label=EMAIL, surface="rest",
        action="update_item", target_type="item", target_id=item.id,
        project_id=project.id, meta={"status": "next", "token": API_KEY},
    ))
    db.add(ApiKey(
        id="k_p89_payload", user_id=USER_ID, project_id=project.id, name="agent",
        prefix=API_KEY, hashed_key=API_KEY, scopes=["read", "write"],
    ))
    db.add(Enrolment(
        id=SEAT_ID, project_id=project.id, code_hash="not-the-code",
        code_prefix=ENROL_CODE, role="worker",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    ))
    db.add(Credential(
        id="cred_p89_payload", kind="openai", label="box", api_key=BOX_SETTING,
    ))
    vector = [0.0] * settings.embed_dim
    vector[0] = EMBED
    db.add(MemoryShard(
        id="mem_p89_payload", project_id=project.id, item_id=item.id,
        text=SHARD_TEXT, embedding=vector,
    ))
    db.add(CodeNode(
        id="cn_p89_payload", project_id=project.id, path="secret.py",
        summary=CODE_TEXT, embedding=vector,
    ))
    db.add(AssistantThread(
        id="th_p89_payload", project_id=project.id, entity_type="item",
        entity_id=item.id, title="transcript",
    ))
    db.add(AssistantMessage(
        id="msg_p89_payload", thread_id="th_p89_payload", seq=1, role="assistant",
        content=TRANSCRIPT,
    ))
    prd = prds_svc.create_prd(
        db, title="Sync", project_id=project.id, created_by=USER_ID,
        body="## What syncs\n\nItems leave by allow-list.\n",
    )
    db.add(GrillTurn(
        prd_id=prd.id, seq=1, role="user", text=GRILL_TEXT, actor=EMAIL,
    ))
    req = requests_svc.create_request(
        db, type_="bug", title="a bug", project_id=project.id, by="boxuser",
    )
    db.add(RequestComment(
        id="rcom_p89_payload", request_id=req.id, author_user_id=USER_ID,
        author_kind="operator", body="looks wrong",
    ))
    db.commit()
    return {
        "project": project, "item": item, "wait": wait, "prd": prd, "vector": vector,
    }


def test_backfill_bytes_contain_none_of_the_seeded_secrets(db):
    world = _world(db)
    raw = payload.backfill_bytes(db, world["project"].id)
    assert raw is not None
    text = raw.decode("utf-8")
    for secret in _secrets():
        assert secret not in text, secret
    # The work itself is on the wire, so an empty payload cannot pass by omission.
    assert "Allow listed work" in text
    assert "the payload is allow-listed" in text
    assert "looks wrong" in text
    assert "ship the allow-list" in text
    assert "boxuser" in text
    assert "What syncs" in text


def test_item_dict_renders_the_secrets_the_allow_list_drops(db):
    """The sabotage premise: ``item_dict`` as-is contains the seeded email and seat."""
    world = _world(db)
    rendered = json.dumps(items_svc.item_dict(world["item"]))
    assert EMAIL in rendered
    assert SEAT_ID in rendered


def test_body_keys_equal_the_allow_list_and_a_model_field_does_not_join_it(db):
    world = _world(db)
    project_id = world["project"].id
    rows = payload.backfill(db, project_id)
    by_type = {}
    for row in rows:
        by_type.setdefault(row["type"], row)
    assert set(by_type) == {"item", "prd", "comment", "wait"}
    expect = {"item": ITEM_KEYS, "prd": PRD_KEYS, "comment": NOTE_KEYS, "wait": NOTE_KEYS}
    for type_, keys in expect.items():
        assert payload.BODY_ALLOW[type_] == keys
        assert set(by_type[type_]["body"]) == set(keys)
    item_body = by_type["item"]["body"]
    assert "assignee" not in item_body
    assert "claimed_by" not in item_body
    assert "built_by" not in item_body
    # Two items. The work item is the one whose title we set; either order is
    # by number, and both must be cut to the same keys.
    item_rows = [row for row in rows if row["type"] == "item"]
    assert len(item_rows) == 2
    for row in item_rows:
        assert set(row["body"]) == set(ITEM_KEYS)
        assert "assignee" not in row["body"]


def test_same_content_hashes_the_same_and_one_field_change_does_not(db):
    world = _world(db)
    item = world["item"]
    first = payload.entity(db, "item", item.key)
    second = payload.entity(db, "item", item.key)
    assert first is not None and second is not None
    assert first["hash"] == second["hash"]
    assert first["hash"] == payload.content_hash(first["body"])
    assert first["hash"].startswith("sha256:")
    assert first["version"] == second["version"]
    assert isinstance(first["version"], int) and not isinstance(first["version"], bool)
    assert 0 <= first["version"] <= outbox.MAX_SAFE_VERSION

    item.description = item.description + "\nOne more sentence.\n"
    db.commit()
    third = payload.entity(db, "item", item.key)
    assert third is not None
    assert third["hash"] != first["hash"]
    # The version does not follow the body hash. Nothing has captured this edit,
    # so it is still the same safe integer. Restoring the 63-bit hash makes this
    # differ and puts it above 2^53-1.
    assert third["version"] == first["version"]
    assert "One more sentence." in third["body"]["description"]


def _legacy_hash_version(body: dict) -> int:
    """The pre-GRPH-1815 wire version: 63 bits of the body's sha256."""
    digest = hashlib.sha256(payload.canonical(body).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def test_a_large_body_and_a_hash_past_2_to_the_53_still_emit_a_safe_version(db):
    """Every version the payload builder emits is in 0..2^53-1.

    The first body is large. The loop keeps editing until the old hash version
    would have been above 2^53-1, which is what the console refuses as invalid.
    """
    world = _world(db)
    item = world["item"]
    item.description = "x" * 100_000
    db.commit()
    large = payload.entity(db, "item", item.key)
    assert large is not None and len(large["body"]["description"]) == 100_000
    assert 0 <= large["version"] <= outbox.MAX_SAFE_VERSION

    exceeded = False
    for n in range(64):
        item.description = f"hash-probe-{n}-" + ("y" * 32)
        db.commit()
        record = payload.entity(db, "item", item.key)
        assert record is not None
        assert isinstance(record["version"], int) and not isinstance(record["version"], bool)
        assert 0 <= record["version"] <= outbox.MAX_SAFE_VERSION
        if _legacy_hash_version(record["body"]) > outbox.MAX_SAFE_VERSION:
            exceeded = True
            break
    assert exceeded


def test_every_backfill_version_is_inside_the_safe_integer_range(db):
    world = _world(db)
    rows = payload.backfill(db, world["project"].id)
    assert rows
    for row in rows:
        assert isinstance(row["version"], int) and not isinstance(row["version"], bool)
        assert 0 <= row["version"] <= outbox.MAX_SAFE_VERSION


def test_canonical_hash_ignores_key_order(db):
    body = {"b": 1, "a": {"d": 2, "c": 3}}
    flipped = {"a": {"c": 3, "d": 2}, "b": 1}
    assert payload.content_hash(body) == payload.content_hash(flipped)
    assert payload.canonical(body) == payload.canonical(flipped)


def test_a_missing_project_is_not_an_empty_backfill(db):
    assert payload.backfill(db, "no-such-project") is None
    assert payload.backfill_bytes(db, "no-such-project") is None
    project = projects_svc.create_project(
        db, name="Empty", owner_user_id=_user(db), tag="EMPT",
    )
    assert payload.backfill(db, project.id) == []
    assert payload.entity(db, "item", "EMPT-1") is None
    assert payload.entity(db, "wait", "EMPT-1") is None


def test_status_history_is_the_label_and_not_the_event_meta(db):
    world = _world(db)
    record = payload.entity(db, "item", world["item"].key)
    history = record["body"]["status_history"]
    assert len(history) == 1
    assert set(history[0]) == {"at", "status", "actor"}
    assert history[0]["status"] == "next"
    assert history[0]["actor"] == "boxuser"
    assert API_KEY not in json.dumps(history)
    assert USER_ID not in json.dumps(history)


def test_grill_state_counts_the_turn_and_does_not_carry_its_text(db):
    world = _world(db)
    record = payload.entity(db, "prd", world["prd"].key)
    state = record["body"]["grill_state"]
    assert state["answers"] == 1
    assert state["grilled"] is True
    assert GRILL_TEXT not in json.dumps(state)
    assert "turns" not in state
    # No baseline yet: None, not a hash of the empty string.
    assert record["body"]["baseline_hash"] is None
    assert record["body"]["sections"] == ["What syncs"]


def test_a_wait_answer_keeps_the_words_and_drops_the_seat(db):
    world = _world(db)
    record = payload.entity(db, "wait", world["wait"].key)
    assert record["body"]["text"] == "Ship it?"
    assert record["body"]["answer"] == "yes: ship the allow-list"
    assert record["body"]["actor"] == "boxuser"
    assert SEAT_ID not in json.dumps(record)
    assert record["id"] == world["wait"].key


def test_unknown_type_is_refused():
    with pytest.raises(ValueError):
        payload.entity(None, "secret", "x")


def _user(db) -> str:
    db.add(User(
        id="u_empty_payload", name="Empty", handle="emptyuser",
        email="empty-payload@example.test", password_hash="x",
    ))
    db.commit()
    return "u_empty_payload"
