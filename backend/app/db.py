from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

connect_args = {"check_same_thread": False} if settings.is_sqlite else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# SQLite ships with foreign keys OFF, so it accepts writes Postgres refuses — an orphaned
# child, a parent deleted out from under one. Production is Postgres, so every such bug is
# invisible on the fast engine and fatal on the real one. D5 shipped exactly that: deleting
# a team before its members passed here and raised a ForeignKeyViolation there.
#
# Turning it on makes the two engines agree about what is a legal write, which is the whole
# point of running the suite twice.
if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_enforce_foreign_keys(dbapi_connection, _record):  # pragma: no cover - wiring
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Base(DeclarativeBase):
    pass


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create the pgvector extension (Postgres only) and all tables."""
    # Import models so they are registered on Base.metadata.
    from app import models  # noqa: F401

    if not settings.is_sqlite:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()

    Base.metadata.create_all(bind=engine)
