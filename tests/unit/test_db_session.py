"""Engine options (spec section 20, "Neon + SQLAlchemy"): Postgres is pooled through PgBouncer and drops idle
connections; SQLite (the test database) gets neither."""

import pytest
from sqlalchemy import Engine

from app.db import session

POSTGRES_URL = "postgresql+psycopg://user:pw@ep-test-pooler.example.neon.tech/db?sslmode=require"


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Record what `make_engine` hands to SQLAlchemy, then build the real engine (nothing connects until first use)."""
    calls: list[tuple[str, dict]] = []
    real = session.create_engine

    def spy(url: str, **kwargs: object) -> Engine:
        calls.append((url, kwargs))
        return real(url, **kwargs)

    monkeypatch.setattr(session, "create_engine", spy)
    return calls


def test_postgres_engine_options(created: list[tuple[str, dict]]) -> None:
    engine = session.make_engine(POSTGRES_URL)
    try:
        ((url, kwargs),) = created
        assert url == POSTGRES_URL
        assert kwargs == {
            "pool_pre_ping": True,  # a dropped idle connection is replaced before it is handed out
            "pool_recycle": 240,  # Neon closes idle connections after a few minutes
            "pool_size": 5,
            "connect_args": {"prepare_threshold": None, "connect_timeout": 15},  # PgBouncer: no prepared statements
        }
        assert engine.dialect.name == "postgresql"
    finally:
        engine.dispose()


def test_postgres_options_can_be_overridden_by_the_caller(created: list[tuple[str, dict]]) -> None:
    engine = session.make_engine(POSTGRES_URL, pool_size=2, pool_recycle=60, connect_args={"connect_timeout": 3})
    try:
        ((_, kwargs),) = created
        assert (kwargs["pool_size"], kwargs["pool_recycle"], kwargs["pool_pre_ping"]) == (2, 60, True)
        assert kwargs["connect_args"] == {"connect_timeout": 3}
    finally:
        engine.dispose()


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:", "sqlite:////tmp/uhlexa-test.db"])
def test_sqlite_gets_none_of_the_postgres_options(created: list[tuple[str, dict]], url: str) -> None:
    engine = session.make_engine(url)
    try:
        ((_, kwargs),) = created
        assert not {"pool_pre_ping", "pool_recycle", "pool_size"} & set(kwargs)
        assert kwargs["connect_args"] == {"check_same_thread": False}
    finally:
        engine.dispose()


def test_in_memory_sqlite_shares_one_connection_so_every_session_sees_the_same_data() -> None:
    engine = session.make_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE probe (n INTEGER)")
            conn.exec_driver_sql("INSERT INTO probe VALUES (1)")
        with engine.connect() as other:
            assert other.exec_driver_sql("SELECT n FROM probe").scalar_one() == 1
            assert other.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        engine.dispose()
