"""Enqueue work for the worker. Callers enqueue inside their own transaction; the job exists only if they commit."""

import threading
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import event, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models import Job

# Wakes an in-process worker (the embedded loop on single-container hosting) the moment a job is committed, so the
# loop can sleep for minutes between heartbeats. Another process (the compose worker) simply polls.
wakeup = threading.Event()

_WAKE_PENDING = "uhlexa_wake_pending"
JOB_ID_INFO = "uhlexa_job_id"  # set on the worker's session while it runs a job (see heartbeat)


def _wake_after_commit(db: Session) -> None:
    if db.info.get(_WAKE_PENDING):
        return
    db.info[_WAKE_PENDING] = True

    def fire(session: Session) -> None:
        session.info.pop(_WAKE_PENDING, None)
        wakeup.set()

    event.listen(db, "after_commit", fire, once=True)


def enqueue(
    db: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    household_id: uuid.UUID | None = None,
    run_after: datetime | None = None,
) -> Job:
    """Add a queued job. Payloads carry ids only: never a secret, a URL to visit, or free text to act on."""
    job = Job(kind=kind, payload=dict(payload), household_id=household_id, run_after=run_after or utcnow())
    db.add(job)
    db.flush()
    _wake_after_commit(db)
    if run_after is not None and (delay := (run_after - utcnow()).total_seconds()) > 0:
        # An embedded worker sleeps until woken: wake it again when the job falls due.
        timer = threading.Timer(delay + 0.2, wakeup.set)
        timer.daemon = True
        timer.start()
    return job


def heartbeat(db: Session) -> None:
    """Extend the lease of the job this session is running, so a long browser run is not reclaimed mid-flight."""
    job_id = db.info.get(JOB_ID_INFO)
    if job_id is not None:
        db.execute(update(Job).where(Job.id == job_id).values(locked_at=utcnow()))
