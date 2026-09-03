"""Engine and session lifecycle.

SQLite remains the default so the service runs with no infrastructure at all,
and Postgres is a `DATABASE_URL` change: the data access layer is plain
SQLAlchemy 2.0 with no backend-specific SQL. The only conditional code is the
pragma listener below, which no-ops elsewhere.

Schema creation is deliberately *not* automatic. Three schema additions were
made against a database that outlived them during development, and each needed
manual repair, because `create_all` adds tables but never alters existing ones.
On ephemeral storage that was survivable; on a database that persists it is a
quiet way to corrupt production. Alembic owns the schema now, and `init_db`
exists only for tests and local convenience.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_settings = get_settings()

_is_sqlite = _settings.database_url.startswith("sqlite")

if _is_sqlite:
    # FastAPI serves requests from a threadpool; SQLite needs to be told that is fine.
    _engine_kwargs: dict = {
        "connect_args": {"check_same_thread": False},
    }
else:
    # Neon and most managed Postgres scale connections to zero and recycle them
    # aggressively. A small pool with recycling avoids handing out a connection
    # the server has already dropped, which otherwise surfaces as an
    # intermittent failure on the first call after an idle period.
    _engine_kwargs = {
        "pool_size": 5,
        "max_overflow": 5,
        "pool_recycle": 300,
        # Serverless Postgres cold-starts. A bounded connect timeout fails
        # cleanly instead of hanging past the voice platform's tool timeout.
        "connect_args": {"connect_timeout": 10},
    }

engine: Engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
    **_engine_kwargs,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Enable foreign keys and WAL on SQLite; no-op for other backends."""
    if "sqlite3" not in type(dbapi_connection).__module__:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def init_db() -> None:
    """Create tables directly from the models.

    For tests and local scratch databases only. Deployed environments run
    `alembic upgrade head`, which is the only path that can alter an existing
    schema rather than silently leaving it stale.
    """
    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a transactional session."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
