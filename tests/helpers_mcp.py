"""Helpers for the MCP tests: mint agent tokens, and speak JSON-RPC to the Streamable HTTP app.

Nothing here touches the network (Starlette's TestClient talks to the ASGI app in-process)."""

import json
import secrets
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.mcp.auth import TOKEN_BINDING_TYPE, hash_token
from app.models import BindingStatus, ProviderBinding, ProviderName

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def mint_token(
    db: Session, principal: Principal, *, status: BindingStatus = BindingStatus.ACTIVE, with_user: bool = True
) -> str:
    """A fresh token bound to `principal`'s default agent (only its sha256 is stored), committed."""
    token = secrets.token_urlsafe(32)
    db.add(
        ProviderBinding(
            household_id=principal.household.id,
            user_id=principal.user.id if with_user else None,
            agent_id=principal.agent.id,
            provider=ProviderName.KEIKI,
            binding_type=TOKEN_BINDING_TYPE,
            external_id=hash_token(token),
            status=status,
            config={},
        )
    )
    db.commit()
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def rpc(
    client: TestClient, method: str, params: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None
) -> Any:
    """POST one JSON-RPC request to /mcp (or the path in `headers["X-Path"]`); returns the httpx response."""
    headers = dict(headers or {})
    path = headers.pop("X-Path", "/mcp")
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post(path, content=json.dumps(body), headers={**MCP_HEADERS, **headers})


def call_tool(
    client: TestClient, name: str, arguments: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """The `result` of a tools/call over HTTP (`isError`, `content`, `structuredContent`); asserts HTTP 200."""
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def result_text(result: dict[str, Any]) -> str:
    return " ".join(block.get("text", "") for block in result["content"])
