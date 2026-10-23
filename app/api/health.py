from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.migrate import is_at_head
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only: never touches the database."""
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> JSONResponse:
    """Readiness: the database answers and the schema is at the newest migration."""
    try:
        connection = db.connection()
        connection.execute(text("SELECT 1"))
        at_head = is_at_head(connection)
    except Exception:
        return JSONResponse({"status": "not_ready", "db": "error", "migrations": "unknown"}, status_code=503)
    if not at_head:
        return JSONResponse({"status": "not_ready", "db": "ok", "migrations": "behind"}, status_code=503)
    return JSONResponse({"status": "ready", "db": "ok", "migrations": "head"})
