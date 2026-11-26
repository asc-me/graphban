"""A blocker must name what it waits on (GRPH-1960).

GRPH-1953's builder wrote "Needs the `gban rules import` command and findings pipeline ...
which are not in this branch." Nothing tied that note to the work, so when it merged nothing
released the item and the P92 drain idled for hours. `update_item(blocker=...)` over MCP and
REST now takes a blocker only when it names an item key, a person or a decision, a typed wait,
or a blocking-fix filing.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Item, Link
from app.services import items as items_svc
from tests import attest

RULE = "build.blocker_names_what_it_waits_on"
TEXT_1953 = ("Needs the `gban rules import` command and findings pipeline (D1 parsers, D3 "
             "command), which are not in this branch.")


def _mcp(client, key, name, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


def _err(res) -> dict:
    assert res.get("isError"), res
    return res["structuredContent"]["error"]


@pytest.fixture()
def key(client, auth):
    return client.post("/api/api-keys", json={"name": "k", "project_id": "core",
                                              "scopes": ["read", "write"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(client, auth, title="x", **kw):
    return client.post("/api/items", json={"title": title, "project_id": "core", **kw},
                       headers=auth).json()


def _blocker(db, item_id):
    db.expire_all()
    return db.get(Item, item_id).blocker or ""


def _key(db, item):
    return db.get(Item, item["id"]).key


def _blocker_links(db, item_id):
    db.expire_all()
    return set(db.scalars(select(Link.b).where(
        Link.type == "dependency", Link.a == item_id,
        Link.reason == items_svc.BLOCKER_DEP_REASON)).all())


def test_the_1953_text_is_refused_over_mcp_and_nothing_is_written(client, auth, key, db):
    it = _item(client, auth)
    e = _err(_mcp(client, key, "update_item", {"id": it["id"], "blocker": TEXT_1953,
                                               "title": "renamed"}))
    assert e["code"] == "conflict"
    assert e.get("rule_id") == RULE or RULE in str(e)
    assert "item key" in e["message"]
    row = db.get(Item, it["id"])
    assert (row.blocker or "") == "" and row.title == "x"


def test_the_1953_text_is_refused_over_rest(client, auth, db):
    it = _item(client, auth)
    r = client.patch(f"/api/items/{it['id']}", json={"blocker": TEXT_1953}, headers=auth)
    assert r.status_code == 409, r.text
    assert r.json()["rule_id"] == RULE
    assert (db.get(Item, it["id"]).blocker or "") == ""


def test_the_1950_text_gives_two_links_and_releases_when_both_are_done(client, auth, key, db):
    d1, d3 = _item(client, auth, "D1 parsers"), _item(client, auth, "D3 command")
    it = _item(client, auth, "waits")
    text = (f"needs {_key(db, d1)} (D1 parsers) and {_key(db, d3)} (D3 command) to land "
            "first")
    _ok(_mcp(client, key, "update_item", {"id": it["id"], "blocker": text}))
    assert _blocker(db, it["id"]) == text
    assert _blocker_links(db, it["id"]) == {d1["id"], d3["id"]}
    attest.complete(db, d1["id"])
    assert _blocker(db, it["id"]) == text, "one of two named items is still open"
    attest.complete(db, d3["id"])
    assert _blocker(db, it["id"]) == ""


@pytest.mark.parametrize("text", [
    "waiting on a decision about the schema",
    "needs a person to rotate the token",
    "the human owner has to say yes",
])
def test_a_person_or_decision_is_accepted(client, key, auth, db, text):
    it = _item(client, auth)
    _ok(_mcp(client, key, "update_item", {"id": it["id"], "blocker": text}))
    assert _blocker(db, it["id"]) == text


def test_a_typed_wait_is_accepted_whether_tagged_before_or_in_the_same_call(client, auth, key):
    it = _item(client, auth)
    _ok(_mcp(client, key, "update_item", {"id": it["id"], "status": "blocked",
                                          "tags": ["wait:merge"], "blocker": "PR 12 open"}))
    other = _item(client, auth, tags=["wait:access"])
    r = client.patch(f"/api/items/{other['id']}", json={"blocker": "staging credentials"},
                     headers=auth)
    assert r.status_code == 200, r.text


def test_a_blocking_fix_filing_is_accepted(client, auth, key, db):
    it = _item(client, auth)
    text = f"GRPH-424242{items_svc.BLOCKING_FIX_BLOCKER_SUFFIX}"
    _ok(_mcp(client, key, "update_item", {"id": it["id"], "blocker": text}))
    assert _blocker(db, it["id"]) == text


def test_a_key_that_resolves_to_nothing_is_refused_and_named(client, auth, key):
    it = _item(client, auth)
    e = _err(_mcp(client, key, "update_item", {"id": it["id"], "blocker": "needs GRPH-999999"}))
    assert e["code"] == "conflict" and "GRPH-999999" in e["message"]


def test_an_empty_blocker_still_clears(client, auth, key, db):
    dep = _item(client, auth)
    it = _item(client, auth)
    _ok(_mcp(client, key, "update_item", {"id": it["id"], "blocker": f"needs {_key(db, dep)}"}))
    assert _blocker(db, it["id"])
    _ok(_mcp(client, key, "update_item", {"id": it["id"], "blocker": ""}))
    assert _blocker(db, it["id"]) == ""


def test_a_gbfleet_hold_is_accepted(client, auth, key, db):
    it = _item(client, auth)
    text = ("external check still red: lint. Repo actions passed; not returned to the author. "
            "[gbfleet external check hold]")
    _ok(_mcp(client, key, "update_item", {"id": it["id"], "blocker": text}))
    assert _blocker(db, it["id"]) == text


def test_naming_only_its_own_key_is_refused(client, auth, key, db):
    it = _item(client, auth)
    e = _err(_mcp(client, key, "update_item",
                  {"id": it["id"], "blocker": f"{_key(db, it)} needs the import command"}))
    assert e["code"] == "conflict"
    assert _blocker(db, it["id"]) == ""
