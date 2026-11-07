"""SQLAlchemy engine / session management (synchronous, pooled)."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine(database: str | None = None) -> Engine:
    """Create (or recreate) the global engine. `database` overrides the DB name (tests)."""
    global _engine, _SessionLocal
    settings = get_settings()
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(
        settings.database_url(database),
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=10,
        max_overflow=20,
        pool_timeout=10,
        isolation_level="READ COMMITTED",
        connect_args={"connect_timeout": 10, "init_command": "SET time_zone = '+00:00'"},
    )

    @event.listens_for(_engine, "connect")
    def _set_session_modes(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("SET SESSION sql_mode = 'STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO'")
        cur.close()

    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request.

    Handlers commit explicitly *before* returning the response. (Committing in the
    dependency's exit code would run after the response is sent, so a redirect could
    race the commit and a failed commit would never reach the user.)
    """
    db = session_factory()()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
