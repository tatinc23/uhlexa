"""The Family Board is built on every poll and every agent turn; Neon costs ~0.1 s per statement."""

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.services import board_service
from tests.integration.test_board import Seeded

MAX_STATEMENTS = 16


def test_board_builds_in_a_handful_of_queries(db: Session, seeded: "Seeded") -> None:
    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement.split("\n")[0][:110])

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        board_service.build_family_board(db, seeded["alex"])
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) <= MAX_STATEMENTS
