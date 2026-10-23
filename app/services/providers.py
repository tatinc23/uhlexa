"""Provider service factories, built from the current settings on every call (settings are cached, services are
cheap). Routers, MCP tools and the worker use these so tests can swap one function."""

from app.providers.inkbox.config import InkboxConfig
from app.providers.inkbox.service import InkboxService
from app.providers.keiki.config import KeikiConfig
from app.providers.keiki.gateway import KeikiGateway
from app.providers.maritime.service import MaritimeService
from app.providers.primitive.service import PrimitiveService
from app.providers.subq.service import SnapService
from app.settings import get_settings


def get_snap() -> SnapService:
    return SnapService(get_settings())


def get_primitive() -> PrimitiveService:
    return PrimitiveService(get_settings())


def get_maritime() -> MaritimeService:
    return MaritimeService(get_settings())


def get_keiki() -> KeikiGateway:
    s = get_settings()
    return KeikiGateway(
        KeikiConfig(
            enabled=s.enable_keiki,
            base_url=s.keiki_api_base_url,
            gateway_key=s.keiki_gateway_key,
            agent_ids=dict(s.keiki_agent_ids),
            timeout_s=s.keiki_gateway_timeout_s,
            call_reasoning=s.keiki_call_reasoning,
        )
    )


def get_inkbox() -> InkboxService:
    s = get_settings()
    return InkboxService(
        InkboxConfig(
            enabled=s.enable_inkbox,
            api_key=s.inkbox_api_key,
            base_url=s.inkbox_base_url,
            signing_keys=dict(s.inkbox_signing_keys),
            webhook_auth_token=s.inkbox_webhook_auth_token,
            imessage=s.enable_inkbox_imessage,
            sms=s.enable_inkbox_sms,
            email=s.enable_inkbox_email,
            calls=s.enable_inkbox_calls,
        )
    )
