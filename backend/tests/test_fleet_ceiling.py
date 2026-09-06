"""S4 — the ceiling proof (D-c): `done` is unreachable except through `sign_off`.

After S3 merged `reviewer` into `worker`, every specialised agent is under the ceiling.
Before the merge a stored ``reviewer`` had a path to ``done`` through ``update_item`` that
never evaluated ``built_by``; after the merge the resolution at ``eligible_roles`` and
``active_role`` feeds the ceiling's ``role == "worker"`` check, so the escape is closed.

This file proves it with three independent sabotage paths — each deletion fails a
*different* test.  If any two deletions fail the same test, the ban has one site where it
claims three, and that is the finding.

1. Delete the ceiling branch in ``check_tool_role`` → test 1 fails.
2. Delete the ``sign_off`` authorship assert → test 2 fails.
3. Delete the S2 ``_resolve_stored_role`` comparison → test 3 fails.

Acceptance: §7.3, §7.4 (the `done` half).
"""
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Agent, ApiKey, Enrolment, Item
from app.services import fleet


# ---- helpers ------------------------------------------------------------------------------

def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _ok(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


def _refused(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert res.get("isError") is True, res
    return res["structuredContent"]["error"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Ceiling"},
                       headers=auth).json()["id"]


@pytest.fixture()
def agent_key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "ceiling", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _register(client, key, role="worker", label=None, instance=None):
    label = label or f"{role}-term"
    caps = {"instance": instance or label}
    return _ok(client, key, "register_agent",
               {"label": label, "role_hint": role, "capabilities": caps})


def _built_by(client, key, agent, title="work"):
    """An item claimed and pushed to `review` by `agent` — the state sign_off acts on."""
    _ok(client, key, "create_item", {"title": title, "status": "next"})
    c = _ok(client, key, "claim_next", {"agent_id": agent["agent_id"]})
    assert c["claimed"], "the fixture item should have been claimable"
    _ok(client, key, "update_item",
        {"id": c["item"]["id"], "status": "review", "agent_id": agent["agent_id"]})
    return c["item"]["id"]


# ---- sabotage 1: the ceiling branch -------------------------------------------------------

def test_a_worker_cannot_write_done_via_update_item(client, agent_key):
    """The ceiling: a worker may move work as far as `review` and no further.

    Deleting the ``if tool == "update_item" and role == "worker"`` branch in
    ``check_tool_role`` makes this test fail — the refusal disappears.
    """
    me = _register(client, agent_key, "worker")
    _ok(client, agent_key, "create_item", {"title": "work", "status": "next"})
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]
    _ok(client, agent_key, "update_item",
        {"id": item_key, "status": "review", "agent_id": me["agent_id"]})

    err = _refused(client, agent_key, "update_item",
                   {"id": item_key, "status": "done", "agent_id": me["agent_id"]})

    assert err["code"] == "unauthorized"
    assert "worker" in err["message"]
    after = _ok(client, agent_key, "get_item_details", {"id": item_key})
    assert after["status"] == "review", "the item did not move"


def test_a_worker_cannot_write_done_via_release_item(client, agent_key):
    """The second door: `release_item` shares the ceiling.  A worker releasing to `done`
    undoes the `update_item` ceiling entirely — `reviewed_by` stays None."""
    me = _register(client, agent_key, "worker")
    _ok(client, agent_key, "create_item", {"title": "work", "status": "next"})
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]
    _ok(client, agent_key, "update_item",
        {"id": item_key, "status": "review", "agent_id": me["agent_id"]})

    err = _refused(client, agent_key, "release_item",
                   {"id": item_key, "to_status": "done", "agent_id": me["agent_id"]})

    assert err["code"] == "unauthorized"
    assert "worker" in err["message"]
    after = _ok(client, agent_key, "get_item_details", {"id": item_key})
    assert after["status"] == "review", "the item did not move"


# ---- sabotage 2: the sign_off authorship assert -------------------------------------------

def test_an_agent_cannot_sign_off_its_own_work(client, agent_key):
    """The second of two independent gates.  Even if the ceiling were bypassed (all-in-one),
    ``sign_off`` still refuses when ``built_by == agent_id``.

    Deleting the ``if item.built_by and item.built_by == agent_id and not danger`` branch
    in ``sign_off`` makes this test fail — the SelfReview refusal disappears.
    """
    me = _register(client, agent_key, "worker")
    item_id = _built_by(client, agent_key, me)

    res = _rpc(client, agent_key, "sign_off",
               {"id": item_id, "agent_id": me["agent_id"]})

    assert res.get("isError") is True, res
    err = res["structuredContent"]["error"]
    assert err["code"] == "unauthorized"
    assert "built" in err["message"] or "sign" in err["message"].lower()
    after = _ok(client, agent_key, "get_item_details", {"id": item_id})
    assert after["status"] == "review", "the item did not move"


# ---- sabotage 3: the S2 resolution --------------------------------------------------------

def test_a_legacy_reviewer_key_resolves_to_worker(client, auth, proj, db):
    """A key stored as ``["reviewer"]`` resolves to exactly ``("worker",)``.

    Deleting the ``if role == "reviewer": return "worker"`` branch in
    ``_resolve_stored_role`` makes this test fail — the key keeps "reviewer" and the
    ceiling's ``role == "worker"`` check never catches it.
    """
    from app.models import ApiKey

    row, plaintext = client.post(
        "/api/api-keys", json={"name": "legacy-rev", "project_id": proj},
        headers=auth), None
    plaintext = row.json()["plaintext"]
    key_row = db.query(ApiKey).filter(ApiKey.name == "legacy-rev").first()
    key_row.roles = ["reviewer"]
    db.commit()

    resolved = fleet.eligible_roles(key_row)
    assert resolved == ("worker",), f"expected exactly ('worker',), got {resolved}"


def test_a_legacy_reviewer_seat_is_caught_by_the_ceiling(client, auth, proj, db):
    """The integration path: a seat stored as `reviewer` resolves to `worker` at register,
    and the ceiling then refuses `done`.  This is the transitive proof — test 3a proves
    the resolution, test 1 proves the ceiling catches workers; together they prove the
    merged agent cannot reach `done` through `update_item`.

    Deleting ``_resolve_stored_role`` makes this fail: the agent registers as `reviewer`,
    the ceiling checks `role == "worker"`, and `reviewer` does not match.
    """
    from app.services.fleet import _hash_code

    plaintext = client.post(
        "/api/api-keys", json={"name": "legacy-seat", "project_id": proj},
        headers=auth).json()["plaintext"]

    body = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    code = f"REVIEWER-{body}"
    db.add(Enrolment(
        id="test-s4-seat", project_id=proj, code_hash=_hash_code(code),
        code_prefix=body[:2], role="reviewer", wave="w1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30)))
    db.commit()

    me = _ok(client, plaintext, "register_agent",
             {"label": "pre-merge-s4", "enrolment_code": code})
    assert me["active_role"] == "worker", (
        f"legacy reviewer should resolve to worker, got {me['active_role']}")

    _ok(client, plaintext, "create_item", {"title": "work", "status": "next"})
    claimed = _ok(client, plaintext, "claim_next", {"agent_id": me["agent_id"]})
    item_key = claimed["item"]["id"]
    _ok(client, plaintext, "update_item",
        {"id": item_key, "status": "review", "agent_id": me["agent_id"]})

    err = _refused(client, plaintext, "update_item",
                   {"id": item_key, "status": "done", "agent_id": me["agent_id"]})

    assert err["code"] == "unauthorized"
    assert "worker" in err["message"]


# ---- controls: the tests are not over-fitted ----------------------------------------------

def test_a_worker_may_still_move_work_to_review(client, agent_key):
    """The ceiling is a ceiling, not a ban.  A worker that cannot move work at all would
    be stranded — the gate must refuse `done` while allowing `review`."""
    me = _register(client, agent_key, "worker")
    _ok(client, agent_key, "create_item", {"title": "work", "status": "next"})
    claimed = _ok(client, agent_key, "claim_next", {"agent_id": me["agent_id"]})

    moved = _ok(client, agent_key, "update_item",
                {"id": claimed["item"]["id"], "status": "review", "agent_id": me["agent_id"]})

    assert moved["status"] == "review"


def test_a_second_agent_can_sign_off_the_firsts_work(client, agent_key, auth, proj):
    """The sign_off assert blocks SELF-review, not review itself.  A test that refused
    every sign_off would be the same defect wearing the fix's clothes."""
    from app.services.fleet import _hash_code

    a = _register(client, agent_key, "worker", label="author", instance="author-inst")
    item_id = _built_by(client, agent_key, a)

    second_key = client.post(
        "/api/api-keys", json={"name": "reviewer2", "project_id": proj},
        headers=auth).json()["plaintext"]
    b = _register(client, second_key, "worker", label="reviewer", instance="reviewer-inst")

    _ok(client, second_key, "claim_review", {"agent_id": b["agent_id"]})
    done = _ok(client, second_key, "sign_off",
               {"id": item_id, "agent_id": b["agent_id"]})
    assert done["status"] == "done"


def test_an_all_in_one_agent_can_write_done(client, auth, proj):
    """An all-in-one agent is unrestricted by the ceiling — the solo posture must still
    work.  The sign_off assert still applies, but writing `done` on your own item via
    `update_item` is allowed (the credential is `*`)."""
    key = client.post(
        "/api/api-keys", json={"name": "solo", "project_id": proj,
                               "scopes": ["read", "write", "gate"]},
        headers=auth).json()["plaintext"]
    _ok(client, key, "create_item", {"title": "work", "status": "next"})
    claimed = _ok(client, key, "claim_next", {})
    item_key = claimed["item"]["id"]
    _ok(client, key, "update_item",
        {"id": item_key, "status": "review"})

    from tests import attest
    moved = _ok(client, key, "update_item",
                {"id": item_key, **attest.complete_body()})
    assert moved["status"] == "done"
