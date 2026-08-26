"""An agent can read a PRD body (GRPH-519).

Found by trying to finish a grill through the MCP surface. `answer_grill` returns
`body_absorbed: false` and `answers_body_has_not_absorbed: N` — the server knows the body does
not reflect the recorded answers — and the only tool that could fix it, `update_prd`, replaces
the body WHOLE. There was no agent-accessible read: `GET /api/prds/{id}` needs a user JWT,
`prd_coverage` returns section titles, `prd_acceptance` returns `{"governed": false}` on a
draft.

So the only route to absorbing your own answers was to rewrite the document from memory. That
is exactly the defect GRPH-515 fixed for `write_file` — except here no guard existed at all, so
it would have silently succeeded and dropped every section the agent did not reproduce.

**The load-bearing test is `test_the_body_survives_a_read_modify_write`.** "get_prd returns a
body" passes against a tool that returns a truncated or stale one; only round-tripping through
`update_prd` shows the read is good enough to make the replace safe, which is the entire reason
this tool exists.
"""
from __future__ import annotations

import pytest

from app.mcp_server import TOOLS

BODY = (
    "# Providers\n\n"
    "## 1. Overview\n\nOne registry.\n\n"
    "## 2. Key decisions\n\nKeyed by row.\n\n"
    "## 3. Resolution order\n\nLegacy, project, deployment, stub.\n"
)


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def mcp_key(client, auth):
    return client.post("/api/api-keys", json={"name": "prd-reader"},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def prd(client, auth):
    r = client.post("/api/prds", json={"title": "P", "body": BODY, "project_id": "core"},
                    headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _rpc(client, key, tool, args=None):
    """Over HTTP, not by calling `_call_tool` directly.

    A tool that dispatches correctly but is not advertised, or is refused by the manifest, is
    unreachable from a real client — and calling the internal function would hide exactly that.
    """
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _call(client, key, name, **args):
    res = _rpc(client, key, name, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


# ---- the one that matters -----------------------------------------------------------------


def test_the_body_survives_a_read_modify_write(client, mcp_key, prd):
    """THE POINT. Read, append a section, write it back, and every original section is still
    there. A read that returned a truncated body would pass "returns a body" and fail here —
    and in production it would silently delete the sections it did not return.
    """
    before = _call(client, mcp_key, "get_prd", prd_id=prd)["body"]
    assert "## 3. Resolution order" in before

    _call(client, mcp_key, "update_prd", prd_id=prd,
          body=before + "\n## 4. Migration\n\nEvery key becomes a row.\n")

    after = _call(client, mcp_key, "get_prd", prd_id=prd)["body"]
    for section in ("## 1. Overview", "## 2. Key decisions", "## 3. Resolution order",
                    "## 4. Migration"):
        assert section in after, f"{section} was lost across the round trip"


# ---- the read itself ----------------------------------------------------------------------


def test_get_prd_returns_the_markdown_body(client, mcp_key, prd):
    out = _call(client, mcp_key, "get_prd", prd_id=prd)

    assert out["id"] == prd
    assert out["body"] == BODY
    assert out["status"] == "draft"


def test_a_missing_prd_is_not_found(client, mcp_key):
    res = _rpc(client, mcp_key, "get_prd", {"prd_id": "GRPH-P999"})

    assert res.get("isError") is True
    assert "not found" in res["structuredContent"]["error"]["message"].lower()


def test_the_tool_is_advertised(client):
    """A dispatch branch nothing advertises is unreachable from a real client — the shape of
    the GRPH-496 heartbeat bug, where the fix existed and nothing constructed it."""
    assert "get_prd" in {t["name"] for t in TOOLS}
    schema = next(t for t in TOOLS if t["name"] == "get_prd")["inputSchema"]
    assert schema["required"] == ["prd_id"]


def test_update_prd_points_at_the_read(client):
    """`update_prd` replaces the whole body. Its description has to say so AND name the tool
    that makes that survivable — GRPH-513 is the same class of defect: a description pointing
    at a path that cannot do the job."""
    desc = next(t for t in TOOLS if t["name"] == "update_prd")["description"]

    assert "get_prd" in desc, "update_prd never tells the caller to read first"


# ---- tenant isolation: added because sabotage proved it was NOT covered -------------------


def test_a_scoped_key_cannot_read_another_projects_prd(client, auth, decoy, db):
    """FOUND BY SABOTAGE. Deleting the `prd.project_id not in allowed` check left every other
    test in this file passing — the guard was written, correct, and unfalsifiable.

    That is the third recorded instance of this exact shape (see `tests/decoy.py`: GRPH-431's
    `shell_counts`, GRPH-387's `held_areas`). The cause is always the same: with one project
    in the fixture, "scoped to this project" and "everything in the instance" are the same set,
    so the WHERE clause providing isolation has nothing to differ from.

    A PRD is created in the decoy project and read with a key scoped to `core`.
    """
    from tests.decoy import assert_populated

    # The control. If the decoy is empty, "excluded" and "never existed" are the same
    # observation and this test passes without being able to fail.
    assert_populated(db, decoy)
    other = decoy["project_id"]

    made = client.post("/api/prds", json={"title": "Theirs", "body": "# Secret\n\nTheirs.\n",
                                          "project_id": other}, headers=auth)
    assert made.status_code in (200, 201), made.text
    their_prd = made.json()["id"]

    scoped = client.post("/api/api-keys", json={"name": "core-only", "project_id": "core"},
                         headers=auth).json()["plaintext"]

    res = _rpc(client, scoped, "get_prd", {"prd_id": their_prd})

    assert res.get("isError") is True, (
        "a key scoped to `core` read a PRD belonging to another project — the body of a "
        "document it has no claim to"
    )
    body = str(res["structuredContent"])
    assert "Secret" not in body and "Theirs." not in body, "the refusal leaked the body anyway"
