"""Environment-driven configuration. Attribute names are the lower-cased env var names (spec section 5)."""

import json
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_SESSION_SECRET = "dev-only-session-secret-do-not-use-in-production"  # DEMO-AUTH: replace before real users
REASONING_LEVELS = ("none", "minimal", "low", "medium", "high")  # Keiki gateway `reasoning_effort` (addendum 21.3)
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error names the variable and never echoes what was typed into it.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    database_url: str = "postgresql+psycopg://uhlexa:uhlexa@localhost:5432/uhlexa"
    app_session_secret: str = Field(DEV_SESSION_SECRET, repr=False)
    secret_encryption_key: str = Field("", repr=False)

    enable_demo_auth: bool = True
    enable_approvals: bool = True
    # How long the receiving Uhlexa takes to accept a handoff (a real queued job, so both screens show the two steps).
    handoff_accept_delay_seconds: int = 3

    enable_primitive: bool = False
    primitive_api_key: str = Field("", repr=False)
    primitive_api_base_url: str = ""
    primitive_webhook_secret: str = Field("", repr=False)

    enable_subq_snap: bool = False
    subq_api_key: str = Field("", repr=False)
    subq_decisions_base_url: str = "https://api.subq.ai/v1"
    subq_snap_model: str = "subq-snap-preview"
    # Below this a Snap choice is treated as no decision (fail closed). 0.6 is the docs' sample cutoff, not a tuned one.
    subq_min_confidence: float = Field(0.6, ge=0.0, le=1.0)

    enable_maritime: bool = False
    maritime_computers_key: str = Field("", repr=False)
    maritime_mcp_url: str = "https://mcp.maritime.sh/mcp"
    maritime_safe_target_url: str = ""

    # Keiki is the LLM brain behind inbound Inkbox turns and the MCP client (addendum 21.6); `ok` needs the gateway key
    # AND at least one agent id.
    enable_keiki: bool = False
    keiki_api_base_url: str = "https://onkeiki.com"
    keiki_gateway_key: str = Field("", repr=False)  # a key with the `agents:chat` scope
    keiki_agent_ids: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)  # {user handle: agent id}
    keiki_gateway_timeout_s: float = Field(120.0, gt=0)
    keiki_call_reasoning: str = "none"
    keiki_daily_turn_cap: int = Field(200, ge=0)
    keiki_api_key: str = Field("", repr=False)  # admin scripts only; the app never needs it

    # Inkbox carries iMessage, SMS, email and calls. A channel is usable only with its own switch on as well.
    enable_inkbox: bool = False
    enable_inkbox_imessage: bool = False
    enable_inkbox_sms: bool = False
    enable_inkbox_email: bool = False
    enable_inkbox_calls: bool = False
    inkbox_api_key: str = Field("", repr=False)
    inkbox_base_url: str = "https://inkbox.ai"
    inkbox_signing_keys: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict, repr=False)  # {handle: key}
    inkbox_webhook_auth_token: str = Field("", repr=False)

    # Outbound contact (`contact_member`): every switch defaults off; the limits are per sender (household cap is 3x).
    enable_outbound_contact: bool = False
    enable_outbound_calls: bool = False
    outbound_texts_per_hour: int = Field(6, ge=0)
    outbound_calls_per_hour: int = Field(2, ge=0)
    outbound_per_day: int = Field(20, ge=0)
    enable_channel_approvals: bool = False  # accept `APPROVE <code>` from a verified adult owner

    # Out of scope for the hackathon: flags exist, no adapters.
    enable_ring: bool = False
    enable_firetv: bool = False

    embedded_worker: bool = False
    seed_demo_data: bool = True
    demo_default_user: str = "alex"
    public_base_url: str = "http://localhost:8000"
    # Extra Host values /mcp accepts (DNS-rebinding protection), comma separated, e.g. "uhlexa.example.com,demo.local".
    mcp_allowed_hosts: str = ""
    job_lease_seconds: int = 120
    # How long an idle worker sleeps. Unset: 1 s for a worker of its own, 900 s for the embedded worker (it is woken
    # in-process the moment a job is committed, and a slow heartbeat lets Neon scale to zero).
    worker_idle_seconds: float | None = Field(default=None, gt=0)
    # Alembic uses this (Neon's direct host) when set; the app itself uses DATABASE_URL (the pooled host).
    migrations_database_url: str = ""

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() == "development"

    @property
    def worker_idle(self) -> float:
        if self.worker_idle_seconds is not None:
            return self.worker_idle_seconds
        return 900.0 if self.embedded_worker else 1.0

    @field_validator("database_url", "migrations_database_url")
    @classmethod
    def _use_psycopg3_driver(cls, value: str) -> str:
        """Hosted Postgres (Neon, Manufact) hands out plain postgres(ql):// URLs; we ship psycopg 3."""
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value.removeprefix(prefix)
        return value

    @field_validator("keiki_agent_ids", "inkbox_signing_keys", mode="before")
    @classmethod
    def _parse_json_map(cls, value: object, info: ValidationInfo) -> dict[str, str]:
        """`{"handle": "value"}` as JSON (or already a dict). Errors name the variable, never the value."""
        env_name = (info.field_name or "").upper()
        if isinstance(value, str):
            if not value.strip():
                return {}
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError(f"{env_name} must be a JSON object of text keys and text values") from None
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str) for k, v in value.items()
        ):
            raise ValueError(f"{env_name} must be a JSON object of text keys and text values")
        if info.field_name == "keiki_agent_ids":
            # Handles are stored lower-case everywhere else; blank agent ids mean "no agent" and are dropped.
            return {k.strip().lower(): v.strip() for k, v in value.items() if v.strip()}
        return {k.strip(): v for k, v in value.items() if v.strip()}

    @field_validator("keiki_api_base_url", "inkbox_base_url")
    @classmethod
    def _https_except_localhost(cls, value: str, info: ValidationInfo) -> str:
        field = cls.model_fields[info.field_name or ""]
        if not value.strip():  # `KEIKI_API_BASE_URL=` in a copied .env means "use the default"
            return str(field.default)
        parsed = urlparse(value.strip())
        local = parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS
        if parsed.scheme != "https" and not local:
            raise ValueError(f"{(info.field_name or '').upper()} must be an https URL (localhost excepted)")
        return value.strip()

    @field_validator("keiki_call_reasoning")
    @classmethod
    def _known_reasoning_level(cls, value: str) -> str:
        level = value.strip().lower()
        if level not in REASONING_LEVELS:
            raise ValueError(f"KEIKI_CALL_REASONING must be one of {', '.join(REASONING_LEVELS)}")
        return level

    @field_validator("app_session_secret")
    @classmethod
    def _blank_secret_uses_dev_default(cls, value: str) -> str:
        return value or DEV_SESSION_SECRET

    @model_validator(mode="after")
    def _production_needs_a_real_session_secret(self) -> "Settings":
        if self.app_env.lower() == "production" and self.app_session_secret == DEV_SESSION_SECRET:
            raise ValueError("APP_SESSION_SECRET must be set to a real secret when APP_ENV=production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
