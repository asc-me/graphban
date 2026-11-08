"""GRPH-1423: the `plan.exclusive_areas` rule — which file areas are exclusive, per project."""
from __future__ import annotations

import pytest

from app.services import fleet as fleet_svc
from app.services import rule_toggles as T
from app.services import rules as R

RULE = T.EXCLUSIVE_AREAS_RULE
MIGRATION = "backend/alembic/versions/x.py"
OTHER = "db/migrations/0001.sql"


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def library(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="plan", title="Exclusive file areas",
        summary="Paths whose leases are exclusive.",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=True, default_params={"paths": list(T.EXCLUSIVE_AREAS_DEFAULT)}))
    return T.LIBRARY


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _put(client, auth, body, project="core"):
    return client.put(f"/api/rules/toggles/{RULE}", params={"project_id": project},
                      json=body, headers=auth)


def _collides(db, mine, theirs):
    hards = fleet_svc.project_hard_areas(db, "core")
    return bool(fleet_svc.hard_collide([mine], [theirs], hards))


def test_unset_project_keeps_the_migrations_exclusive_and_the_rest_soft(client, library, db):
    assert fleet_svc.project_hard_areas(db, "core") == ("backend/alembic/versions",)
    assert _collides(db, MIGRATION, "backend/alembic/versions/*")
    assert not _collides(db, "backend/app/services/fleet.py", "backend/app/services")
    assert not _collides(db, OTHER, "db/migrations/*")


def test_a_project_path_is_exclusive_and_the_old_default_is_soft(client, library, db):
    auth = _login(client)
    r = _put(client, auth, {"params": {"paths": ["db/migrations/**"]}, "reason": "our migrations"})
    assert r.status_code == 200, r.text
    assert _collides(db, OTHER, "db/migrations/*")
    assert not _collides(db, MIGRATION, "backend/alembic/versions/*")


def test_a_pack_without_the_rule_reads_the_default(client, monkeypatch, db):
    monkeypatch.setattr(T, "LIBRARY", {})
    assert fleet_svc.project_hard_areas(db, "core") == ("backend/alembic/versions",)


@pytest.mark.parametrize("bad", ["db/migrations", [1, 2], ["ok", 3], {"a": 1}])
def test_paths_that_are_not_a_list_of_strings_are_refused(client, library, bad):
    auth = _login(client)
    r = _put(client, auth, {"params": {"paths": bad}, "reason": "x"})
    assert r.status_code == 422, r.text
