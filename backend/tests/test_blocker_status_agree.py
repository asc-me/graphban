"""Status and blocker cannot disagree (GRPH-1961).

A planner moved two blocked items to `backlog`; their blocker text stayed and kept them
unready with no `blocked` status to show it, so a drain idled for hours. Leaving `blocked`
now clears the blocker or is refused, and a blocker written onto a backlog/next item
makes it `blocked`.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.services import items as items_svc
from tests import attest


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _blocked_on(db, text):
    it = items_svc.create_item(db, project_id="core", title="waits", status="blocked")
    items_svc.update_item(db, it.id, blocker=text)
    db.refresh(it)
    assert it.status == "blocked" and it.blocker == text
    return it


def _notes(it):
    return [e.get("detail") or "" for e in (it.evidence or []) if e.get("kind") == "note"]


# --- leaving blocked -----------------------------------------------------------------

@pytest.mark.parametrize("target", ["backlog", "next"])
def test_leaving_blocked_is_refused_while_the_blocker_names_an_open_item(db, target):
    dep = items_svc.create_item(db, project_id="core", title="D1 parsers")
    it = _blocked_on(db, f"needs {dep.key} (D1 parsers)")
    with pytest.raises(items_svc.BlockerDisagrees) as e:
        items_svc.update_item(db, it.id, status=target)
    assert dep.key in str(e.value) and "needs" in str(e.value)
    db.refresh(it)
    assert it.status == "blocked" and it.blocker


@pytest.mark.parametrize("target", ["backlog", "next"])
def test_leaving_blocked_clears_a_prose_blocker_in_the_same_call_with_a_note(db, target):
    it = _blocked_on(db, "Needs the import command, not in this branch")
    out = items_svc.update_item(db, it.id, status=target)
    assert out.status == target
    assert out.blocker == ""
    assert ("blocker cleared on leaving blocked: Needs the import command, not in this "
            "branch") in _notes(out)


def test_leaving_blocked_clears_a_blocker_whose_named_items_are_done(db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    it = _blocked_on(db, f"needs {dep.key}")
    attest.complete(db, dep.id)
    db.refresh(it)
    # GRPH-1530 already released it; the call that leaves blocked has nothing to refuse.
    assert it.blocker == ""
    assert items_svc.update_item(db, it.id, status="backlog").status == "backlog"


def test_leaving_blocked_is_refused_on_a_typed_wait(db):
    it = items_svc.create_item(db, project_id="core", title="wait", status="blocked",
                               tags=["wait:access"])
    items_svc.update_item(db, it.id, blocker="a person signs this off")
    with pytest.raises(items_svc.BlockerDisagrees) as e:
        items_svc.update_item(db, it.id, status="next")
    assert "wait:access" in str(e.value)


def test_clearing_the_blocker_in_the_same_call_is_not_refused(db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    it = _blocked_on(db, f"needs {dep.key}")
    out = items_svc.update_item(db, it.id, status="next", blocker="")
    assert out.status == "next" and out.blocker == ""


# --- writing a blocker ---------------------------------------------------------------

@pytest.mark.parametrize("start", ["backlog", "next"])
def test_a_blocker_written_onto_a_backlog_or_next_item_sets_blocked(db, start):
    it = items_svc.create_item(db, project_id="core", title="t", status=start)
    out = items_svc.update_item(db, it.id, blocker="Needs the import command")
    assert out.status == "blocked" and out.blocker == "Needs the import command"


def test_a_blocker_with_an_explicit_backlog_status_is_refused(db):
    it = items_svc.create_item(db, project_id="core", title="t", status="next")
    with pytest.raises(items_svc.BlockerDisagrees):
        items_svc.update_item(db, it.id, status="backlog", blocker="Needs a thing")
    db.refresh(it)
    assert it.status == "next" and not it.blocker


def test_a_blocker_naming_only_done_items_clears_and_keeps_the_status(db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    attest.complete(db, dep.id)
    it = items_svc.create_item(db, project_id="core", title="t", status="backlog")
    out = items_svc.update_item(db, it.id, blocker=f"needs {dep.key}")
    assert out.status == "backlog" and out.blocker == ""


def test_no_write_leaves_a_backlog_or_next_item_with_a_live_blocker(db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    calls = [
        ("backlog", {"blocker": "prose"}),
        ("next", {"blocker": f"needs {dep.key}"}),
        ("blocked", {"blocker": f"needs {dep.key}", "status": "backlog"}),
        ("blocked", {"blocker": "prose", "status": "next"}),
    ]
    for start, fields in calls:
        it = items_svc.create_item(db, project_id="core", title="t", status=start)
        if start == "blocked":
            items_svc.update_item(db, it.id, blocker="first")
        try:
            items_svc.update_item(db, it.id, **fields)
        except items_svc.BlockerDisagrees:
            pass
        db.refresh(it)
        assert not (it.status in ("backlog", "next") and (it.blocker or "").strip()), (
            start, fields, it.status, it.blocker)


# --- the surfaces --------------------------------------------------------------------

def test_patch_refuses_leaving_blocked_naming_the_blocker(client, auth, db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    it = _blocked_on(db, f"needs {dep.key}")
    r = client.patch(f"/api/items/{it.id}", json={"status": "backlog"}, headers=auth)
    assert r.status_code == 422, r.text  # update_item's refusals map ValueError to 422
    assert dep.key in r.json()["detail"]


def test_mcp_update_item_clears_a_prose_blocker_on_leaving_blocked(client, auth, db):
    it = _blocked_on(db, "Needs the import command")
    made = client.post("/api/api-keys", json={"name": "blk-writer", "scopes": ["write"],
                                              "project_id": "core"}, headers=auth).json()
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "update_item", "arguments": {"id": it.id, "status": "next"}}},
        headers={"X-API-Key": made["plaintext"]}).json()["result"]
    assert not r.get("isError"), r
    out = json.loads(r["content"][0]["text"])
    assert out["status"] == "next"
    db.refresh(it)
    assert it.blocker == ""


# --- the backfill --------------------------------------------------------------------

def _migration():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "0233_blocker_status_agree.py")
    spec = importlib.util.spec_from_file_location("migration_0233", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_moves_backlog_and_next_rows_with_a_blocker_to_blocked(db):
    legacy = []
    for status, blocker in [("backlog", "needs X"), ("next", "prose"), ("backlog", "   "),
                            ("next", ""), ("in_progress", "held")]:
        it = items_svc.create_item(db, project_id="core", title="legacy")
        it.status, it.blocker = status, blocker  # written the way rows before GRPH-1961 were
        legacy.append(it)
    db.commit()
    mod = _migration()
    with db.get_bind().begin() as conn:
        assert mod.backfill(conn) == 2
    with db.get_bind().begin() as conn:
        assert mod.backfill(conn) == 0
    for it in legacy:
        db.refresh(it)
    assert [it.status for it in legacy] == [
        "blocked", "blocked", "backlog", "next", "in_progress"]


def test_mcp_update_item_refuses_leaving_blocked_as_conflict(client, auth, db):
    dep = items_svc.create_item(db, project_id="core", title="dep")
    it = _blocked_on(db, f"needs {dep.key}")
    made = client.post("/api/api-keys", json={"name": "blk-refused", "scopes": ["write"],
                                              "project_id": "core"}, headers=auth).json()
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "update_item", "arguments": {"id": it.id, "status": "backlog"}}},
        headers={"X-API-Key": made["plaintext"]}).json()["result"]
    assert r.get("isError"), r
    err = r["structuredContent"]["error"]
    assert err["code"] == "conflict" and dep.key in err["message"]


# --- fleet release paths (GRPH-1970) -------------------------------------------------
# These write `status` directly and never reach `reconcile_blocker_status`, so the guard
# is driven at each call site: an `in_progress` item carrying a blocker must not land on
# `next` with that blocker still live.

PROSE = "Needs the import command, not in this branch"


def _fleet_agent(db, *, enrolment_id=None, minter=None):
    from datetime import datetime, timezone
    from app.models import Agent
    import uuid
    agent = Agent(
        id=f"A-{uuid.uuid4().hex[:8]}", number=1, project_id="core", label="w",
        capabilities={}, enrolment_id=enrolment_id, active_role="worker", state="working",
        registered_at=datetime.now(timezone.utc), last_seen_at=datetime.now(timezone.utc))
    db.add(agent)
    db.flush()
    return agent


def _held(db, agent, blocker):
    from datetime import datetime, timezone
    it = items_svc.create_item(db, project_id="core", title="held", status="in_progress")
    it.claimed_by = agent.id if agent else None
    it.claimed_at = datetime.now(timezone.utc)
    it.assignee = agent.id if agent else ""
    it.blocker = blocker
    db.commit()
    db.refresh(it)
    assert it.status == "in_progress" and it.blocker == blocker
    return it


def _seated_agent(db, minted_by=None):
    from app.services import fleet
    seat = fleet.issue_enrolment(db, project_id="core", role="worker", wave="w-1970",
                                 minted_by=minted_by)[0]
    db.flush()
    return _fleet_agent(db, enrolment_id=seat.id)


def _quarantine(db, agent, it):
    from app.services import fleet
    fleet.quarantine(db, agent.id)


def _end_wave(db, agent, it):
    from app.services import fleet
    fleet.end_wave(db, project_id="core", wave="w-1970")


def _retire_wave(db, agent, it):
    from app.services import fleet
    fleet.retire_wave(db, minter_id="GRPH-A1970", project_id="core")


def _send_back(db, agent, it):
    from app.services import fleet
    fleet._send_back_behind(db, it, branch="gb/x", reason="behind the trunk")
    db.commit()


_RELEASES = {
    "quarantine": (_quarantine, lambda db: _fleet_agent(db)),
    "end_wave": (_end_wave, lambda db: _seated_agent(db)),
    "retire_wave": (_retire_wave, lambda db: _seated_agent(db, minted_by="GRPH-A1970")),
    "send_back_behind": (_send_back, lambda db: _fleet_agent(db)),
}


@pytest.mark.parametrize("path", sorted(_RELEASES))
def test_a_fleet_release_clears_a_prose_blocker_with_a_note(db, path):
    release, make_agent = _RELEASES[path]
    agent = make_agent(db)
    it = _held(db, agent, PROSE)
    release(db, agent, it)
    db.refresh(it)
    assert it.status == "next", path
    assert it.blocker == "", f"{path} left a next row with a live blocker"
    assert f"blocker cleared on leaving in_progress: {PROSE}" in _notes(it)


@pytest.mark.parametrize("path", sorted(_RELEASES))
def test_a_fleet_release_blocks_while_the_blocker_names_an_open_item(db, path):
    release, make_agent = _RELEASES[path]
    dep = items_svc.create_item(db, project_id="core", title="D1 parsers")
    agent = make_agent(db)
    it = _held(db, agent, f"needs {dep.key}")
    release(db, agent, it)
    db.refresh(it)
    assert it.status == "blocked", path
    assert it.blocker == f"needs {dep.key}"


@pytest.mark.parametrize("path", sorted(_RELEASES))
def test_a_fleet_release_without_a_blocker_lands_on_next(db, path):
    release, make_agent = _RELEASES[path]
    agent = make_agent(db)
    it = _held(db, agent, "")
    release(db, agent, it)
    db.refresh(it)
    assert it.status == "next" and it.blocker == ""
    assert not any("blocker cleared" in n for n in _notes(it))


def _red(monkeypatch):
    from app.services import fleet
    from app.services import rule_toggles as T
    from app.services import rules as R
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=fleet.REOPEN_ON_RED_RULE, stage="merge", title="Reopen on red", summary="s",
        enforcement="enforced",
        enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=True, default_params={}))
    monkeypatch.setattr(fleet, "_red_at_signed_head", lambda item, arrived: (
        {"commit": "abcdef0123456789", "run_ref": "https://ci/run/1"},
        {"name": "suite_green", "detail": "backend failed"}))


def test_reopen_on_red_clears_a_prose_blocker_with_a_note(db, monkeypatch):
    from app.services import fleet
    _red(monkeypatch)
    it = _held(db, None, PROSE)
    it.status = "done"
    db.commit()
    assert fleet.reopen_on_red(db, it, []) is True
    db.commit()
    db.refresh(it)
    assert it.status == "next" and it.blocker == ""
    assert f"blocker cleared on leaving done: {PROSE}" in _notes(it)


def test_reopen_on_red_blocks_while_the_blocker_names_an_open_item(db, monkeypatch):
    from app.services import fleet
    _red(monkeypatch)
    dep = items_svc.create_item(db, project_id="core", title="dep")
    it = _held(db, None, f"needs {dep.key}")
    it.status = "done"
    db.commit()
    assert fleet.reopen_on_red(db, it, []) is True
    db.commit()
    db.refresh(it)
    assert it.status == "blocked" and it.blocker == f"needs {dep.key}"


def test_bounce_still_clears_the_blocker(db):
    from app.services import fleet
    agent = _fleet_agent(db)
    it = _held(db, agent, PROSE)
    it.status = "review"
    it.built_by = agent.id
    db.commit()
    fleet.bounce(db, item_id=it.id, reason="needs a test", agent_id="GRPH-A-reviewer")
    db.refresh(it)
    assert it.status == "next" and it.blocker == ""
