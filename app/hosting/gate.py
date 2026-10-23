"""Access gate for the public tunnel URL (docs/HOSTING.md).

Demo auth lets anyone who reaches the app act as any family member, which is fine on localhost and not on a public
URL. Behind the tunnel, browser and API routes need an access cookie from `/access?token=<DEMO_ACCESS_TOKEN>`.
Machine routes stay open because they carry their own auth: `/webhooks/*` and `/ws/phone/*` (Inkbox/Primitive
signatures) and the health checks. `/mcp` is open only to requests with `Authorization: Bearer` (Keiki agent tokens,
which the MCP layer verifies with no fallback); without one it needs the access cookie, because the MCP layer would
otherwise fall back to spoofable demo auth. No token configured ⇒ gated routes answer 503.
"""

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

ACCESS_COOKIE = "uhlexa_access"
COOKIE_MAX_AGE_S = 7 * 24 * 3600
DEFAULT_NEXT = "/voice/hub.html"
OPEN_PATHS = ("/health", "/ready")
OPEN_PREFIXES = ("/webhooks/", "/ws/phone/")
BEARER_PATH = "/mcp"
_DENIED_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Uhlexa</title>"
    b"<body style='font-family:system-ui;background:#0f1020;color:#eee;padding:2rem'>"
    b"<h1>Uhlexa</h1><p>This demo needs its access link. Ask Ken for it.</p></body>"
)

Scope = dict[str, Any]


def _cookie_value(token: str) -> str:
    """The cookie never holds the token itself."""
    return hmac.new(token.encode(), b"uhlexa-access-v1", hashlib.sha256).hexdigest()


def _safe_next(target: str | None) -> str:
    """A same-site path only. Browsers drop tabs and newlines and read `\\` as `/`, so any of those (or any other
    control character or space) anywhere is refused, as is anything that parses with a scheme or a host."""
    if not target or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch == "\\" for ch in target):
        return DEFAULT_NEXT
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return DEFAULT_NEXT
    return target


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _cookies(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


class AccessGate:
    """ASGI middleware. Lifespan and other scopes pass straight through."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = token
        self._expected = _cookie_value(token) if token else ""

    @staticmethod
    def _is_open(path: str, headers: dict[str, str]) -> bool:
        if path == BEARER_PATH or path.startswith(BEARER_PATH + "/"):
            return headers.get("authorization", "").lower().startswith("bearer ")
        return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)

    def _has_access(self, headers: dict[str, str]) -> bool:
        got = _cookies(headers).get(ACCESS_COOKIE, "")
        return bool(self._expected) and hmac.compare_digest(got, self._expected)

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        if self._is_open(scope.get("path", ""), headers):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("path") == "/access":
            await self._access(scope, headers, send)
            return
        if self._has_access(headers):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self._deny(headers, send)

    async def _access(self, scope: Scope, headers: dict[str, str], send: Any) -> None:
        if not self._token:
            await _respond(send, 503, {"detail": "DEMO_ACCESS_TOKEN is not set", "code": "access_unconfigured"})
            return
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        given = (query.get("token") or [""])[0]
        if not hmac.compare_digest(given.encode(), self._token.encode()):
            await _respond(send, 403, {"detail": "Wrong access link", "code": "access_denied"})
            return
        secure = scope.get("scheme") == "https" or headers.get("x-forwarded-proto") == "https"
        cookie = f"{ACCESS_COOKIE}={self._expected}; Path=/; Max-Age={COOKIE_MAX_AGE_S}; HttpOnly; SameSite=Lax"
        if secure:
            cookie += "; Secure"
        location = _safe_next((query.get("next") or [None])[0])
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [(b"location", location.encode()), (b"set-cookie", cookie.encode())],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def _deny(self, headers: dict[str, str], send: Any) -> None:
        if not self._token:
            await _respond(send, 503, {"detail": "DEMO_ACCESS_TOKEN is not set", "code": "access_unconfigured"})
        elif "text/html" in headers.get("accept", ""):
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/html")]})
            await send({"type": "http.response.body", "body": _DENIED_PAGE})
        else:
            await _respond(send, 401, {"detail": "Access link required", "code": "access_required"})


async def _respond(send: Any, status: int, body: dict[str, str]) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})
