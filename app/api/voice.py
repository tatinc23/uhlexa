"""Voice routes (spec 22.4): status, a LiveKit session for the signed-in user, wake-mode settings, and the pages.

The token's dispatch metadata is built here from the authenticated Principal; nothing in the request body can choose
the user. Max (no Keiki agent) gets a 409, not a token.
"""

import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.auth.dependencies import get_principal
from app.db.session import get_db
from app.errors import InvalidTransition, ProviderUnavailable
from app.voice.config import VoiceConfig
from app.voice.tokens import mint_token
from app.voice.wake import WAKE_MODES

router = APIRouter(prefix="/api/voice", tags=["voice"])

_WEB = Path(__file__).resolve().parents[2] / "web"
Surface = Literal["echo", "app", "hub"]
Mode = Literal["wake_word", "follow_up", "always"]


class SessionIn(BaseModel):
    surface: Surface


class SessionOut(BaseModel):
    url: str
    token: str
    room: str
    identity: str
    surface: Surface
    wake_mode: Mode
    avatar: bool


class SettingsIn(BaseModel):
    wake_mode: Mode


class SettingsOut(BaseModel):
    wake_mode: Mode
    modes: list[str]


def get_voice_config() -> VoiceConfig:
    return VoiceConfig.from_env()


def _wake_mode(principal: Principal, config: VoiceConfig) -> str:
    saved = ((principal.agent.config or {}).get("voice") or {}).get("wake_mode")
    return saved if saved in WAKE_MODES else config.default_wake_mode


@router.get("/status")
def voice_status(config: VoiceConfig = Depends(get_voice_config)) -> dict[str, dict[str, str]]:
    """Badges for the voice pages. Never a secret."""
    return config.status()


@router.post("/session")
def create_session(
    body: SessionIn,
    principal: Principal = Depends(get_principal),
    config: VoiceConfig = Depends(get_voice_config),
) -> SessionOut:
    status = config.status()
    if status["voice"]["status"] != "ok":
        raise ProviderUnavailable("voice", status["voice"]["detail"])
    handle = principal.user.handle
    if config.agent_id_for(handle) is None:
        raise InvalidTransition(f"Voice isn't set up for {principal.user.display_name}", code="voice_not_set_up")
    wake_mode = _wake_mode(principal, config)
    avatar = body.surface == "hub" and status["avatar"]["status"] == "ok"
    room = f"uhlexa-{body.surface}-{handle}-{secrets.token_hex(4)}"
    identity = f"human-{handle}-{secrets.token_hex(3)}"
    metadata = {
        "handle": handle,
        "user_id": str(principal.user.id),
        "display_name": principal.user.display_name,
        "surface": body.surface,
        "wake_mode": wake_mode,
        "avatar": avatar,
    }
    token = mint_token(
        api_key=config.livekit_api_key,
        api_secret=config.livekit_api_secret,
        room=room,
        identity=identity,
        name=principal.user.display_name,
        agent_name=config.agent_name,
        metadata=metadata,
    )
    return SessionOut(
        url=config.livekit_url,
        token=token,
        room=room,
        identity=identity,
        surface=body.surface,
        wake_mode=wake_mode,
        avatar=avatar,
    )


@router.get("/settings")
def get_voice_settings(
    principal: Principal = Depends(get_principal), config: VoiceConfig = Depends(get_voice_config)
) -> SettingsOut:
    return SettingsOut(wake_mode=_wake_mode(principal, config), modes=list(WAKE_MODES))


@router.put("/settings")
def put_voice_settings(
    body: SettingsIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> SettingsOut:
    """Only the caller's own agent. The JSON column is reassigned so SQLAlchemy sees the change."""
    agent = principal.agent
    config = dict(agent.config or {})
    config["voice"] = {**(config.get("voice") or {}), "wake_mode": body.wake_mode}
    agent.config = config
    db.commit()
    return SettingsOut(wake_mode=body.wake_mode, modes=list(WAKE_MODES))


def mount_pages(app: FastAPI) -> None:
    """`/voice/*` pages and `/voice-vendor/*` (pinned livekit-client). Call before the MCP mount at `/`.

    `/voice/echo|app|hub` redirect to the `.html` files (StaticFiles only maps `/` to index.html)."""
    for page in ("echo", "app", "hub"):
        app.add_api_route(
            f"/voice/{page}",
            lambda page=page: RedirectResponse(f"/voice/{page}.html"),
            methods=["GET"],
            include_in_schema=False,
        )
    if (_WEB / "voice").is_dir():
        app.mount("/voice", StaticFiles(directory=_WEB / "voice", html=True), name="voice")
    if (_WEB / "vendor").is_dir():
        app.mount("/voice-vendor", StaticFiles(directory=_WEB / "vendor"), name="voice-vendor")
