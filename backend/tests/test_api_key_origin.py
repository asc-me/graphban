"""`api_keys.origin` (GRPH-1310): accepted on create, returned on list, backfilled by shape."""
import importlib.util
import pathlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _proj(client, auth, name="OriginProj"):
    return client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]


def test_origin_round_trips_on_list(client, auth):
    made = client.post("/api/api-keys", json={"name": "a", "origin": "gban_setup"}, headers=auth)
    assert made.status_code == 201 and made.json()["origin"] == "gban_setup"
    rows = client.get("/api/api-keys", headers=auth).json()
    assert [r["origin"] for r in rows if r["id"] == made.json()["id"]] == ["gban_setup"]


def test_unknown_origin_is_422(client, auth):
    r = client.post("/api/api-keys", json={"name": "a", "origin": "somewhere"}, headers=auth)
    assert r.status_code == 422


def test_no_origin_is_null_not_web(client, auth):
    r = client.post("/api/api-keys", json={"name": "a"}, headers=auth)
    assert r.json()["origin"] is None


def test_fleet_wave_mint_stores_fleet(client, auth):
    from app.db import SessionLocal
    from app.models import User
    from app.services import fleet as fleet_svc

    pid = _proj(client, auth, "WaveOrigin")
    db = SessionLocal()
    try:
        uid = db.query(User).first().id
        row, _ = fleet_svc.mint_fleet_key(db, user_id=uid, project_id=pid, role="worker", wave="W1")
        assert row.fleet_wave and row.origin == "fleet"
    finally:
        db.close()


def _migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "alembic/versions/0177_api_key_origin.py"
    spec = importlib.util.spec_from_file_location("m0177", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_classifies_by_shape():
    eng = sa.create_engine("sqlite://")
    md = sa.MetaData()
    keys = sa.Table(
        "api_keys", md, sa.Column("id", sa.String, primary_key=True), sa.Column("name", sa.String),
        sa.Column("scopes", sa.JSON), sa.Column("project_id", sa.String),
        sa.Column("fleet_wave", sa.String))
    md.create_all(eng)
    with eng.begin() as conn:
        conn.execute(keys.insert(), [
            dict(id="wave", name="fleet worker", scopes=["read", "write"], project_id="p1", fleet_wave="w1"),
            dict(id="gate", name="p1 swamp gate", scopes=["read", "write", "gate"], project_id="p1", fleet_wave=None),
            dict(id="deleg", name="p1 delegation", scopes=["read", "write"], project_id="p1", fleet_wave=None),
            dict(id="plain", name="my key", scopes=["read", "write"], project_id="p1", fleet_wave=None),
            dict(id="synclike", name="p1 delegation", scopes=["read", "sync"], project_id="p1", fleet_wave=None),
        ])
        ctx = MigrationContext.configure(conn)
        mod = _migration()
        with Operations.context(ctx):
            mod.upgrade()
        got = dict(conn.execute(sa.text("select id, origin from api_keys")).fetchall())
    assert got == {"wave": "fleet", "gate": "gban_swamp_setup", "deleg": "gban_setup",
                   "plain": None, "synclike": None}
