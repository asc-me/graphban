"""The harness's own invariant: every test starts against an empty database.

The schema is built once per session now, so the thing that used to enforce isolation — the
schema being dropped between tests — is gone. What replaces it is an autouse reset, and an
invariant with no test is a convention. These two tests run in order and the second is the
guard: without the reset it would see the rows the first one seeded.

Deliberately does NOT use `client`. That is the whole point — the reset has to happen for a
test that never asks for it, because such a test used to fail loudly (no tables) and would
now quietly read stale data.
"""
from sqlalchemy import func, select

from app.models import User


def _user_count() -> int:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return db.scalar(select(func.count()).select_from(User)) or 0
    finally:
        db.close()


def test_a_client_test_leaves_rows_behind(client):
    """Sets the stage: the app's lifespan seeds on startup, so rows exist after this."""
    assert _user_count() > 0, "the seed should have run"


def test_the_next_test_starts_clean_without_asking_for_client():
    """THE guard, and it only means anything because the test above ran first and seeded.

    If the autouse reset is removed this reads the previous test's users and fails, which
    is exactly the silent staleness it exists to prevent.
    """
    assert _user_count() == 0
