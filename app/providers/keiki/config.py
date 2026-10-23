"""Keiki gateway configuration.

Note: a later task adds `KeikiConfig.from_settings` and wires the `KEIKI_*` settings (spec 21.6). This
module reads no environment and imports nothing from `app.settings`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://onkeiki.com"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


@dataclass(frozen=True)
class KeikiConfig:
    enabled: bool
    base_url: str = DEFAULT_BASE_URL
    gateway_key: str = field(default="", repr=False)  # key with the `agents:chat` scope
    agent_ids: Mapping[str, str] = field(default_factory=dict)  # user handle -> Keiki agent id
    timeout_s: float = 120.0
    connect_timeout_s: float = 5.0
    call_reasoning: str = "none"

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS):
            raise ValueError("Keiki base URL must be HTTPS (localhost excepted)")
