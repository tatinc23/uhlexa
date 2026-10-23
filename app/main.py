from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import (
    activity,
    approvals,
    board,
    browser_tasks,
    channels,
    conversations,
    demo,
    health,
    providers,
    tasks,
    voice,
    webhooks,
)
from app.errors import register_error_handlers
from app.lifespan import lifespan
from app.mcp.server import build_mcp_server, create_mcp_http_app

_ROOT = Path(__file__).resolve().parents[1]
SIMULATOR_DIR = _ROOT / "web" / "simulator"
BOARD_PAGE = _ROOT / "app" / "templates" / "family-board.html"


def board_page() -> Response:
    """The standalone Family Board (it polls `/api/board`). 503 until the template ships."""
    if not BOARD_PAGE.is_file():
        return JSONResponse(
            {"detail": "The Family Board page is not built yet.", "code": "board_page_missing"}, status_code=503
        )
    return FileResponse(BOARD_PAGE, media_type="text/html")


def create_app() -> FastAPI:
    server = build_mcp_server()

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with server.session_manager.run(), lifespan(app):
            yield

    app = FastAPI(title="Uhlexa", version="0.1.0", lifespan=app_lifespan)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(demo.router)
    app.include_router(providers.router)
    app.include_router(webhooks.router)
    app.include_router(channels.router)
    app.include_router(voice.router)
    for router in (conversations, tasks, approvals, activity, browser_tasks, board):
        app.include_router(router.router)
    app.add_api_route(
        "/", lambda: RedirectResponse("/simulator/", status_code=307), methods=["GET"], include_in_schema=False
    )
    app.add_api_route("/board", board_page, methods=["GET"], include_in_schema=False)
    voice.mount_pages(app)  # before the catch-all mount at `/` (MCP)
    SIMULATOR_DIR.mkdir(parents=True, exist_ok=True)  # the simulator build fills it; startup never depends on it
    app.mount("/simulator", StaticFiles(directory=SIMULATOR_DIR, html=True), name="simulator")
    app.mount("/", create_mcp_http_app(server))  # LAST: it serves exactly /mcp and /mcp/{alias}
    return app


app = create_app()
