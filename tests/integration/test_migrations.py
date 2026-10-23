import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401
from app.db.base import Base
from app.db.migrate import alembic_config, is_at_head, upgrade_to_head
from app.db.session import make_engine
from app.settings import get_settings

EXPECTED_TABLES = {
    "users",
    "households",
    "household_memberships",
    "agents",
    "conversations",
    "conversation_participants",
    "messages",
    "tasks",
    "approvals",
    "activity_events",
    "provider_bindings",
    "webhook_events",
    "jobs",
    "browser_tasks",
    "trusted_contacts",
}


def _wipe(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def migration_engine(request: pytest.FixtureRequest, tmp_path: Path, settings_env: object) -> Iterator[Engine]:
    if request.param == "sqlite":
        engine = make_engine(f"sqlite:///{tmp_path / 'migrations.db'}")
    else:
        url = os.environ.get("TEST_DATABASE_URL", "")
        if not url.startswith("postgresql"):
            pytest.skip("TEST_DATABASE_URL is not a Postgres URL")
        engine = make_engine(url)
        _wipe(engine)
    yield engine
    if request.param == "postgres":
        _wipe(engine)
    engine.dispose()


def test_upgrade_head_creates_all_tables(migration_engine: Engine) -> None:
    upgrade_to_head(migration_engine)
    assert set(inspect(migration_engine).get_table_names()) == EXPECTED_TABLES | {"alembic_version"}
    assert is_at_head(migration_engine)


def test_autogenerate_diff_empty(migration_engine: Engine) -> None:
    upgrade_to_head(migration_engine)
    with migration_engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_downgrade_base_drops_everything(migration_engine: Engine) -> None:
    upgrade_to_head(migration_engine)
    with migration_engine.begin() as conn:
        config = alembic_config()
        config.attributes["connection"] = conn
        command.downgrade(config, "base")
    assert set(inspect(migration_engine).get_table_names()) <= {"alembic_version"}
    assert not is_at_head(migration_engine)


def test_is_at_head_false_without_alembic_version(engine: Engine) -> None:
    assert is_at_head(engine) is False


def test_upgrade_without_engine_uses_settings_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings_env: object
) -> None:
    url = f"sqlite:///{tmp_path / 'from-settings.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    upgrade_to_head()
    engine = make_engine(url)
    try:
        assert is_at_head(engine)
        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()


def test_migrations_database_url_wins_over_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings_env: object
) -> None:
    """Neon: the app uses the pooled host, DDL goes to the direct one (spec section 20)."""
    pooled, direct = tmp_path / "app-pooled.db", tmp_path / "migrations-direct.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{pooled}")
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", f"sqlite:///{direct}")
    get_settings.cache_clear()
    upgrade_to_head()
    engine = make_engine(f"sqlite:///{direct}")
    try:
        assert is_at_head(engine) and set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()
    assert not pooled.exists()  # the pooled URL was never opened


def test_blank_migrations_database_url_falls_back_to_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings_env: object
) -> None:
    url = f"sqlite:///{tmp_path / 'fallback.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", "")
    get_settings.cache_clear()
    upgrade_to_head()
    engine = make_engine(url)
    try:
        assert is_at_head(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize("scheme", ["postgres://", "postgresql://"])
def test_hosted_postgres_urls_get_the_psycopg3_driver(
    monkeypatch: pytest.MonkeyPatch, settings_env: object, scheme: str
) -> None:
    """Neon and Manufact hand out plain postgres(ql):// URLs; both database settings are normalised."""
    monkeypatch.setenv("DATABASE_URL", f"{scheme}u:p@pooler.example/db")
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", f"{scheme}u:p@direct.example/db")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.database_url == "postgresql+psycopg://u:p@pooler.example/db"
    assert settings.migrations_database_url == "postgresql+psycopg://u:p@direct.example/db"


# --- 0002: channels (addendum 21.7) -----------------------------------------------------------------------------


def _seed_household_and_users(engine: Engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """(household id, user id, other user id) inserted through the table objects (column defaults fill the rest)."""
    household, alex, katie = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tables = Base.metadata.tables
    with engine.begin() as conn:
        conn.execute(
            tables["households"].insert().values(id=household, name="H", slug="h", timezone="UTC", settings={})
        )
        for user_id, handle in ((alex, "u0"), (katie, "u1")):
            conn.execute(tables["users"].insert().values(id=user_id, handle=handle, display_name=handle, is_demo=True))
    return household, alex, katie


def _insert_binding(
    engine: Engine, household: uuid.UUID, user: uuid.UUID | None, external_id: str, binding_type: str
) -> None:
    with engine.begin() as conn:
        conn.execute(
            Base.metadata.tables["provider_bindings"]
            .insert()
            .values(
                id=uuid.uuid4(),
                household_id=household,
                user_id=user,
                provider="keiki",
                binding_type=binding_type,
                external_id=external_id,
                status="active",
                config={},
            )
        )


def test_migration_0002_upgrade_downgrade(migration_engine: Engine) -> None:
    upgrade_to_head(migration_engine)
    inspector = inspect(migration_engine)
    assert "assignee_user_id" in {c["name"] for c in inspector.get_columns("tasks")}
    assignee = next(c for c in inspector.get_columns("tasks") if c["name"] == "assignee_user_id")
    assert assignee["nullable"] is True
    indexes = {i["name"]: i for i in inspector.get_indexes("provider_bindings")}
    unique = indexes["uq_provider_bindings_provider_type_external"]
    assert unique["unique"] and list(unique["column_names"]) == ["provider", "binding_type", "external_id"]

    with migration_engine.begin() as conn:
        config = alembic_config()
        config.attributes["connection"] = conn
        command.downgrade(config, "0001")
    inspector = inspect(migration_engine)
    assert "assignee_user_id" not in {c["name"] for c in inspector.get_columns("tasks")}
    assert "uq_provider_bindings_provider_type_external" not in {
        i["name"] for i in inspector.get_indexes("provider_bindings")
    }
    upgrade_to_head(migration_engine)  # and back up again
    assert is_at_head(migration_engine)


def test_duplicate_binding_external_id_rejected(migration_engine: Engine) -> None:
    upgrade_to_head(migration_engine)
    household, alex, katie = _seed_household_and_users(migration_engine)
    _insert_binding(migration_engine, household, alex, "hash-1", "mcp_token")
    with pytest.raises(IntegrityError):
        _insert_binding(migration_engine, household, katie, "hash-1", "mcp_token")  # same external id, other user
    _insert_binding(migration_engine, household, katie, "hash-1", "inkbox_imessage")  # other binding_type is fine
    _insert_binding(migration_engine, household, katie, "hash-2", "mcp_token")  # other external id is fine


def test_a_pre_0002_database_upgrades_with_existing_rows(migration_engine: Engine) -> None:
    with migration_engine.begin() as conn:
        config = alembic_config()
        config.attributes["connection"] = conn
        command.upgrade(config, "0001")
    household, alex, _ = _seed_household_and_users(migration_engine)
    _insert_binding(migration_engine, household, alex, "hash-1", "mcp_token")
    upgrade_to_head(migration_engine)
    with migration_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM provider_bindings")).scalar_one() == 1
