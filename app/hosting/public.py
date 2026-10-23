"""The app as served behind the tunnel: `uvicorn app.hosting.public:app` (docs/HOSTING.md).

Same app as `app.main:app`, wrapped in the access gate, plus `/hosting/ws-echo`, a gated WebSocket echo for checking
that WebSockets pass the tunnel before the call socket depends on it.
"""

import os

from fastapi import WebSocket

from app.hosting.gate import AccessGate
from app.main import create_app

core = create_app()


async def ws_echo(ws: WebSocket) -> None:
    await ws.accept()
    while True:
        await ws.send_text(await ws.receive_text())


# Insert ahead of the MCP app, which create_app() mounts last at `/` and would otherwise shadow this route.
core.add_api_websocket_route("/hosting/ws-echo", ws_echo)
core.router.routes.insert(0, core.router.routes.pop())


app = AccessGate(core, token=os.environ.get("DEMO_ACCESS_TOKEN", ""))
