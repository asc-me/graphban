"""GRPH-1671 — close an item as a duplicate or superseded without an attestation.

The completion gate asks for proof that work was checked. A duplicate did no work, so the gate
could never open (GRPH-1667 sat in `blocked` after GRPH-1668 had shipped the fix). These pin
the narrow way round it: the pointer must name the work, a duplicate's target must be done,
the agent holding the item cannot use it, only a planner (MCP) or the project owner (REST) may
call it, and a resolved item is never counted as delivered.
"""
from __future__ import annotations

import pytest

from app.models import Item
from app.services import items as items_svc
from app.services import prds as prd_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(db, title="t", project_id="core", **kw) -> Item:
    it = items_svc.create_item(db, project_id=project_id, title=title)
    for k, v in kw.items():
        setattr(it, k, v)
    db.commit()
    db.refresh(it)
    return it


def _done(db, title="the fix", project_id="core") -> Item:
    """A finished item. Set directly: what is under test is the pointer, not this item's gate."""
    return _item(db, title, project_id=project_id, status="done")


def _refused(db, item, **kw) -> str:
    with pytest.raises(items_svc.ResolutionRefused) as e:
        items_svc.resolve_item(db, item.id, actor="owner", **kw)
    db.rollback()
    return e.value.reason


# ── the service ───────────────────────────────────────────────────────────────

def test_duplicate_of_a_done_item_closes_without_an_attestation(db):
    fix = _done(db)
    dup = _item(db, "same bug, filed twice")
    out = items_svc.resolve_item(db, dup.id, kind="duplicate", of=fix.key, actor="owner")
    assert out.status == "done"
    assert out.resolution["kind"] == "duplicate"
    assert out.resolution["of"] == fix.key
    assert out.resolution["by"] == "owner"
    assert not items_svc.attestation_receipts(out.evidence)
    assert out.delivered is False and fix.delivered is True
    assert items_svc.item_dict(out)["resolution"]["of"] == fix.key


def test_plain_done_without_an_attestation_is_still_refused(db):
    it = _item(db)
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")


def test_duplicate_of_unfinished_work_is_refused(db):
    """Sabotage: drop the `target_not_done` check in resolve_item and this test fails — a
    duplicate of open work would close without anything having been checked."""
    open_fix = _item(db, "still building", status="in_progress")
    dup = _item(db)
    assert _refused(db, dup, kind="duplicate", of=open_fix.key) == "target_not_done"
    db.refresh(dup)
    assert dup.status != "done" and dup.resolution is None


def test_each_bad_pointer_has_its_own_reason(db):
    dup = _item(db)
    from app.models import Project
    db.add(Project(id="other-proj", name="Other", tag="OTH"))
    db.commit()
    other = _item(db, "elsewhere", project_id="other-proj", status="done")
    assert _refused(db, dup, kind="duplicate", of=dup.key) == "target_self"
    assert _refused(db, dup, kind="duplicate", of="CORE-99999") == "target_missing"
    assert _refused(db, dup, kind="duplicate", of=other.key) == "target_other_project"
    assert _refused(db, dup, kind="wontfix", of=dup.key) == "bad_kind"


def test_superseded_needs_a_reason_and_may_point_at_open_work_or_a_prd(db):
    newer = _item(db, "the replacement", status="next")
    old = _item(db, "the old approach")
    assert _refused(db, old, kind="superseded", of=newer.key) == "reason_required"
    out = items_svc.resolve_item(db, old.id, kind="superseded", of=newer.key,
                                 reason="replaced by a narrower item", actor="owner")
    assert out.status == "done" and out.resolution["reason"] == "replaced by a narrower item"

    prd = prd_svc.create_prd(db, project_id="core", title="Successor", body="## A\n\nx\n")
    third = _item(db, "folded into a PRD")
    out = items_svc.resolve_item(db, third.id, kind="superseded", of=prd.key,
                                 reason="now a PRD", actor="owner")
    assert out.resolution["of"] == prd.key


def test_the_agent_holding_the_item_cannot_close_it_this_way(db):
    fix = _done(db)
    built = _item(db, status="in_progress", claimed_by="CORE-A7")
    assert _refused(db, built, kind="duplicate", of=fix.key, actor_agent="CORE-A7") == "claim_holder"
    reviewing = _item(db, status="review", review_claimed_by="CORE-A8")
    assert _refused(db, reviewing, kind="duplicate", of=fix.key,
                    actor_agent="CORE-A8") == "claim_holder"
    # Someone else (the planner) may, and it releases the hold.
    out = items_svc.resolve_item(db, built.id, kind="duplicate", of=fix.key,
                                 actor="CORE-A1", actor_agent="CORE-A1")
    assert out.status == "done" and out.claimed_by is None


def test_already_done_is_refused(db):
    fix = _done(db)
    done = _done(db, "already finished")
    assert _refused(db, done, kind="duplicate", of=fix.key) == "already_done"


def test_reopening_drops_the_resolution(db):
    fix = _done(db)
    dup = _item(db)
    items_svc.resolve_item(db, dup.id, kind="duplicate", of=fix.key, actor="owner")
    items_svc.update_item(db, dup.id, status="next")
    db.refresh(dup)
    assert dup.resolution is None
    # And closing it again faces the gate like any other item.
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, dup.id, status="done")


def test_a_resolved_item_does_not_deliver_its_prd_section(db):
    """Sabotage: count `status == "done"` in prds.completeness and this fails — the
    duplicate makes the section read as delivered with nothing built against it."""
    from tests.test_completeness import BODY, _approve

    prd = _approve(db, prd_svc.create_prd(db, title="Spec", project_id="core", body=BODY))
    fix = _done(db)
    dup = _item(db, prd_id=prd.id, prd_section="Judging")
    items_svc.resolve_item(db, dup.id, kind="duplicate", of=fix.key, actor="owner")
    report = prd_svc.completeness(db, prd)
    assert report["governed"], report
    sec = next(s for s in report["sections"] if s["section"] == "Judging")
    assert sec["planned"] == 1 and sec["delivered"] == 0
    assert sec["state"] != "delivered"
    assert "Judging" in report["undelivered"]


# ── the MCP boundary: planner only ────────────────────────────────────────────

def _mcp(client, key, name, args):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Resolve"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]


def _mk(client, key, title, status="next"):
    res = _mcp(client, key, "create_item", {"title": title, "status": status})
    assert not res.get("isError"), res
    return res["structuredContent"]["id"]


def test_mcp_planner_closes_as_duplicate(client, key, db):
    fix = _mk(client, key, "the fix")
    db.get(Item, items_svc.get_item(db, fix).id).status = "done"
    db.commit()
    dup = _mk(client, key, "filed twice")
    res = _mcp(client, key, "update_item",
               {"id": dup, "resolution": {"kind": "duplicate", "of": fix}})
    assert not res.get("isError"), res
    body = res["structuredContent"]
    assert body["status"] == "done" and body["resolution"]["of"] == fix


def test_mcp_refusal_names_the_rule_and_reason(client, key):
    open_fix = _mk(client, key, "open")
    dup = _mk(client, key, "filed twice")
    res = _mcp(client, key, "update_item",
               {"id": dup, "resolution": {"kind": "duplicate", "of": open_fix}})
    assert res.get("isError"), res
    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict"
    assert "not done" in err["message"]


def test_mcp_worker_is_refused(client, auth, key, proj, db):
    code = client.post("/api/fleet/seats", json={"project_id": proj, "roles": ["worker"],
                                                 "wave": "w"},
                       headers=auth).json()["seats"][0]["code"]
    me = _mcp(client, key, "register_agent", {"branch": "gb/t", "label": "w",
                                              "enrolment_code": code})["structuredContent"]["agent_id"]
    fix = _mk(client, key, "the fix")
    db.get(Item, items_svc.get_item(db, fix).id).status = "done"
    db.commit()
    dup = _mk(client, key, "filed twice")
    res = _mcp(client, key, "update_item", {"id": dup, "agent_id": me,
                                            "resolution": {"kind": "duplicate", "of": fix}})
    assert res.get("isError"), res
    assert res["structuredContent"]["error"]["code"] == "unauthorized"
    db.expire_all()
    assert items_svc.get_item(db, dup).status != "done"


def test_mcp_resolution_must_be_sent_alone(client, key):
    dup = _mk(client, key, "filed twice")
    res = _mcp(client, key, "update_item", {"id": dup, "title": "renamed",
                                            "resolution": {"kind": "duplicate", "of": dup}})
    assert res.get("isError") and res["structuredContent"]["error"]["code"] == "validation"


# ── the REST boundary: project owner only ─────────────────────────────────────

def test_rest_owner_resolves_and_a_member_is_refused(client, auth, db):
    fix = _done(db)
    dup = _item(db)
    r = client.post(f"/api/items/{dup.key}/resolve",
                    json={"kind": "duplicate", "of": fix.key}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["resolution"]["of"] == fix.key and r.json()["status"] == "done"

    other = _item(db)
    login = client.post("/api/auth/login", json={"email": "dana@ascme-labs.com",
                                                 "password": "graphban"})
    if login.status_code != 200:
        pytest.skip("no second seeded user")
    dana = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r = client.post(f"/api/items/{other.key}/resolve",
                    json={"kind": "duplicate", "of": fix.key}, headers=dana)
    assert r.status_code in (403, 404), r.text


def test_rest_patch_cannot_carry_a_resolution(client, auth, db):
    """The gate skip rides only on `resolve_item`; PATCH must not be a way to send it."""
    fix = _done(db)
    it = _item(db)
    r = client.patch(f"/api/items/{it.key}",
                     json={"status": "done", "resolution": {"kind": "duplicate", "of": fix.key}},
                     headers=auth)
    assert r.status_code == 409, r.text
