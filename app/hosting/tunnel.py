"""Inkbox tunnel runner: `python -m app.hosting.tunnel` (docs/HOSTING.md).

Connects the Inkbox identity's tunnel (`https://{handle}.inkboxwire.com`) to the app on 127.0.0.1. The tunnel is an
identity property (one per identity, edge TLS by default) and authenticates with the Inkbox API key. Blocks until
Ctrl-C. Never uses @samantha (Ken's PM agent).
"""

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

logger = logging.getLogger("uhlexa.tunnel")

DEFAULT_HANDLE = "uhlexa"
FORBIDDEN_HANDLES = frozenset({"samantha"})


@dataclass(frozen=True)
class TunnelConfig:
    handle: str = DEFAULT_HANDLE
    port: int = 8000
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TunnelConfig":
        env = os.environ if env is None else env
        try:
            port = int(env.get("APP_PORT") or 8000)
        except ValueError:
            port = 8000
        return cls(
            handle=(env.get("INKBOX_TUNNEL_HANDLE") or DEFAULT_HANDLE).strip().lower(),
            port=port,
            api_key=(env.get("INKBOX_API_KEY") or "").strip(),
        )

    @property
    def public_url(self) -> str:
        return f"https://{self.handle}.inkboxwire.com"

    @property
    def forward_to(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def problems(self) -> list[str]:
        found = []
        if not self.api_key:
            found.append("INKBOX_API_KEY is not set (admin key, or a key scoped to the tunnel's identity)")
        if self.handle in FORBIDDEN_HANDLES:
            found.append(f"refusing to use @{self.handle}'s tunnel; set INKBOX_TUNNEL_HANDLE to the Uhlexa identity")
        return found


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = TunnelConfig.from_env()
    if problems := config.problems():
        for problem in problems:
            logger.error(problem)
        return 2
    from inkbox import Inkbox

    def on_status(status: str) -> None:
        if status == "superseded":
            logger.error("tunnel superseded: another process connected @%s's tunnel; this one stops", config.handle)
        else:
            logger.info("tunnel %s", status)

    with Inkbox(api_key=config.api_key) as inkbox:
        listener = inkbox.tunnels.connect(name=config.handle, forward_to=config.forward_to, on_status=on_status)
        logger.info("public URL %s -> %s", listener.public_url, config.forward_to)
        try:
            listener.wait()
        except KeyboardInterrupt:
            logger.info("stopping tunnel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
