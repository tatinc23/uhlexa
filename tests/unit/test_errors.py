import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.errors import (
    Forbidden,
    InvalidTransition,
    NotFound,
    ProviderUnavailable,
    ServiceError,
    Unauthorized,
    ValidationFailed,
    register_error_handlers,
)


class Body(BaseModel):
    text: str = Field(min_length=1, max_length=5)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/not-found")
    def not_found() -> None:
        raise NotFound("Task not found")

    @app.get("/forbidden")
    def forbidden() -> None:
        raise Forbidden("Children cannot approve")

    @app.get("/conflict")
    def conflict() -> None:
        raise InvalidTransition("Approval already decided")

    @app.get("/invalid")
    def invalid() -> None:
        raise ValidationFailed("No items to add")

    @app.get("/unauthorized")
    def unauthorized() -> None:
        raise Unauthorized("Demo auth is disabled")

    @app.get("/provider")
    def provider() -> None:
        raise ProviderUnavailable("maritime", "disabled")

    @app.get("/custom")
    def custom() -> None:
        raise ServiceError("teapot", "short_and_stout")

    @app.get("/http")
    def http_error() -> None:
        raise HTTPException(status_code=401, detail="Not signed in")

    @app.post("/body")
    def body(payload: Body) -> dict:
        return payload.model_dump()

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internals")

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("path", "status", "code", "detail"),
    [
        ("/not-found", 404, "not_found", "Task not found"),
        ("/forbidden", 403, "forbidden", "Children cannot approve"),
        ("/conflict", 409, "invalid_transition", "Approval already decided"),
        ("/invalid", 422, "validation_failed", "No items to add"),
        ("/unauthorized", 401, "unauthorized", "Demo auth is disabled"),
        ("/http", 401, "http_401", "Not signed in"),
        ("/custom", 500, "short_and_stout", "teapot"),
    ],
)
def test_service_errors_map_to_status_and_body(
    client: TestClient, path: str, status: int, code: str, detail: str
) -> None:
    response = client.get(path)
    assert response.status_code == status
    assert response.json() == {"detail": detail, "code": code}


def test_provider_unavailable_is_503_and_names_the_provider(client: TestClient) -> None:
    response = client.get("/provider")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "maritime unavailable: disabled",
        "code": "provider_unavailable",
        "provider": "maritime",
        "reason": "disabled",
    }


def test_default_messages() -> None:
    assert NotFound().message == "Not found"
    assert str(Forbidden()) == "Forbidden"


def test_request_validation_uses_the_same_envelope(client: TestClient) -> None:
    response = client.post("/body", json={"text": "toolong"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["detail"], str)
    assert body["errors"][0]["loc"] == ["body", "text"]
    assert "input" not in body["errors"][0]


def test_unknown_route_uses_the_envelope(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found", "code": "http_404"}


def test_unhandled_exception_does_not_leak_internals(client: TestClient) -> None:
    response = client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}
