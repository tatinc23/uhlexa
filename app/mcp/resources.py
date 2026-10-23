"""The Family Board as an MCP App: a `ui://` HTML resource the host renders in a sandboxed iframe (spec section 15)."""

from pathlib import Path

from mcp.server.apps import Apps

BOARD_URI = "ui://uhlexa/family-board.html"
BOARD_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "family-board.html"


def board_html() -> str:
    """The template, read at server build time. A missing file is a deployment error, not something to paper over."""
    return BOARD_TEMPLATE.read_text(encoding="utf-8")


def register_board_resource(apps: Apps) -> None:
    apps.add_html_resource(
        BOARD_URI,
        board_html(),
        name="family-board",
        title="Family Board",
        description="The household's members, threads, approvals, tasks, browser task and activity at a glance.",
    )
