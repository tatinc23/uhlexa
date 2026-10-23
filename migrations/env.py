"""Alembic environment. The URL comes from MIGRATIONS_DATABASE_URL, else DATABASE_URL (app.settings), unless a live
connection is passed in via `config.attributes["connection"]` (app.db.migrate and the tests do that)."""

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, create_engine, pool

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base, UTCDateTime
from app.settings import get_settings

config = context.config
if config.config_file_name and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Keep migrations free of app imports: UTCDateTime is a plain tz-aware DateTime at the DDL level."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _database_url() -> str:
    """MIGRATIONS_DATABASE_URL (Neon's direct host: DDL does not go through PgBouncer) when set, else DATABASE_URL."""
    settings = get_settings()
    return settings.migrations_database_url or settings.database_url


def _configure(**kwargs: Any) -> None:
    context.configure(target_metadata=target_metadata, compare_type=True, render_item=render_item, **kwargs)


def _run(connection: Connection) -> None:
    _configure(connection=connection, render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url") or _database_url()
    _configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_engine(config.get_main_option("sqlalchemy.url") or _database_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
