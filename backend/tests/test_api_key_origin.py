"""`api_keys.origin` (GRPH-1310): accepted on create, returned on list, backfilled by shape."""
import importlib.util
import pathlib

import sqlalchemy as sa


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


def test_backfill_classifies_by_shape(client, auth):
    """Runs on the suite's engine, so on Postgres it reads a real JSON column."""
    from app.db import SessionLocal, engine
    from app.models import ApiKey, User

    pid = _proj(client, auth, "BackfillP")
    db = SessionLocal()
    try:
        uid = db.query(User).first().id
        shapes = {
            "wave": ("fleet worker", ["read", "write"], "w1"),
            "gate": (f"{pid} swamp gate", ["read", "write", "gate"], None),
            "deleg": (f"{pid} delegation", ["read", "write"], None),
            "plain": ("my key", ["read", "write"], None),
            "synclike": (f"{pid} delegation", ["read", "sync"], None),
        }
        for kid, (name, scopes, wave) in shapes.items():
            db.add(ApiKey(id=f"bf-{kid}", user_id=uid, name=name, prefix=kid, hashed_key=f"h-{kid}",
                          scopes=scopes, project_id=pid, fleet_wave=wave, origin=None))
        db.commit()
    finally:
        db.close()
    with engine.begin() as conn:
        _migration().backfill(conn)
        got = dict(conn.execute(
            sa.text("select id, origin from api_keys where id like 'bf-%'")).fetchall())
    assert got == {"bf-wave": "fleet", "bf-gate": "gban_swamp_setup", "bf-deleg": "gban_setup",
                   "bf-plain": None, "bf-synclike": None}
