"""Alembic environment. URL and metadata come from the app itself."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.db import Base
import app.models  # noqa: F401  (register all tables on Base.metadata)

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

# TWO LAYERS, because they stop different halves of the same accident. `run_migrations()`
# executes inside `lifespan` on Postgres — which is what production runs — AFTER the app has
# imported its modules and called `configure_logging()`. SQLite installs use `create_all` and
# never reach here, which is why every local run and the SQLite CI job look fine either way.
#
# LAYER 1 — `disable_existing_loggers=False` (GRPH-525). The default is True, which sets
# `disabled = True` on every logger that already exists. That silenced ten of the twelve
# `graphban.*` loggers for the life of the process, including the credential retry loop's
# "pass failed; continuing" warning — so the background task added in PRD-25 S2b would have
# failed in exactly the silence its own error handling was written to prevent.
#
# LAYER 2 — skip `fileConfig` altogether when the app embeds us (GRPH-33). Layer 1 alone is
# not enough, measured: with `disable_existing_loggers=False` the loggers stay enabled, but
# `fileConfig` still applies `[logger_root]`, which sets **level = WARNING** and swaps root's
# handler for alembic.ini's plain `generic` console handler. So after migrations every INFO
# record is dropped — the per-request access log, `graphban.main`'s "credential retry: N
# attempt(s)", the seed line — and whatever still passes comes out as plain text on a stream
# that LOG_JSON=true promised would be JSON.
#
#     start      : disabled=False formatter=['_JsonFormatter']
#     layer 1    : disabled=False formatter=['Formatter']   <- level also raised to WARNING
#
# `app/migrate.py` sets the attribute. The default keeps `alembic upgrade head` on the command
# line configuring its own logging, which is the one case where alembic SHOULD own the stream.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
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
