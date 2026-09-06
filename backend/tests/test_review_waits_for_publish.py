"""GRPH-754 — an item is not reviewable until its work is reachable.

Measured on the deployed instance, twice: an item goes to `review` when the child says so, and
its branch is published when the supervisor reaps the child a few seconds later. A reviewer
handed the item inside that window fetches a 404 and bounces work that is fine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import AttemptTelemetry, Item
from app.services import fleet as fleet_svc
from app.services import harness as hsvc
from app.services import items as items_svc


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Publish"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label, **kw}))["agent_id"]


def _in_review(client, key, db, *, supervised: bool, published: bool = False) -> str:
    """An item sitting in review, with or without a supervisor's launch post behind it."""
    who = _agent(client, key, "worker", worktree="/w/one", branch="gb/wave-1",
                 capabilities={"instance": "builder"})
    item = _ok(_mcp(client, key, "create_item", {
        "title": "work", "status": "next", "touchpoints": ["backend/app/x.py"]}))["id"]
    assert items_svc.claim_item(db, item, who) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": who}))
    if supervised:
        db.add(AttemptTelemetry(
            id=f"at_{item}", item_id=item, project_id=db.get(Item, item).project_id,
            chosen_source="matrix", chosen_winner="gbagent:qwen3.6",
            branch_published_at=hsvc.published_now() if published else None))
        db.commit()
    return item


def _reviewer(client, key) -> str:
    return _agent(client, key, "reviewer", capabilities={"instance": "rev"})


def test_a_supervised_item_is_withheld_until_its_branch_is_published(client, key, db, proj):
    """The finding, as a test. Sabotage: drop the `publish_pending` clause and the reviewer is
    handed an item whose branch it will 404 on."""
    item = _in_review(client, key, db, supervised=True)
    rev = _reviewer(client, key)
    assert fleet_svc.claim_review(db, agent_id=rev, project_id=proj) is None

    # The supervisor reports the push; now it is reviewable.
    row = db.scalar(select(AttemptTelemetry).where(AttemptTelemetry.item_id == item))
    row.branch_published_at = hsvc.published_now()
    db.commit()
    got = fleet_svc.claim_review(db, agent_id=rev, project_id=proj)
    assert got is not None and got.id == item


def test_an_unsupervised_item_is_never_withheld(client, key, db, proj):
    """A human's branch, or an agent running standalone: nothing will ever arrive to release
    it, so withholding would hide the work forever. Sabotage: gate on the null alone and this
    item never becomes reviewable."""
    item = _in_review(client, key, db, supervised=False)
    got = fleet_svc.claim_review(db, agent_id=_reviewer(client, key), project_id=proj)
    assert got is not None and got.id == item


def test_the_wait_expires_so_a_dead_supervisor_cannot_hide_the_work(client, key, db, proj):
    """Never hiding work outranks never showing an unpublished branch."""
    item = _in_review(client, key, db, supervised=True)
    stored = db.get(Item, item)
    stored.updated_at = datetime.now(timezone.utc) - timedelta(
        seconds=hsvc.PUBLISH_GRACE_SECONDS + 5)
    db.commit()
    got = fleet_svc.claim_review(db, agent_id=_reviewer(client, key), project_id=proj)
    assert got is not None and got.id == item


def test_an_item_with_no_branch_is_not_withheld(client, key, db, proj):
    """The gate is about an unreadable branch, not about having none."""
    item = _in_review(client, key, db, supervised=True)
    stored = db.get(Item, item)
    stored.branch = ""
    db.commit()
    got = fleet_svc.claim_review(db, agent_id=_reviewer(client, key), project_id=proj)
    assert got is not None and got.id == item


def test_the_supervisor_reports_the_publish_through_the_attempts_route(client, key, db, proj):
    """The other half: the report has to reach the server, and only a real push may set it."""
    item = _in_review(client, key, db, supervised=True)
    row = db.scalar(select(AttemptTelemetry).where(AttemptTelemetry.item_id == item))
    # Addressed by delegation id, one of the two shapes a supervisor posts with.
    row.delegation_id = "dlg_pub"
    db.commit()

    from app.models import Delegation
    planner = _agent(client, key, "planner")
    db.add(Delegation(id="dlg_pub", project_id=proj, item_id=item, delegated_by=planner,
                      lane="backend", requested_tier="cheap"))
    db.commit()

    r = client.post("/api/fleet/attempts", headers={"X-API-Key": key},
                    json={"delegation_id": "dlg_pub", "branch_published": True})
    assert r.status_code in (200, 202), r.text
    assert r.json()["branch_published"] is True
    db.expire_all()
    assert db.scalar(select(AttemptTelemetry)
                     .where(AttemptTelemetry.item_id == item)).branch_published_at is not None


def test_a_post_that_does_not_claim_a_publish_leaves_it_unset(client, key, db, proj):
    """Sabotage: set the timestamp on every exit report and the window re-opens — a branch
    that skipped or was refused would be announced as readable."""
    item = _in_review(client, key, db, supervised=True)
    row = db.scalar(select(AttemptTelemetry).where(AttemptTelemetry.item_id == item))
    row.delegation_id = "dlg_quiet"
    db.commit()
    from app.models import Delegation
    planner = _agent(client, key, "planner")
    db.add(Delegation(id="dlg_quiet", project_id=proj, item_id=item, delegated_by=planner,
                      lane="backend", requested_tier="cheap"))
    db.commit()

    r = client.post("/api/fleet/attempts", headers={"X-API-Key": key},
                    json={"delegation_id": "dlg_quiet", "wall_seconds": 12})
    assert r.status_code in (200, 202), r.text
    assert r.json()["branch_published"] is False
    db.expire_all()
    assert db.scalar(select(AttemptTelemetry)
                     .where(AttemptTelemetry.item_id == item)).branch_published_at is None
