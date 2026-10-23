"""Service-layer errors and their HTTP mapping. Every error body is `{"detail": str, "code": str}` (spec section 14)."""

import http
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ServiceError(Exception):
    """Base for errors services raise. Routers and MCP tools never build HTTP errors by hand."""

    status_code = 500
    code = "service_error"
    default_message = "Service error"

    def __init__(self, message: str | None = None, code: str | None = None) -> None:
        self.message = message or self.default_message
        self.code = code or self.code
        super().__init__(self.message)

    def body(self) -> dict[str, Any]:
        return {"detail": self.message, "code": self.code}


class Unauthorized(ServiceError):
    status_code = 401
    code = "unauthorized"
    default_message = "Not signed in"


class NotFound(ServiceError):
    """Also what a foreign-household id returns, so existence never leaks."""

    status_code = 404
    code = "not_found"
    default_message = "Not found"


class Forbidden(ServiceError):
    status_code = 403
    code = "forbidden"
    default_message = "Forbidden"


class InvalidTransition(ServiceError):
    status_code = 409
    code = "invalid_transition"
    default_message = "Invalid state transition"


class ValidationFailed(ServiceError):
    status_code = 422
    code = "validation_failed"
    default_message = "Validation failed"


class ProviderUnavailable(ServiceError):
    status_code = 503
    code = "provider_unavailable"

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider} unavailable: {reason}")

    def body(self) -> dict[str, Any]:
        return {**super().body(), "provider": self.provider, "reason": self.reason}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        if isinstance(exc, ProviderUnavailable):
            logger.warning("provider unavailable: %s: %s", exc.provider, exc.reason)
        return JSONResponse(exc.body(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # `input` and `ctx` are dropped: they echo request data and can hold non-serialisable objects.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        body = {"detail": "Request validation failed", "code": "validation_error", "errors": errors}
        return JSONResponse(body, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else http.HTTPStatus(exc.status_code).phrase
        body = {"detail": detail, "code": f"http_{exc.status_code}"}
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error: %s", type(exc).__name__, exc_info=exc)
        return JSONResponse({"detail": "Internal server error", "code": "internal_error"}, status_code=500)
