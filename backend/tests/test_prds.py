"""GRPH-1217: PRD summaries carry closed_at from the close record, not updated_at."""

import datetime as dt

import pytest

from app.models import Prd
from app.services import keys


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _row(client, auth, prd_id):
    return next(p for p in client.get("/api/prds?project_id=core", headers=auth).json() if p["id"] == prd_id)


def test_closed_at_is_the_close_record_time_not_updated_at(client, auth, db):
    prd_id = client.get("/api/prds?project_id=core", headers=auth).json()[0]["id"]
    prd = db.get(Prd, keys.resolve_prd(db, prd_id) or prd_id)
    closed = dt.datetime(2026, 1, 5, 12, 0, tzinfo=dt.timezone.utc)
    prd.status = "closed"
    prd.close_record = {"closed_at": closed.isoformat()}
    prd.updated_at = dt.datetime.now(dt.timezone.utc)
    db.commit()

    got = dt.datetime.fromisoformat(_row(client, auth, prd_id)["closed_at"])
    assert got == closed
    assert got != prd.updated_at


def test_open_prd_has_null_closed_at(client, auth):
    prds = client.get("/api/prds?project_id=core", headers=auth).json()
    open_rows = [p for p in prds if p["status"] != "closed"]
    assert open_rows
    assert all(p["closed_at"] is None for p in open_rows)


def test_closed_without_close_record_is_null(client, auth, db):
    prd_id = client.get("/api/prds?project_id=core", headers=auth).json()[0]["id"]
    prd = db.get(Prd, keys.resolve_prd(db, prd_id) or prd_id)
    prd.status = "closed"
    prd.close_record = None
    db.commit()
    assert _row(client, auth, prd_id)["closed_at"] is None
