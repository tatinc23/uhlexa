from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import create_app


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_does_not_touch_the_database(settings_env: object) -> None:
    app = create_app()

    def boom() -> None:
        raise AssertionError("/health must not open a DB session")

    app.dependency_overrides[get_db] = boom
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


def test_ready_ok_on_migrated_db(migrated_engine: Engine) -> None:
    app = create_app()
    with Session(migrated_engine) as session:
        app.dependency_overrides[get_db] = lambda: session
        with TestClient(app) as c:
            response = c.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "db": "ok", "migrations": "head"}


def test_ready_503_when_schema_is_not_migrated(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "db": "ok", "migrations": "behind"}


def test_ready_503_when_database_is_down(settings_env: object) -> None:
    app = create_app()

    class DeadSession:
        def connection(self) -> None:
            raise RuntimeError("db down")

    app.dependency_overrides[get_db] = lambda: DeadSession()
    with TestClient(app) as c:
        response = c.get("/ready")
    assert response.status_code == 503
    assert response.json()["db"] == "error"
