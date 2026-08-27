"""A lean row says it is lean (GRPH-440).

`search_items` serialises three fields. A consumer asking a row for `built_by` got nothing
back, and in every client language absent arrives as null — `.get()` in Python, `undefined`
in TS. So **"nobody built this" and "this payload does not say" were the same answer**, on
the exact field a reviewer consults to decide what it may take.

It was misread twice in one day from two different tools, minutes after `update_item` had
returned the author for the same items.

The compact payload is defensible; the manifest has a token budget and these reads return
many rows. What was not defensible is that the compact form was indistinguishable from a
complete one. So the ENVELOPE names the projection — one short array per page rather than
four more nulls per row.
"""
from __future__ import annotations

import json

import pytest

LEAN_READS = ("search_items", "get_backlog")


@pytest.fixture()
def key(client, auth):
    return client.post("/api/api-keys", json={"name": "lean"}, headers=auth).json()["plaintext"]


def _call(client, key, tool, args=None):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": tool, "arguments": args or {}}},
                    headers={"X-API-Key": key}).json()["result"]
    assert not r.get("isError"), r
    return json.loads(r["content"][0]["text"])


def _args(tool, **extra):
    return {"query": "", **extra} if tool == "search_items" else dict(extra)


# ── the fix ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", LEAN_READS)
def test_a_lean_page_names_what_its_rows_carry(client, key, tool):
    page = _call(client, key, tool, _args(tool))
    assert "fields" in page, "the projection is unnamed — a caller cannot tell partial from empty"
    for f in ("id", "title", "status"):
        assert f in page["fields"]


@pytest.mark.parametrize("tool", LEAN_READS)
def test_the_declaration_matches_the_rows_actually_returned(client, auth, key, tool):
    """The declaration is only worth anything if it is true. A `fields` list that drifted from
    the rows would be worse than none: a consumer would trust it and be wrong."""
    client.post("/api/items", json={"title": "a row to return", "status": "next"}, headers=auth)
    page = _call(client, key, tool, _args(tool))
    assert page["results"], "no rows — this test would assert nothing"
    for row in page["results"]:
        assert set(row) == set(page["fields"]), (
            f"{tool} declared {sorted(page['fields'])} and returned {sorted(row)}")


def test_authorship_is_absent_from_the_lean_declaration(client, key):
    """Naming the projection is only useful if the fields that caused the misreading are
    visibly NOT in it."""
    page = _call(client, key, "search_items", {"query": ""})
    for f in ("built_by", "claimed_by", "reviewed_by", "review_claimed_by"):
        assert f not in page["fields"]


def test_get_backlog_declares_its_ranking_fields_too(client, key):
    """The ranking signal is on every row whichever projection is in play — it is the reason
    to call `get_backlog` at all. A declaration listing only the item shape would be false
    about the rows it ships."""
    page = _call(client, key, "get_backlog", {})
    for f in ("ready", "blocked_by", "unblocks", "votes", "score"):
        assert f in page["fields"], f


# ── what full must still do ───────────────────────────────────────────────────

@pytest.mark.parametrize("tool", LEAN_READS)
def test_a_full_page_declares_nothing_because_it_omits_nothing(client, key, tool):
    """`fields` exists to warn about a projection. Emitting it on the complete shape would
    invite a consumer to treat that list as the item's whole vocabulary, which it is not —
    `_item_dict` adds `intent_hold` only on the rows that have one."""
    page = _call(client, key, tool, _args(tool, fields="full"))
    assert "fields" not in page


@pytest.mark.parametrize("tool", LEAN_READS)
def test_full_still_carries_the_authorship(client, auth, key, tool):
    """The escape hatch has to actually work, or naming the omission just documents a dead
    end."""
    client.post("/api/items", json={"title": "authored", "status": "next"}, headers=auth)
    page = _call(client, key, tool, _args(tool, fields="full"))
    assert page["results"]
    assert "built_by" in page["results"][0]


# ── the description ───────────────────────────────────────────────────────────

def test_the_tool_description_states_the_projection():
    """The ticket's other half: nothing told a caller the answer it was about to act on was
    partial. A caller reads the description once and the response every time, so both say it."""
    from app.mcp_server import TOOLS

    for name in LEAN_READS:
        t = next(t for t in TOOLS if t["name"] == name)
        desc = json.dumps(t["inputSchema"]["properties"]["fields"])
        assert "fields" in desc and "unreported" in desc, desc
