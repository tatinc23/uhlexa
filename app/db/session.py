"""Engine and session plumbing. Services `flush()`; the request handler, MCP tool or job commits."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.settings import get_settings


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Postgres: pre-ping and recycle after 240 s (Neon drops idle connections), and no server-side prepared
    statements (Neon's pooled host is PgBouncer). SQLite: FK enforcement, cross-thread use, and one shared
    connection for in-memory databases so every session sees the same data."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        kwargs.setdefault("pool_pre_ping", True)
        kwargs.setdefault("pool_recycle", 240)
        kwargs.setdefault("pool_size", 5)
        kwargs.setdefault("connect_args", {"prepare_threshold": None, "connect_timeout": 15})
        return create_engine(url, **kwargs)
    kwargs.setdefault("connect_args", {"check_same_thread": False})
    if parsed.database in (None, "", ":memory:"):
        kwargs.setdefault("poolclass", StaticPool)
    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Does not commit: handlers commit explicitly; unfinished work rolls back on close."""
    with SessionLocal() as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success, roll back on error (jobs, seed, lifespan)."""
    with SessionLocal() as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
