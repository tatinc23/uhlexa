"""Inkbox adapter configuration. A later task builds it from Settings; this module reads no environment."""

from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_BASE_URL = "https://inkbox.ai"


@dataclass(frozen=True)
class InkboxConfig:
    enabled: bool
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    signing_keys: Mapping[str, str] = field(default_factory=dict, repr=False)  # handle -> per-identity signing key
    webhook_auth_token: str = field(default="", repr=False)  # optional second factor (Authorization: Bearer)
    imessage: bool = False
    sms: bool = False
    email: bool = False
    calls: bool = False

    def signing_key_for(self, handle: str) -> str:
        return self.signing_keys.get(handle, "")
