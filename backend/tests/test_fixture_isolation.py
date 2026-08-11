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


# ---- one database per xdist worker (GRPH-353) ------------------------------------------------
# The suite empties every table between tests, so workers sharing a database would truncate
# each other's rows mid-test — and the failure would land on whichever test happened to be
# reading at the time, nowhere near the cause.
import os

import pytest

from tests.dbnames import worker_url  # NOT from conftest — it rewrites env on import


@pytest.mark.parametrize("url,expected", [
    ("sqlite:///./.pytest.db", "sqlite:///./.pytest_gw3.db"),
    ("postgresql+psycopg://u:p@localhost:5432/graphban_test",
     "postgresql+psycopg://u:p@localhost:5432/graphban_test_gw3"),
])
def test_a_worker_gets_a_database_of_its_own(url, expected):
    assert worker_url(url, "gw3") == expected


def test_two_workers_never_share_one():
    """The property that matters, stated directly rather than inferred from the examples."""
    url = "postgresql+psycopg://u:p@localhost:5432/graphban_test"
    assert len({worker_url(url, f"gw{i}") for i in range(8)}) == 8


def test_this_worker_is_actually_using_its_own_database():
    """The seam, checked at runtime. The derivation above can be perfect while nothing wires
    it to the engine — which is how both ends of a seam pass and the middle is missing.

    Skips rather than passes on a serial run: there is no worker, so there is nothing to
    assert, and a silent pass would report coverage this run did not have.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        pytest.skip("serial run — no worker to isolate")

    from app.db import engine

    url = str(engine.url)
    assert url.endswith(worker) or url.endswith(f"{worker}.db"), \
        f"{url} is not {worker}'s database"
    # Exactly once. A conftest re-import applied the suffix TWICE and produced
    # `graphban_test_gw0_gw0`; nothing failed, because the engine was already bound to the
    # first name, so the only evidence was stray databases nobody looked at.
    assert url.count(worker) == 1, f"{url} has the worker id more than once"
