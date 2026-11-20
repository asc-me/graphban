"""GRPH-1746: a project stores an ombre second colour and a tile pattern.

`accent` stays the one solid colour. `accent2` is null for a solid tile, and a
value equal to `accent` is stored as null. `pattern` is one of the tile
patterns, default `none`. Both new fields only shape the tile fill.
"""
import importlib.util
import json
import pathlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.db import engine

_BACKEND = pathlib.Path(__file__).parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0216_project_tile_fill.py"
_IS_SQLITE = engine.url.drivername.startswith("sqlite")


def _migration():
    spec = importlib.util.spec_from_file_location("mig_0216", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(eng, fn):
    with eng.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()


def _mcp(client, api_key: str, name: str, args: dict) -> dict:
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(client, api_key: str, name: str, args: dict) -> dict:
    result = _mcp(client, api_key, name, args)
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _err(client, api_key: str, name: str, args: dict) -> dict:
    result = _mcp(client, api_key, name, args)
    assert result.get("isError") is True, result
    return result["structuredContent"]["error"]


def _key(client, auth, scopes):
    r = client.post("/api/api-keys", json={"name": "fill", "scopes": scopes}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["plaintext"]


# ---- reads of projects that never sent the new fields ---------------------------------
def test_existing_projects_read_as_solid_and_unpatterned(client, auth):
    projects = client.get("/api/projects", headers=auth).json()
    assert projects, "the seed must have a project, or this reads as nobody looked"
    for project in projects:
        assert project["accent2"] is None
        assert project["pattern"] == "none"
        assert project["accent"].startswith("#")


def test_post_and_patch_round_trip_the_fill(client, auth):
    created = client.post("/api/projects", headers=auth, json={
        "name": "Ombre", "accent": "#112233", "accent2": "#AABBCC", "pattern": "chevron",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["accent"] == "#112233"
    assert body["accent2"] == "#aabbcc"
    assert body["pattern"] == "chevron"

    listed = client.get("/api/projects", headers=auth).json()
    match = next(p for p in listed if p["id"] == body["id"])
    assert match["accent2"] == "#aabbcc" and match["pattern"] == "chevron"

    patched = client.patch(f"/api/projects/{body['id']}", headers=auth, json={
        "accent2": "#445566", "pattern": "paisley",
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["accent"] == "#112233"
    assert patched.json()["accent2"] == "#445566"
    assert patched.json()["pattern"] == "paisley"


def test_an_accent2_equal_to_accent_is_stored_as_null(client, auth):
    """The caller is the router: it must hand the pair to the service, which
    collapses the equal colour. A setattr of the raw body would store it."""
    same = client.post("/api/projects", headers=auth, json={
        "name": "Same Colour", "accent": "#112233", "accent2": "#112233", "pattern": "stripes",
    })
    assert same.status_code == 201, same.text
    assert same.json()["accent2"] is None
    assert same.json()["pattern"] == "stripes"

    cased = client.post("/api/projects", headers=auth, json={
        "name": "Same Case", "accent": "#112233", "accent2": "#112233".upper(),
    })
    assert cased.status_code == 201, cased.text
    assert cased.json()["accent2"] is None

    created = client.post("/api/projects", headers=auth, json={
        "name": "Later", "accent": "#112233", "accent2": "#445566", "pattern": "bubbles",
    }).json()
    moved = client.patch(f"/api/projects/{created['id']}", headers=auth,
                         json={"accent": "#445566"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["accent"] == "#445566"
    assert moved.json()["accent2"] is None
    assert moved.json()["pattern"] == "bubbles"

    cleared = client.patch(f"/api/projects/{created['id']}", headers=auth, json={
        "accent": "#112233", "accent2": "#778899",
    })
    assert cleared.json()["accent2"] == "#778899"
    gone = client.patch(f"/api/projects/{created['id']}", headers=auth, json={"accent2": None})
    assert gone.status_code == 200, gone.text
    assert gone.json()["accent2"] is None
    assert gone.json()["accent"] == "#112233"


@pytest.mark.parametrize("body", [
    {"name": "Bad Pattern", "pattern": "plaid"},
    {"name": "Bad Case", "pattern": "Chevron"},
    {"name": "Bad Accent2", "accent2": "red"},
    {"name": "Short", "accent2": "#fff"},
    {"name": "Alpha", "accent2": "#11223344"},
    {"name": "Bad Accent", "accent": "blue"},
])
def test_unknown_pattern_or_non_hex_is_422(client, auth, body):
    r = client.post("/api/projects", headers=auth, json=body)
    assert r.status_code == 422, r.text


def test_patch_refuses_an_unknown_pattern_and_a_null_pattern(client, auth):
    created = client.post("/api/projects", headers=auth, json={"name": "Hold"}).json()
    bad = client.patch(f"/api/projects/{created['id']}", headers=auth, json={"pattern": "plaid"})
    assert bad.status_code == 422, bad.text
    null = client.patch(f"/api/projects/{created['id']}", headers=auth, json={"pattern": None})
    assert null.status_code == 422, null.text
    kept = client.get("/api/projects", headers=auth).json()
    match = next(p for p in kept if p["id"] == created["id"])
    assert match["pattern"] == "none"


def test_mcp_list_projects_returns_both_fields(client, auth):
    key = _key(client, auth, ["read"])
    listed = _ok(client, key, "list_projects", {})["results"]
    assert listed
    for row in listed:
        assert row["accent2"] is None
        assert row["pattern"] == "none"


def test_mcp_create_project_accepts_the_fill_and_collapses_the_default_accent(client, auth):
    key = _key(client, auth, ["read", "write"])
    out = _ok(client, key, "create_project", {
        "name": "Tile Repo", "accent2": "#FF00AA", "pattern": "bubbles",
    })
    assert out["accent2"] == "#ff00aa"
    assert out["pattern"] == "bubbles"
    listed = _ok(client, key, "list_projects", {})["results"]
    match = next(p for p in listed if p["id"] == out["id"])
    assert match["accent2"] == "#ff00aa" and match["pattern"] == "bubbles"

    solid = _ok(client, key, "create_project", {
        "name": "Solid Repo", "accent2": "#c6f24e", "pattern": "none",
    })
    assert solid["accent2"] is None
    assert solid["pattern"] == "none"

    refused = _err(client, key, "create_project", {"name": "Plaid", "pattern": "plaid"})
    assert refused["code"] == "validation"
    bad_hex = _err(client, key, "create_project", {"name": "Named", "accent2": "red"})
    assert bad_hex["code"] == "validation"


# ---- the migration, on a table that already has a row ---------------------------------
def test_revision_is_the_single_head_after_0215():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_BACKEND / "alembic"))
    assert script.get_current_head() == "0216"
    assert script.get_revision("0216").down_revision == "0215"


def test_upgrade_backfills_existing_rows_and_downgrade_removes_the_columns():
    """SQLite DDL, exercised on every run. Postgres runs the same functions
    through Alembic in the test below; this one is the scratch table."""
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE projects (id VARCHAR PRIMARY KEY, name VARCHAR, accent VARCHAR)"))
        conn.execute(text(
            "INSERT INTO projects (id, name, accent) VALUES ('old', 'Pre', '#c6f24e')"))
    mod = _migration()
    _run(eng, mod.upgrade)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT accent, accent2, pattern FROM projects WHERE id = 'old'")).one()
    assert row == ("#c6f24e", None, "none")

    _run(eng, mod.downgrade)
    assert {"accent2", "pattern"}.isdisjoint(
        {c["name"] for c in inspect(eng).get_columns("projects")})

    _run(eng, mod.upgrade)
    with eng.connect() as conn:
        again = conn.execute(text(
            "SELECT accent2, pattern FROM projects WHERE id = 'old'")).one()
    assert again == (None, "none")


@pytest.mark.skipif(_IS_SQLITE, reason="Alembic owns the schema on Postgres only")
def test_postgres_downgrade_and_upgrade_keep_the_row_and_the_defaults(client):
    from alembic import command
    from alembic.config import Config

    from app.migrate import _BACKEND_ROOT

    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))

    names = {c["name"] for c in inspect(engine).get_columns("projects")}
    assert {"accent2", "pattern"} <= names
    with engine.connect() as conn:
        pid, accent = conn.execute(text("SELECT id, accent FROM projects LIMIT 1")).one()

    command.downgrade(cfg, "0215")
    assert {"accent2", "pattern"}.isdisjoint(
        {c["name"] for c in inspect(engine).get_columns("projects")})

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT accent, accent2, pattern FROM projects WHERE id = :i"), {"i": pid}).one()
    assert row == (accent, None, "none")
