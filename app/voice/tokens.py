"""LiveKit access tokens with the voice worker dispatched inside the token (spec 22.3).

HS256 JWT built with the standard library so the app image needs no `livekit-api`. Claim names match `livekit-api`'s
`AccessToken` (pinned by `tests/unit/test_voice_tokens.py`). The browser can't alter the signed dispatch metadata, so
the worker can trust it.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

DEFAULT_TTL_S = 900


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _compact(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def mint_token(
    *,
    api_key: str,
    api_secret: str,
    room: str,
    identity: str,
    name: str,
    agent_name: str,
    metadata: dict[str, Any],
    ttl_s: int = DEFAULT_TTL_S,
    now: float | None = None,
) -> str:
    """One room, mic + data publish only, no admin grants, and one explicit dispatch of `agent_name`."""
    if not (api_key and api_secret and room and identity and agent_name):
        raise ValueError("api key, secret, room, identity and agent name are required")
    issued = int(time.time() if now is None else now)
    claims = {
        "iss": api_key,
        "sub": identity,
        "nbf": issued,
        "exp": issued + ttl_s,
        "name": name,
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
            "canPublishSources": ["microphone"],
        },
        "roomConfig": {
            "agents": [{"agentName": agent_name, "metadata": json.dumps(metadata, separators=(",", ":"))}],
        },
    }
    signing_input = f"{_b64(_compact({'alg': 'HS256', 'typ': 'JWT'}))}.{_b64(_compact(claims))}"
    signature = hmac.new(api_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"
