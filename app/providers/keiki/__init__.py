"""Keiki (onkeiki.com) API gateway client: the LLM brain behind inbound Inkbox turns. Never authorization."""

from app.providers.keiki.config import KeikiConfig
from app.providers.keiki.errors import KeikiAuthError, KeikiBadRequest, KeikiBusy, KeikiError, KeikiTurnFailed
from app.providers.keiki.gateway import GatewayReply, KeikiGateway, conversation_id_for

__all__ = [
    "GatewayReply",
    "KeikiAuthError",
    "KeikiBadRequest",
    "KeikiBusy",
    "KeikiConfig",
    "KeikiError",
    "KeikiGateway",
    "KeikiTurnFailed",
    "conversation_id_for",
]
