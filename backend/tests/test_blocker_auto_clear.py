"""A `backlog` item's blocker clears when what it names is done (GRPH-1958).

`_satisfied_blocker_keys` only looked at `blocked` and `next`. A planner who linked the
dependencies and parked the item in `backlog` left its blocker set forever, and
`get_backlog` showed it as `ready: false, blocked_by: []`: unready with no reason.
"""
from __future__ import annotations

import json

import pytest

from app.services import fleet as fleet_svc
from app.services import items as items_svc
from app.services import prioritization as prio_svc
from tests import attest

HEAD = "7f18c3db0a1b2c3d4e5f60718293a4b5c6d7e8f9"
BASE = "0000111122223333444455556666777788889999"
GREEN = {"kind": "attestation", "adapter": "github-checks", "commit": HEAD,
         "predicates": [{"name": "suite_green", "passed": True, "detail": "ok"}]}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _in_review(db, title):
    """In `review`, built by another agent, attested at its head: signable."""
    it = items_svc.create_item(db, project_id="core", title=title)
    it.status = "review"
    it.built_by = "builder-agent"
    it.head_commit = HEAD
    it.evidence = items_svc.append_evidence(it.evidence, [
        {"kind": "note", "detail": f"{items_svc.CUT_FROM_MARKER}{BASE} (`gb/fixture-1`)"},
        GREEN])
    db.commit()
    db.refresh(it)
    return it


def _backlog_waiting_on(db, *deps):
    waiting = items_svc.create_item(db, project_id="core", title="waits", status="blocked")
    names = " and ".join(d.key for d in deps)
    items_svc.update_item(db, waiting.id, blocker=f"needs {names} (parsers, command)")
    # The planner's move: dependencies linked, item parked in backlog.
    items_svc.update_item(db, waiting.id, status="backlog")
    db.refresh(waiting)
    assert waiting.status == "backlog" and waiting.blocker
    return waiting


def _cleared(waiting, *deps):
    assert waiting.status == "backlog"
    assert waiting.blocker == ""
    shown = ", ".join(d.key for d in deps)
    assert any((e.get("detail") or "") == f"blocker cleared: {shown} done"
               for e in (waiting.evidence or [])), waiting.evidence


def test_a_backlog_blocker_clears_when_both_named_items_reach_done_via_update_item(db):
    first = items_svc.create_item(db, project_id="core", title="D1 parsers")
    second = items_svc.create_item(db, project_id="core", title="D3 command")
    waiting = _backlog_waiting_on(db, first, second)

    attest.complete(db, first.id)
    db.refresh(waiting)
    assert waiting.blocker, "one of two named items is still open"

    attest.complete(db, second.id)
    db.refresh(waiting)
    _cleared(waiting, first, second)


def test_a_backlog_blocker_clears_when_both_named_items_reach_done_via_sign_off(db):
    first = _in_review(db, "D1 parsers")
    second = _in_review(db, "D3 command")
    waiting = _backlog_waiting_on(db, first, second)

    assert fleet_svc.sign_off(db, item_id=first.id, agent_id="reviewer-1",
                              commit=HEAD).status == "done"
    db.refresh(waiting)
    assert waiting.blocker

    assert fleet_svc.sign_off(db, item_id=second.id, agent_id="reviewer-1",
                              commit=HEAD).status == "done"
    db.refresh(waiting)
    _cleared(waiting, first, second)


def test_the_backlog_row_names_the_blocker_that_keeps_it_unready(db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    waiting = _backlog_waiting_on(db, dep)
    attest.complete(db, dep.id)  # dependency done; blocker clears
    prose = items_svc.create_item(db, project_id="core", title="prose", status="backlog")
    items_svc.update_item(db, prose.id, blocker="Needs the import command")

    rows = {r["item"].id: r for r in prio_svc.prioritized(db, "core")}
    assert rows[waiting.id]["ready"] is True and rows[waiting.id]["blocker"] == ""
    row = rows[prose.id]
    assert row["ready"] is False and row["blocked_by"] == []
    assert row["blocker"] == "Needs the import command"


def test_a_blocker_naming_no_item_key_stays_and_get_backlog_says_so(client, auth, db):
    other = items_svc.create_item(db, project_id="core", title="unrelated")
    prose = items_svc.create_item(db, project_id="core", title="prose", status="backlog")
    items_svc.update_item(db, prose.id, blocker="Needs the import command")
    attest.complete(db, other.id)
    db.refresh(prose)
    assert prose.blocker == "Needs the import command"

    made = client.post("/api/api-keys", json={"name": "backlog-reader", "scopes": ["read"],
                                              "project_id": "core"}, headers=auth).json()
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_backlog", "arguments": {"limit": 200}}},
        headers={"X-API-Key": made["plaintext"]}).json()["result"]
    assert not r.get("isError"), r
    out = json.loads(r["content"][0]["text"])
    assert "blocker" in out["fields"]
    row = next(x for x in out["results"] if x["id"] == prose.key or x["id"] == prose.id)
    assert row["ready"] is False and row["blocked_by"] == []
    assert row["blocker"] == "Needs the import command"
