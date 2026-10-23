"""The job worker (spec section 3): claims one job at a time from the `jobs` table and runs its handler.

Postgres: `SELECT ... FOR UPDATE SKIP LOCKED` lets several workers share the table. A job whose worker died is
`running` with a stale `locked_at`; after `JOB_LEASE_SECONDS` it is reclaimed (attempts + 1). After `max_attempts` it is
`failed` and its owner is told (a browser task becomes `failed`, never stuck `running`).

Run as a process with `python -m app.jobs.worker`, or embedded in the api (see `app.lifespan`).
"""

import json
import logging
import os
import signal
import threading
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import utcnow
from app.db.session import SessionLocal
from app.errors import NotFound, ServiceError
from app.jobs import channel_handlers, dispatcher, primitive_email
from app.models import (
    Conversation,
    Job,
    JobStatus,
    Message,
    SenderType,
    TranscriptProvider,
    WebhookEvent,
    WebhookStatus,
)
from app.providers.primitive.webhook import WebhookPayloadError, parse_event
from app.services import activity_service, browser_task_service, handoff_service, providers
from app.services.clock import tick
from app.settings import get_settings

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 10
MAX_ERROR_CHARS = 300

# Swapped by tests; resolved at call time so a monkeypatch takes effect.
make_maritime = providers.get_maritime
make_snap = providers.get_snap
make_primitive = providers.get_primitive


def handle_browser_task_run(db: Session, payload: dict[str, Any]) -> None:
    browser_task_id = browser_task_service.job_payload_id(payload)
    if browser_task_id is None:
        logger.warning("browser_task_run job without a browser_task_id; dropping it")
        return
    try:
        browser_task_service.run(db, browser_task_id, maritime=make_maritime(), snap=make_snap())
    except NotFound:
        logger.warning("browser task %s no longer exists; nothing to run", browser_task_id)


def handle_primitive_sync(db: Session, payload: dict[str, Any]) -> None:
    """Mirror one Primitive email webhook into the linked conversation's local transcript. Idempotent."""
    try:
        event_row = db.get(WebhookEvent, uuid.UUID(str(payload["webhook_event_id"])))
    except (KeyError, ValueError):
        logger.warning("primitive_sync job without a valid webhook_event_id; dropping it")
        return
    if event_row is None or event_row.status in (WebhookStatus.PROCESSED, WebhookStatus.IGNORED):
        return
    try:
        parsed = parse_event(json.dumps(event_row.payload).encode())
    except WebhookPayloadError as exc:
        _finish_webhook(event_row, WebhookStatus.IGNORED, f"unusable payload: {exc}")
        return
    conversation = (
        db.scalars(select(Conversation).where(Conversation.primitive_thread_id == parsed.thread_id)).first()
        if parsed.thread_id
        else None
    )
    for_agents = parsed.type == "email.received" and primitive_email.has_agent_inboxes(db)
    if (conversation is None and not for_agents) or not parsed.message_id:
        _finish_webhook(event_row, WebhookStatus.IGNORED, "no conversation is linked to this thread")
        return
    primitive = make_primitive()
    if for_agents:
        # An email to someone's agent inbox: their Keiki agent answers it (primitive_email has the rules).
        email = primitive.get_message(parsed.message_id)  # ProviderUnavailable -> the job retries
        if primitive_email.handle(db, event_row, email, primitive):
            return
        if conversation is None:
            _finish_webhook(event_row, WebhookStatus.IGNORED, "no conversation is linked to this thread")
            return
    if db.scalars(select(Message.id).where(Message.primitive_message_id == parsed.message_id)).first() is None:
        message = primitive.get_message(parsed.message_id)  # ProviderUnavailable -> the job retries
        if not primitive_email.from_household(db, conversation.household_id, message):
            # Agents read this transcript: an outsider writing into the thread must not put words in front of them.
            _finish_webhook(event_row, WebhookStatus.IGNORED, "sender not in household")
            return
        now = tick()
        db.add(
            Message(
                conversation_id=conversation.id,
                sender_type=SenderType.SYSTEM,
                body=f"Email from {message.sender}: {message.body_text}"[:5000],
                primitive_message_id=parsed.message_id,
                source=TranscriptProvider.PRIMITIVE,
                created_at=now,
            )
        )
        conversation.last_message_at = now
        activity_service.record(
            db,
            household_id=conversation.household_id,
            event_type="webhook.received",
            actor_type="provider",
            conversation_id=conversation.id,
            summary="A new email arrived on a Primitive-backed thread.",
            payload={"provider": "primitive", "event": parsed.type},
        )
    _finish_webhook(event_row, WebhookStatus.PROCESSED, None)


def _finish_webhook(row: WebhookEvent, status: WebhookStatus, error: str | None) -> None:
    row.status, row.error, row.processed_at = status, error, utcnow()


HANDLERS: dict[str, Callable[[Session, dict[str, Any]], None]] = {
    "browser_task_run": handle_browser_task_run,
    "primitive_sync": handle_primitive_sync,
    channel_handlers.JOB_KIND: channel_handlers.handle_inkbox_event,
    handoff_service.JOB_KIND: handoff_service.handle_job,
}


def _abandon_browser_task(db: Session, job: Job) -> None:
    browser_task_id = browser_task_service.job_payload_id(job.payload or {})
    if browser_task_id is not None:
        browser_task_service.abandon(db, browser_task_id, f"The job gave up: {job.last_error}")


def _abandon_primitive_sync(db: Session, job: Job) -> None:
    try:
        row = db.get(WebhookEvent, uuid.UUID(str((job.payload or {})["webhook_event_id"])))
    except (KeyError, ValueError):
        return
    if row is not None and row.status == WebhookStatus.RECEIVED:
        _finish_webhook(row, WebhookStatus.FAILED, job.last_error)


# Called once, with the job already `failed`, so the thing waiting on it can stop saying "running".
ON_ABANDONED: dict[str, Callable[[Session, Job], None]] = {
    "browser_task_run": _abandon_browser_task,
    "primitive_sync": _abandon_primitive_sync,
    channel_handlers.JOB_KIND: channel_handlers.abandon_inkbox_event,
}


def _claim(db: Session, worker_id: str, lease_seconds: int) -> tuple[Job, bool] | None:
    """Take the next due job (or reclaim one whose lease expired) and commit the claim.

    Returns None when there is nothing to do, else (job, runnable). `runnable` is False when the job had already used
    its last allowed attempt: it is marked failed and its owner told, and there is nothing left to run."""
    now = utcnow()
    stale_before = now - timedelta(seconds=lease_seconds)
    stmt = (
        select(Job)
        .where(
            or_(
                and_(Job.status == JobStatus.QUEUED, Job.run_after <= now),
                and_(Job.status == JobStatus.RUNNING, or_(Job.locked_at.is_(None), Job.locked_at < stale_before)),
            )
        )
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
    )
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = db.scalars(stmt).first()
    if job is None:
        db.rollback()
        return None
    reclaimed = job.status == JobStatus.RUNNING
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.FAILED
        job.last_error = f"The worker stopped before finishing and the attempt limit ({job.max_attempts}) was reached."
        job.locked_at = job.locked_by = None
        db.commit()
        _run_abandon_hook(db, job)
        return job, False
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.locked_at = now
    job.locked_by = worker_id
    if reclaimed:
        job.last_error = "Reclaimed after the previous worker's lease expired."
    db.commit()
    return job, True


def _run_abandon_hook(db: Session, job: Job) -> None:
    hook = ON_ABANDONED.get(job.kind)
    if hook is None:
        return
    try:
        hook(db, job)
        db.commit()
    except Exception:
        logger.exception("abandon hook for %s job %s failed", job.kind, job.id)
        db.rollback()


def _describe(exc: BaseException) -> str:
    """A log-safe reason: the type, plus the message only for our own errors (driver errors can echo SQL parameters)."""
    detail = getattr(exc, "message", None) if isinstance(exc, ServiceError) else None
    return f"{type(exc).__name__}{': ' + detail if detail else ''} (see the server log)"[:MAX_ERROR_CHARS]


def _wake_later(seconds: float) -> None:
    timer = threading.Timer(seconds, dispatcher.wakeup.set)
    timer.daemon = True
    timer.start()


def _fail_or_retry(db: Session, job_id: uuid.UUID, exc: BaseException) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    job.last_error = _describe(exc)
    job.locked_at = job.locked_by = None
    if job.attempts < job.max_attempts:
        delay = RETRY_BACKOFF_SECONDS * job.attempts
        job.status = JobStatus.QUEUED
        job.run_after = utcnow() + timedelta(seconds=delay)
        db.commit()
        _wake_later(delay)
        return
    job.status = JobStatus.FAILED
    db.commit()
    _run_abandon_hook(db, job)


def run_once(session_factory: sessionmaker[Session] = SessionLocal, *, worker_id: str = "worker") -> int:
    """Claim and run at most one job. Returns how many jobs it handled (0 when idle), so a loop knows when to sleep.

    A handler that raises is retried with backoff until `max_attempts`, then the job is `failed`."""
    with session_factory() as db:
        claimed = _claim(db, worker_id, get_settings().job_lease_seconds)
        if claimed is None:
            return 0
        job, runnable = claimed
        if not runnable:
            return 1
        job_id, kind, payload = job.id, job.kind, dict(job.payload or {})
        db.info[dispatcher.JOB_ID_INFO] = job_id
        try:
            handler = HANDLERS.get(kind)
            if handler is None:
                raise LookupError(f"no handler for job kind {kind!r}")
            handler(db, payload)
            finished = db.get(Job, job_id)
            if finished is not None:
                finished.status = JobStatus.SUCCEEDED
                finished.locked_at = finished.locked_by = None
            db.commit()
        except Exception as exc:
            logger.exception("job %s (%s) failed", job_id, kind)
            db.rollback()
            _fail_or_retry(db, job_id, exc)
        finally:
            db.info.pop(dispatcher.JOB_ID_INFO, None)
        return 1


def main() -> None:
    """Drain the table until SIGTERM/SIGINT, sleeping `WORKER_IDLE_SECONDS` (or until woken) when it is empty."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()
        dispatcher.wakeup.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    worker_id = f"worker-{os.getpid()}"
    idle = get_settings().worker_idle
    logger.info("worker %s started (idle poll %.1fs)", worker_id, idle)
    while not stop.is_set():
        dispatcher.wakeup.clear()  # before the claim, so a job committed meanwhile is not slept through
        try:
            handled = run_once(worker_id=worker_id)
        except Exception:
            logger.exception("worker iteration failed")
            handled = 0
        if not handled:
            dispatcher.wakeup.wait(idle)
    logger.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
