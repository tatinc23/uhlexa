"""Programmatic Alembic helpers (the container entrypoint uses the `alembic` CLI with the same env.py)."""

from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine

ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["skip_logging"] = True  # do not reconfigure the host application's logging
    return config


def upgrade_to_head(engine: Engine | None = None) -> None:
    """Upgrade to the latest revision: on `engine` when given, else on DATABASE_URL from settings."""
    config = alembic_config()
    if engine is None:
        command.upgrade(config, "head")
        return
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@lru_cache
def _head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _is_at_head(connection: Connection) -> bool:
    return MigrationContext.configure(connection).get_current_heads() == (_head_revision(),)


def is_at_head(bind: Engine | Connection) -> bool:
    """True when the database's alembic_version equals the newest migration (False for an unmigrated database)."""
    if isinstance(bind, Engine):
        with bind.connect() as connection:
            return _is_at_head(connection)
    return _is_at_head(bind)
