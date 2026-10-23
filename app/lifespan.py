"""App startup and shutdown. Migrations run in the container entrypoint, not here."""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db.session import session_scope
from app.settings import get_settings

logger = logging.getLogger(__name__)

SHUTDOWN_JOIN_SECONDS = 10.0


def _seed() -> None:
    from app.seed import seed_demo

    with session_scope() as db:
        seed_demo(db)


def _embedded_worker(stop: threading.Event, idle_seconds: float) -> None:
    """Drain the jobs table inside the api process (single-container hosting such as Manufact).

    Sleeps until `dispatcher.enqueue` commits a job (it sets `dispatcher.wakeup`) or `idle_seconds` pass, so on
    Neon the database is only touched on demand plus one slow heartbeat."""
    from app.jobs import dispatcher
    from app.jobs.worker import run_once

    while not stop.is_set():
        dispatcher.wakeup.clear()  # before the claim, so a job committed meanwhile wakes the next wait
        try:
            handled = run_once(worker_id="embedded")
        except Exception:
            logger.exception("embedded worker iteration failed")
            handled = 0
        if not handled:
            dispatcher.wakeup.wait(idle_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.seed_demo_data and settings.is_development:
        await asyncio.to_thread(_seed)
    stop = threading.Event()
    worker: threading.Thread | None = None
    if settings.embedded_worker:
        worker = threading.Thread(
            target=_embedded_worker, args=(stop, settings.worker_idle), name="uhlexa-embedded-worker", daemon=True
        )
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            from app.jobs import dispatcher

            dispatcher.wakeup.set()
            await asyncio.to_thread(worker.join, SHUTDOWN_JOIN_SECONDS)
