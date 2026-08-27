"""Alembic environment. URL and metadata come from the app itself."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.db import Base
import app.models  # noqa: F401  (register all tables on Base.metadata)

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    # `disable_existing_loggers=False` is load-bearing, not tidiness (GRPH-525).
    #
    # `fileConfig` defaults to True, which sets `disabled = True` on every logger that already
    # exists. On Postgres — which is what production runs — `run_migrations()` executes inside
    # `lifespan` AFTER the app's modules have been imported, so this silenced ten of the twelve
    # `graphban.*` loggers for the entire life of the process: `graphban.main`,
    # `graphban.platform`, `graphban.credential_retry`, `graphban.mcp`, `graphban.events`,
    # `graphban.email`, `graphban.ratelimit`, and more.
    #
    # What that cost: the credential retry loop's "pass failed; continuing" warning could never
    # appear in production, so the background task added in PRD-25 S2b would have failed in
    # exactly the silence its own error handling was written to prevent. SQLite installs use
    # `create_all` and never call this, which is why every test suite and every local run looked
    # fine — the one engine that mattered was the one nobody could observe.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
