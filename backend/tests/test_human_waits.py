"""Typed human waits on the same tracker (GRPH-612 / P30 D11).

Free-text `blocker` without a `wait:` tag is not a wait. Filing a wait is not
finishing the work. When the wait item is marked `done`, the original returns
to `next` unless another unmet dep remains.
"""
from __future__ import annotations

import pytest

from app.db import SessionLocal
from app.services import items as items_svc
from app.services import links as links_svc
from app.services import waits as wait_svc


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


def _item(db, **kw):
    return items_svc.create_item(db, project_id="core", title=kw.pop("title", "work"), **kw)


# ---- finder ------------------------------------------------------------------


def test_a_free_text_blocker_is_not_a_human_wait(client):
    """SABOTAGE. `blocker="please look"` with no type must not read as a wait."""
    db = SessionLocal()
    try:
        stuck = _item(db, title="stuck", status="blocked")
        items_svc.update_item(db, stuck.id, blocker="please look")
        db.refresh(stuck)
        assert wait_svc.is_human_wait(stuck) is False
        assert stuck.id not in {w.id for w in wait_svc.waiting(db, "core")}
    finally:
        db.close()


def test_a_wait_tagged_blocked_item_is_a_human_wait(client):
    db = SessionLocal()
    try:
        wait = _item(db, title="merge this", status="blocked", tags=["wait:merge"])
        assert wait_svc.is_human_wait(wait) is True
        assert wait.id in {w.id for w in wait_svc.waiting(db, "core")}
    finally:
        db.close()


def test_an_unknown_wait_kind_is_refused():
    with pytest.raises(ValueError, match="unknown wait type"):
        wait_svc.wait_tag("please look")


# ---- filing and clearing -----------------------------------------------------


def test_done_on_the_wait_unblocks_the_original(client):
    db = SessionLocal()
    try:
        original = _item(db, title="feature", status="in_progress")
        wait = _item(db, title="needs merge", status="blocked", tags=["wait:merge"])
        links_svc.create_link(db, a=original.id, b=wait.id, type_="dependency",
                              project_id="core")
        items_svc.update_item(db, original.id, status="blocked")
        db.refresh(original)
        assert original.status == "blocked"

        items_svc.update_item(db, wait.id, status="done")
        db.refresh(original)
        assert original.status == "next", "the wait cleared and the original did not return"
        assert original.blocker == ""
    finally:
        db.close()


def test_another_unmet_dep_keeps_the_original_blocked(client):
    db = SessionLocal()
    try:
        original = _item(db, title="feature", status="blocked")
        wait = _item(db, title="needs merge", status="blocked", tags=["wait:merge"])
        other = _item(db, title="other dep", status="next")
        links_svc.create_link(db, a=original.id, b=wait.id, type_="dependency",
                              project_id="core")
        links_svc.create_link(db, a=original.id, b=other.id, type_="dependency",
                              project_id="core")

        items_svc.update_item(db, wait.id, status="done")
        db.refresh(original)
        assert original.status == "blocked", "another unmet dep was ignored"
    finally:
        db.close()


def test_in_progress_with_a_lease_is_not_rewritten(client):
    db = SessionLocal()
    try:
        original = _item(db, title="still building", status="in_progress")
        wait = _item(db, title="needs merge", status="blocked", tags=["wait:merge"])
        links_svc.create_link(db, a=original.id, b=wait.id, type_="dependency",
                              project_id="core")

        items_svc.update_item(db, wait.id, status="done")
        db.refresh(original)
        assert original.status == "in_progress"
    finally:
        db.close()


# ---- the original is not finished because a wait was filed -------------------


def test_review_is_refused_while_a_wait_dep_is_open(client):
    """SABOTAGE. `update_item(status=review)` on the original because it filed a
    wait must fail — filing a wait is not finishing the work."""
    db = SessionLocal()
    try:
        original = _item(db, title="feature", status="blocked")
        wait = _item(db, title="needs merge", status="blocked", tags=["wait:merge"])
        links_svc.create_link(db, a=original.id, b=wait.id, type_="dependency",
                              project_id="core")
        with pytest.raises(ValueError, match="typed human wait"):
            items_svc.update_item(db, original.id, status="review")
        db.refresh(original)
        assert original.status == "blocked"
    finally:
        db.close()


def test_a_wait_item_can_be_marked_done_without_an_attestation(client):
    """The operator clears the wait from Tracker. A delivery gate would make that
    impossible without CI, which is the opposite of a human emptying the list."""
    db = SessionLocal()
    try:
        wait = _item(db, title="needs merge", status="blocked", tags=["wait:merge"])
        items_svc.update_item(db, wait.id, status="done")
        db.refresh(wait)
        assert wait.status == "done"
    finally:
        db.close()


# ---- the call (MCP) ----------------------------------------------------------


def test_mcp_review_is_refused_while_waiting(client, auth):
    key = client.post("/api/api-keys", json={"name": "wait-w", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    original = _ok(client, key, "create_item", {"title": "feature", "status": "next"})
    wait = _ok(client, key, "create_item",
               {"title": "needs merge", "status": "blocked", "tags": ["wait:merge"]})
    _ok(client, key, "link_items",
        {"a": original["id"], "b": wait["id"], "type": "dependency"})
    _ok(client, key, "update_item", {"id": original["id"], "status": "blocked"})

    res = _rpc(client, key, "update_item",
               {"id": original["id"], "status": "review"})
    assert res.get("isError") is True
    err = (res.get("structuredContent") or {}).get("error") or {}
    assert "wait" in str(err.get("message") or res).lower()


def test_mcp_search_finds_typed_waits_not_free_text(client, auth):
    key = client.post("/api/api-keys", json={"name": "wait-s", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    wait = _ok(client, key, "create_item",
               {"title": "needs a secret", "status": "blocked", "tags": ["wait:secret"]})
    stuck = _ok(client, key, "create_item",
                {"title": "please look", "status": "blocked"})
    _ok(client, key, "update_item",
        {"id": stuck["id"], "blocker": "please look"})

    found = _ok(client, key, "search_items",
                {"status": "blocked", "tags": ["wait:secret"]})
    ids = [r["id"] for r in found["results"]]
    assert wait["id"] in ids
    assert stuck["id"] not in ids
