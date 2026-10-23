"""Shared fixtures.

DATABASE_URL is forced to an empty in-memory SQLite before the app is imported, so nothing in a test can reach a real
database through the module-level `SessionLocal`. Set TEST_DATABASE_URL to rerun the suite on Postgres.
"""

import os
from collections.abc import Callable, Iterator

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite://"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.auth.demo_auth import USER_HEADER, Principal, resolve_principal
from app.db.base import Base
from app.db.migrate import upgrade_to_head
from app.db.session import get_db, make_engine
from app.main import create_app
from app.providers import reset_runtime_state
from app.seed import seed_demo
from app.settings import Settings, get_settings
from tests import network_guard

PROVIDER_FLAGS = (
    "ENABLE_PRIMITIVE",
    "ENABLE_SUBQ_SNAP",
    "ENABLE_MARITIME",
    "ENABLE_KEIKI",
    "ENABLE_INKBOX",
    "ENABLE_RING",
    "ENABLE_FIRETV",
    "ENABLE_INKBOX_IMESSAGE",
    "ENABLE_INKBOX_SMS",
    "ENABLE_INKBOX_EMAIL",
    "ENABLE_INKBOX_CALLS",
)
# Blank (not unset) so a developer's .env cannot leak a key into a test run.
BLANKED = (
    "PRIMITIVE_API_KEY",
    "PRIMITIVE_API_BASE_URL",
    "PRIMITIVE_WEBHOOK_SECRET",
    "SUBQ_API_KEY",
    "MARITIME_COMPUTERS_KEY",
    "MARITIME_SAFE_TARGET_URL",
    "SECRET_ENCRYPTION_KEY",
    "MIGRATIONS_DATABASE_URL",
    "KEIKI_GATEWAY_KEY",
    "KEIKI_AGENT_IDS",
    "KEIKI_API_KEY",
    "INKBOX_API_KEY",
    "INKBOX_SIGNING_KEYS",
    "INKBOX_WEBHOOK_AUTH_TOKEN",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "RUNWAYML_API_SECRET",
    "DEMO_ACCESS_TOKEN",
)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def network_guard_attempts(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """No test may reach a non-local host: provider HTTP is always faked (`tests.network_guard`).

    The attempt raises at the call site, and is also recorded and re-raised here at teardown, because product code
    turns transport errors into `ProviderUnavailable` and would otherwise let a test pass by accident. A test that
    deliberately provokes the guard consumes its own record with `network_guard_attempts.clear()`."""
    attempts = network_guard.install(monkeypatch)
    yield attempts
    if attempts:
        pytest.fail(
            "the test tried to reach the network (blocked): " + ", ".join(dict.fromkeys(attempts)), pytrace=False
        )


@pytest.fixture(autouse=True)
def _fresh_provider_runtime_state() -> Iterator[None]:
    """`unavailable` is a process-wide overlay; a failure injected by one test must not leak into the next."""
    reset_runtime_state()
    yield
    reset_runtime_state()


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Hermetic settings: test env, no seed, every provider flag off and every credential blank."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("SEED_DEMO_DATA", "false")
    monkeypatch.setenv("EMBEDDED_WORKER", "false")
    monkeypatch.setenv("ENABLE_DEMO_AUTH", "true")
    monkeypatch.setenv("ENABLE_APPROVALS", "true")
    monkeypatch.setenv("HANDOFF_ACCEPT_DELAY_SECONDS", "0")
    for name in PROVIDER_FLAGS:
        monkeypatch.setenv(name, "false")
    for name in BLANKED:
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("WORKER_IDLE_SECONDS", raising=False)  # a float field: unset, not blank
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def engine(settings_env: Settings) -> Iterator[Engine]:
    """Fresh schema per test: in-memory SQLite, or TEST_DATABASE_URL (Postgres) when set."""
    eng = make_engine(os.environ.get("TEST_DATABASE_URL") or "sqlite://")
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    """TestClient over create_app() with get_db pinned to the test session; runs the app lifespan."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def migrated_engine(settings_env: Settings) -> Iterator[Engine]:
    """In-memory SQLite upgraded with Alembic (so alembic_version exists), unlike `engine`'s create_all."""
    eng = make_engine("sqlite://")
    upgrade_to_head(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def seeded(db: Session) -> dict[str, Principal]:
    """The Parker household (spec section 7), committed, as `{"alex", "katie", "max"}` principals."""
    seed_demo(db)
    db.commit()
    principals: dict[str, Principal] = {}
    for handle in ("alex", "katie", "max"):
        principal = resolve_principal(db, handle)
        assert principal is not None, handle
        principals[handle] = principal
    return principals


@pytest.fixture
def as_user() -> Callable[[str], dict[str, str]]:
    """`client.get(url, headers=as_user("max"))`. A cookie from `/demo/login` outranks this header (spec section 8)."""

    def headers(handle: str) -> dict[str, str]:
        return {USER_HEADER: handle}

    return headers
