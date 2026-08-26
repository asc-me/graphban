"""Migrations that DELETE rows, run against seeded pre-migration data (GRPH-433).

Nothing in this suite ran `upgrade()` against real rows before this file. That is fine for
a migration that adds a column and not fine for `0080`, which deletes:

    DELETE FROM memberships WHERE id NOT IN (… ROW_NUMBER() OVER (PARTITION BY …) …)

Its own docstring calls the sweep "the whole risk of this migration". It was verified once,
by hand, against a throwaway Postgres — and a hand-verification recorded in a commit message
never runs again.

**A sweep that keeps *a* row looks correct while keeping the wrong one**, so each precedence
rule gets its own case rather than one "duplicates are gone" assertion.

The other half of the risk is duplication of a different kind: the SQL re-states, in another
language, the precedence `teams.recompute` implements in Python. `test_the_sql_and_the_python_agree`
pins them together, because the two can drift the moment the runtime rule changes — with the
SQL frozen inside an applied migration nobody re-reads.

Runs on **Postgres only**, because migrations only run there (SQLite builds from
`create_all`). The skip says so out loud, for the reason GRPH-432 established: a suite that
skips silently reads as green when it ran nothing.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from app.db import engine as app_engine

IS_PG = app_engine.url.drivername.startswith("postgresql")

postgres_only = pytest.mark.skipif(
    not IS_PG,
    reason=(
        "MIGRATION SWEEPS UNVERIFIED ON THIS ENGINE. Alembic only runs on Postgres; SQLite "
        "builds the schema from `create_all`, so 0080's DELETE never executes here. CI runs "
        "these against Postgres."
    ),
)


@pytest.fixture()
def scratch():
    """A throwaway database migrated to 0079 — the state just before the sweep.

    Its own database, not the suite's: bringing the shared one down to 0079 mid-run would
    pull the schema out from under every other test, and the failures would land nowhere
    near the cause.
    """
    from alembic import command
    from alembic.config import Config

    from tests.conftest import _create_database

    base, _, _ = os.environ["DATABASE_URL"].rpartition("/")
    name = f"gb_mig_{uuid.uuid4().hex[:8]}"
    _create_database(f"{base}/postgres", name)
    url = f"{base}/{name}"

    # `alembic/env.py` overrides `sqlalchemy.url` with `settings.database_url`, so setting
    # it on the Config is ignored — the first version of this fixture migrated the SUITE's
    # database instead of the scratch one, and the seeds then failed against a schema that
    # was never built. Patch the setting, which is what env.py actually reads.
    from app.config import settings

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    previous = settings.database_url
    settings.database_url = url
    command.upgrade(cfg, "0079")

    eng = create_engine(url)
    try:
        yield eng, cfg
    finally:
        settings.database_url = previous
        eng.dispose()
        admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            admin.dispose()


def _seed(eng, rows):
    """One user, one project, and the membership rows under test."""
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (id,name,handle,email,password_hash) "
                       "VALUES ('u1','U','u','u@x.io','x')"))
        c.execute(text("INSERT INTO projects (id,name,tag) VALUES ('p1','P','PRJ')"))
        for access, origin in rows:
            c.execute(text("INSERT INTO memberships (user_id,project_id,role,access,origin) "
                           "VALUES ('u1','p1','member',:a,:o)"), {"a": access, "o": origin})


def _survivors(eng):
    with eng.connect() as c:
        return c.execute(text(
            "SELECT access, origin FROM memberships WHERE user_id='u1' ORDER BY id")).all()


def _ids(eng):
    """The membership ids, in the DATABASE's ordering — which is the ordering 0080 ranks by.

    Asked of Postgres rather than sorted in Python: the migration's tie-break is `ORDER BY …,
    id`, and reproducing that with `min()` here would be a second implementation of the
    collation, quietly right until the id type or the collation changed.
    """
    with eng.connect() as c:
        return [r[0] for r in c.execute(text(
            "SELECT id FROM memberships WHERE user_id='u1' ORDER BY id")).all()]


@postgres_only
def test_direct_survives_a_team_row_even_at_lower_access(scratch):
    """Rule 1, and the counter-intuitive one.

    A `direct read` beats a `team write`, so the sweep LOWERS effective access here. That is
    correct rather than regrettable: `teams.recompute` returns a direct row untouched and
    materialises nothing over it, so under D5 the team grant was never in force for this
    person. The sweep converges on the state recompute would produce rather than one
    recompute would immediately undo.
    """
    from alembic import command

    eng, cfg = scratch
    _seed(eng, [("read", "direct"), ("write", "team")])
    command.upgrade(cfg, "0080")

    assert _survivors(eng) == [("read", "direct")]


@postgres_only
def test_the_highest_access_wins_within_one_origin_tier(scratch):
    """Rule 2. Where two TEAM rows disagree, the more permissive survives — matching
    recompute's highest-wins across grants."""
    from alembic import command

    eng, cfg = scratch
    _seed(eng, [("read", "team"), ("write", "team"), ("none", "team")])
    command.upgrade(cfg, "0080")

    assert _survivors(eng) == [("write", "team")]


@postgres_only
def test_identical_rows_collapse_deterministically(scratch):
    """Rule 3, and this test could not fail until it looked at the ID.

    It asserted `len(_survivors(eng)) == 1` — and `_survivors` selects `(access, origin)`,
    which are identical across all three seeded rows. Nothing observed WHICH row survived, so
    the docstring's claim that the lowest id wins was never checked: replacing the `id`
    tie-break in 0080 with `random()` left the whole file green.

    Which is precisely the shape this ticket was filed about — *"a sweep that keeps a row
    looks correct while keeping the wrong one"* — landing on its own fix.

    Determinism is the point rather than tidiness: without a stable tie-break, two runs of the
    same migration against the same data can keep different rows, and a migration that is not
    a function of its input cannot be reasoned about after the fact.
    """
    from alembic import command

    eng, cfg = scratch
    _seed(eng, [("write", "team"), ("write", "team"), ("write", "team")])
    before = _ids(eng)
    assert len(before) == 3, "the seed did not produce three rows to choose between"

    command.upgrade(cfg, "0080")

    assert _ids(eng) == [before[0]], "the LOWEST id survives, not merely one of the three"


@postgres_only
def test_the_constraint_holds_afterwards_and_a_rerun_changes_nothing(scratch):
    """The sweep exists to make the constraint addable. If a duplicate could still be
    written afterwards it bought nothing."""
    from alembic import command
    from sqlalchemy.exc import IntegrityError

    eng, cfg = scratch
    _seed(eng, [("read", "direct"), ("write", "team")])
    command.upgrade(cfg, "0080")

    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(text("INSERT INTO memberships (user_id,project_id,role,access,origin) "
                           "VALUES ('u1','p1','member','write','team')"))

    command.downgrade(cfg, "0079")
    command.upgrade(cfg, "0080")
    assert _survivors(eng) == [("read", "direct")]


@postgres_only
def test_a_table_with_no_duplicates_is_untouched(scratch):
    """The live case. `0079` is what the deployment runs, with 5 memberships and no
    duplicate pairs — so this migration must remove nothing there. Verified rather than
    assumed, because 'it happened to be safe on one dataset' is not a property."""
    from alembic import command

    eng, cfg = scratch
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (id,name,handle,email,password_hash) "
                       "VALUES ('u1','U','u','u@x.io','x')"))
        for pid in ("p1", "p2", "p3"):
            c.execute(text("INSERT INTO projects (id,name,tag) VALUES (:i,:i,:t)"),
                      {"i": pid, "t": pid.upper()})
            c.execute(text("INSERT INTO memberships (user_id,project_id,role,access,origin) "
                           "VALUES ('u1',:i,'member','write','direct')"), {"i": pid})

    command.upgrade(cfg, "0080")
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM memberships")).scalar() == 3


def test_the_sql_and_the_python_agree_on_precedence():
    """Runs on BOTH engines. The sweep re-states in SQL what `teams.recompute` decides in
    Python, and the two can drift the moment the runtime rule changes — with the SQL frozen
    inside an applied migration nobody re-reads.

    Asserted on the source so it fails wherever the suite runs, not only where the migration
    does. `_RANK` is recompute's ordering; the migration's CASE arms must list the same
    access levels in the same order.
    """
    from pathlib import Path

    from app.services.teams import _RANK

    # Same discipline as the lock suite: a Postgres-only file that skips silently reads as
    # green when it ran nothing. This test runs everywhere, so it is where the absence gets
    # said out loud.
    if not IS_PG:
        print(
            "\n  MIGRATION SWEEPS NOT EXERCISED on this engine — alembic only runs on "
            "Postgres. 0080's DELETE was not executed here; its precedence rules are "
            "unverified in this run. CI runs them."
        )

    mig = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
           / "0080_one_membership_per_user_project.py").read_text()

    by_rank = [a for a, _ in sorted(_RANK.items(), key=lambda kv: -kv[1])]

    # The sweep names the top tiers and lets ELSE catch the rest —
    # `CASE access WHEN 'write' THEN 0 WHEN 'read' THEN 1 ELSE 2 END` — which is equivalent
    # to recompute's ranking and a better encoding, since an access level added later falls
    # to the bottom rather than being silently ranked alongside `write`.
    #
    # So the invariant is ORDER, not exhaustiveness: whichever levels the sweep names must
    # appear in descending rank, and every level it omits must be one recompute also ranks
    # lowest. Demanding each level literally is what an earlier version of this test did,
    # and it failed against correct code.
    # The order the SQL actually states, read from the text — not re-derived from `by_rank`,
    # which is what the first version did. `[a for a in by_rank if a in named]` is `named`
    # rebuilt from the same ordering, so it could never differ: the assertion was a
    # tautology and passed with the Python rank inverted.
    at = {a: mig.index(f"WHEN '{a}'") for a in by_rank if f"WHEN '{a}'" in mig}
    sql_order = sorted(at, key=at.get)
    assert sql_order == [a for a in by_rank if a in at], (
        f"recompute ranks access {by_rank}; the sweep states {sql_order}. "
        "Two statements of one rule have drifted."
    )
    named = sql_order
    omitted = [a for a in by_rank if a not in named]
    assert omitted == by_rank[len(named):], (
        f"the sweep leaves {omitted} to ELSE, but recompute does not rank those lowest — "
        "an omitted level would be swept as though it were the least permissive"
    )
    assert mig.index("'direct'") < mig.index("WHEN 'write'"), (
        "origin must outrank access in the sweep, as it does in recompute — a direct row "
        "wins whatever its access"
    )
