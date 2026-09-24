"""GRPH-922: PRD list recency comes from updated_at, not a stored English string."""

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


def test_list_summary_carries_updated_at_not_a_display_string(client, auth):
    prds = client.get("/api/prds?project_id=core", headers=auth).json()
    assert prds
    for row in prds:
        assert "updated_at" in row
        assert row["updated_at"]
        assert "updated" not in row


def test_an_untouched_prd_does_not_read_as_just_now(client, auth, db):
    listed = client.get("/api/prds?project_id=core", headers=auth).json()
    prd_id = listed[0]["id"]
    prd = db.get(Prd, keys.resolve_prd(db, prd_id) or prd_id)
    prd.updated = "just now"
    prd.updated_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    db.commit()

    row = next(p for p in client.get("/api/prds?project_id=core", headers=auth).json() if p["id"] == prd_id)
    assert row["updated_at"].startswith("20")
    assert "updated" not in row
