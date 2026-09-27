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
    return _ok(_mcp(client, key, "register_agent", {"branch": "gb/test", "label": label, **kw}))["agent_id"]


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


def test_the_wait_does_not_expire_while_nothing_was_published(client, key, db, proj):
    """GRPH-946 reversed the old grace period. SA-575 reached a reviewer before its branch was
    pushed and was bounced for "no remote branch"; its real defects went unreviewed. Past any
    clock, an unpublished branch is still unreadable."""
    item = _in_review(client, key, db, supervised=True)
    stored = db.get(Item, item)
    stored.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()
    assert hsvc.publish_pending(db, stored) is True
    assert fleet_svc.claim_review(db, agent_id=_reviewer(client, key), project_id=proj) is None


def test_leaving_review_releases_the_wait(client, key, db, proj):
    """The other release: a bounce or reclaim puts the item back to work, and a supervisor that
    died no longer holds it."""
    item = _in_review(client, key, db, supervised=True)
    stored = db.get(Item, item)
    stored.status = "in_progress"
    db.commit()
    assert hsvc.publish_pending(db, stored) is False


def test_an_item_with_no_branch_is_withheld(client, key, db, proj):
    """GRPH-946. An empty branch is the LEAST readable case, not one to wave through.
    Sabotage: restore `if not item.branch: return False` in `publish_pending` and this fails."""
    item = _in_review(client, key, db, supervised=True)
    stored = db.get(Item, item)
    stored.branch = ""
    db.commit()
    assert hsvc.publish_pending(db, stored) is True


def test_claim_review_never_offers_an_item_with_nothing_to_read(client, key, db, proj):
    """No branch, no PR url, and no supervisor coming: nothing will ever make it readable, so
    it is not offered. Sabotage: drop the `reviewable_handoff` clause in `claim_review`."""
    item = _in_review(client, key, db, supervised=False)
    stored = db.get(Item, item)
    stored.branch = ""
    db.commit()
    rev = _reviewer(client, key)
    assert fleet_svc.claim_review(db, agent_id=rev, project_id=proj) is None

    # A PR url is the other thing a reviewer can read.
    stored.evidence = [{"kind": "url", "url": "https://github.com/o/r/pull/9", "detail": "PR"}]
    db.commit()
    got = fleet_svc.claim_review(db, agent_id=rev, project_id=proj)
    assert got is not None and got.id == item


# ---- the gate on the way in (GRPH-946) --------------------------------------------------------

def _refused(res) -> str:
    assert res.get("isError"), res
    return res["content"][0]["text"]


def test_review_is_refused_with_no_branch_and_no_pr_url(client, key, db, proj):
    """The refusal names both missing fields. Sabotage: drop the `reviewable_handoff` clause in
    `update_item` and the item reaches review with nothing to read."""
    item = _ok(_mcp(client, key, "create_item", {"title": "inline", "status": "in_progress"}))["id"]
    text = _refused(_mcp(client, key, "update_item", {"id": item, "status": "review"}))
    assert "`branch`" in text and "PR url" in text
    db.expire_all()
    assert db.get(Item, items_svc.keys.resolve_item(db, item)).status == "in_progress"


def test_a_pr_url_in_the_same_call_satisfies_the_gate(client, key, db, proj):
    item = _ok(_mcp(client, key, "create_item", {"title": "inline", "status": "in_progress"}))["id"]
    _ok(_mcp(client, key, "update_item", {
        "id": item, "status": "review",
        "evidence": [{"kind": "url", "url": "https://github.com/o/r/pull/3", "detail": "PR"}]}))


def test_the_submitting_agent_s_branch_is_recorded(client, key, db, proj):
    """An item built inline by an agent that registered a branch lands on that branch, the
    same derivation `claim_item` makes."""
    who = _agent(client, key, "inline", branch="gb/inline-7", capabilities={"instance": "in"})
    item = _ok(_mcp(client, key, "create_item", {"title": "inline", "status": "in_progress"}))["id"]
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": who}))
    db.expire_all()
    assert db.get(Item, items_svc.keys.resolve_item(db, item)).branch == "gb/inline-7"


def test_a_person_moving_a_card_is_not_gated(client, auth, db, proj):
    """The board is a tracker too: a human's item may have no branch at all."""
    made = client.post("/api/items", json={"title": "by hand", "project_id": proj},
                       headers=auth).json()
    r = client.patch(f"/api/items/{made['id']}", json={"status": "review"}, headers=auth)
    assert r.status_code == 200, r.text


# ---- a bounce pin to an author that has gone (GRPH-946) ---------------------------------------

def _pinned(db, item: str, author: str) -> Item:
    stored = db.get(Item, item)
    # What `bounce` leaves: back to `next`, the lease released, the author pinned.
    stored.status = "next"
    stored.claimed_by = None
    stored.claimed_at = None
    stored.bounce_pinned_to = author
    stored.bounce_pinned_until = datetime.now(timezone.utc) + timedelta(seconds=600)
    db.commit()
    return stored


def test_a_pin_to_an_offline_author_lapses_at_once(client, key, db, proj):
    """Sabotage: drop the presence check in `bounce_pin_holder` and the item stays reserved for
    a process that exited, for the rest of the 600s lease."""
    from app.models import Agent

    author = _agent(client, key, "gone", worktree="/w/gone", capabilities={"instance": "g"})
    item = _in_review(client, key, db, supervised=False)
    stored = _pinned(db, item, author)
    assert fleet_svc.bounce_pin_holder(stored) == author

    db.get(Agent, author).last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=fleet_svc.presence_ttl_seconds() + 5)
    db.commit()
    assert fleet_svc.bounce_pin_holder(stored) is None

    # And through a claim path, not only the helper.
    other = _agent(client, key, "other", capabilities={"instance": "o"})
    got = items_svc.claim_item(db, item, other)
    assert got is not None and got.claimed_by == other


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
