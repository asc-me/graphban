"""Run Alembic migrations programmatically at startup (Postgres path)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _config() -> Config:
    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    # We are inside the app's lifespan and logging is already configured. Without this,
    # env.py's `fileConfig` would disable every app logger and swap the JSON handler for
    # alembic.ini's plain console one — silencing the access log and the security warnings
    # on exactly the deployments that run migrations. See the comment in alembic/env.py.
    cfg.attributes["configure_logger"] = False
    return cfg


@lru_cache(maxsize=1)
def _script_heads() -> tuple[str, ...]:
    """The revision(s) the migration scripts end at. The scripts ship with the process and
    cannot change under it, so this is read once — parsing ~160 revision files is most of
    what `alembic upgrade head` costs when there is nothing to do."""
    return tuple(sorted(ScriptDirectory.from_config(_config()).get_heads()))


def _database_heads() -> tuple[str, ...] | None:
    """What `alembic_version` says, or None when the table is absent or unreadable — a
    database Alembic has never touched, which the full upgrade must handle."""
    from sqlalchemy import text

    from app.db import engine

    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT version_num FROM alembic_version")).all()
    except Exception:  # noqa: BLE001 — absent table, empty database: the full path decides
        return None
    return tuple(sorted(r[0] for r in rows))


def run_migrations() -> None:
    # Fast path: the database already says it is at the scripts' head, so `upgrade head`
    # would apply nothing. Skipping it saves ~50 ms per app start, which the test suite pays
    # on every `TestClient` (measured 2026-11-01). Anything short of an exact match — behind,
    # ahead, branched, unreadable — still takes the full upgrade, which is the authority.
    if _database_heads() != _script_heads():
        command.upgrade(_config(), "head")

    # Vector widths track EMBED_DIM, which is CONFIG — it changes independently of schema
    # revisions, so a migration can't own it. Alembic stamps a revision applied and never
    # re-runs it, so migration 0037 converged once (at the then-current EMBED_DIM) and was
    # skipped on every later boot, leaving the schema behind the configured embedder.
    # Runs on every startup instead; a no-op when the widths already agree.
    from app.vector_schema import converge_all

    converge_all()
