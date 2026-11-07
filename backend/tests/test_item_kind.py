"""GRPH-1358: an item records what kind of change it is — feature, fix or chore — or nothing.

Empty is the third answer, "not recorded", and must never be read as "not a feature": rows
that predate the column stay empty rather than being guessed at.
"""
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from tests.prd_approve import approve_id

BODY = "# Spec\n\n## Ingest\nread the feed\n\n## Transform\nnormalize\n"


def _key(client, auth):
    return client.post("/api/api-keys", json={"name": "kind-agent", "project_id": "core"},
                       headers=auth).json()["plaintext"]


def _mcp(client, key, name, args):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": args}}, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_mcp_create_with_kind_reads_back(client, auth):
    key = _key(client, auth)
    made = _mcp(client, key, "create_item", {"title": "New thing", "kind": "feature"})
    assert not made.get("isError"), made
    item_id = made["structuredContent"]["id"]
    assert made["structuredContent"]["kind"] == "feature"
    # Every read surface: MCP details, REST detail, REST list.
    got = _mcp(client, key, "get_item_details", {"id": item_id})["structuredContent"]
    assert got["kind"] == "feature"
    assert client.get(f"/api/items/{item_id}", headers=auth).json()["kind"] == "feature"
    rows = client.get("/api/items?project_id=core", headers=auth).json()
    assert next(r for r in rows if r["id"] == item_id)["kind"] == "feature"


def test_mcp_update_sets_kind(client, auth):
    key = _key(client, auth)
    item_id = _mcp(client, key, "create_item", {"title": "Tidy"})["structuredContent"]["id"]
    out = _mcp(client, key, "update_item", {"id": item_id, "kind": "chore"})
    assert out["structuredContent"]["kind"] == "chore"


def test_bad_kind_is_refused_naming_allowed_values(client, auth):
    key = _key(client, auth)
    r = _mcp(client, key, "create_item", {"title": "Typo", "kind": "feat"})
    assert r["isError"] is True
    err = r["structuredContent"]["error"]
    text = f"{err.get('message', '')} {err.get('hint', '')}"
    for allowed in ("feature", "fix", "chore"):
        assert allowed in text, text
    item_id = _mcp(client, key, "create_item", {"title": "Ok"})["structuredContent"]["id"]
    r = _mcp(client, key, "update_item", {"id": item_id, "kind": "feat"})
    assert r["isError"] is True


def test_rest_refuses_bad_kind_and_accepts_good(client, auth):
    bad = client.post("/api/items", json={"title": "x", "project_id": "core", "kind": "feat"},
                      headers=auth)
    assert bad.status_code == 422
    assert "feature, fix, chore" in bad.json()["detail"]
    ok = client.post("/api/items", json={"title": "x", "project_id": "core", "kind": "fix"},
                     headers=auth)
    assert ok.status_code == 201 and ok.json()["kind"] == "fix"
    r = client.patch(f"/api/items/{ok.json()['id']}", json={"kind": "nope"}, headers=auth)
    assert r.status_code == 422
    r = client.patch(f"/api/items/{ok.json()['id']}", json={"kind": "chore"}, headers=auth)
    assert r.json()["kind"] == "chore"


def test_bug_tag_without_kind_defaults_to_fix(client, auth):
    key = _key(client, auth)
    out = _mcp(client, key, "create_item", {"title": "It broke", "tags": ["Bug"]})
    assert out["structuredContent"]["kind"] == "fix"
    # An explicit kind wins over the tag default.
    out = _mcp(client, key, "create_item",
               {"title": "Bug-tagged chore", "tags": ["bug"], "kind": "chore"})
    assert out["structuredContent"]["kind"] == "chore"


def test_untagged_item_without_kind_is_not_recorded_not_feature(client, auth):
    key = _key(client, auth)
    out = _mcp(client, key, "create_item", {"title": "Plain"})
    assert out["structuredContent"]["kind"] == ""


def test_decompose_create_files_features(client, auth):
    prd = client.post("/api/prds", json={"title": "Spec", "body": BODY, "project_id": "core"},
                      headers=auth).json()
    approve_id(prd["id"])
    r = client.post(f"/api/prds/{prd['id']}/decompose?create=true", headers=auth)
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert len(created) == 2
    for item_id in created:
        assert client.get(f"/api/items/{item_id}", headers=auth).json()["kind"] == "feature"


def test_migration_leaves_existing_rows_empty_not_feature():
    """Run 0187's own upgrade() against a table that already holds a row."""
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0187_item_kind.py"
    spec = importlib.util.spec_from_file_location("mig_0187", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == "0186"

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE items (id VARCHAR PRIMARY KEY, title VARCHAR)"))
        conn.execute(sa.text("INSERT INTO items (id, title) VALUES ('OLD-1', 'shipped long ago')"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()
        kind = conn.execute(sa.text("SELECT kind FROM items WHERE id = 'OLD-1'")).scalar_one()
    assert kind == ""

