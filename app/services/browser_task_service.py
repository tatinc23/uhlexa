"""Browser tasks: an approved task that drives a Maritime computer through a fixed plan (spec sections 12 and 13).

Only two plans exist, and neither takes a URL or an action from a person or a model:
  - `list_add_items` -> `settings.maritime_safe_target_url`, with the task's validated items;
  - `sign_in_check`  -> our own `/demo/sign-in-wall`, to show the takeover path.
Disabled or misconfigured Maritime is `unavailable` (honest, instant); it is never reported as success.
"""

import base64
import binascii
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.auth.demo_auth import Principal
from app.errors import Forbidden, InvalidTransition, NotFound, ProviderUnavailable
from app.jobs import dispatcher
from app.models import (
    Agent,
    BindingStatus,
    BrowserTask,
    BrowserTaskStatus,
    Job,
    JobStatus,
    ProviderBinding,
    ProviderName,
    Task,
    TaskStatus,
)
from app.providers import config_status
from app.providers.maritime.models import RunOutcome, StepRecord, Verdict, Verifier
from app.providers.maritime.service import MaritimeService
from app.providers.subq.service import SnapService
from app.services import access, activity_service, task_machine
from app.services.clock import tick
from app.services.intent import LIST_ADD_ITEMS, MAX_ITEM_CHARS, MAX_ITEMS, SIGN_IN_CHECK
from app.services.views import SCREENSHOT_KEY, screenshot_url
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

B = BrowserTaskStatus
ACTIVE = (B.QUEUED, B.STARTING, B.RUNNING, B.NEEDS_TAKEOVER)
RUNNABLE = (B.QUEUED, B.STARTING, B.RUNNING)  # STARTING/RUNNING: a reclaimed job re-enters after a worker crash
MAX_SCREENSHOT_BYTES = 1_500_000
MAX_STEP_TEXT = 300
# The runner's question names every item to look for (up to 10 x 40 characters): cutting it short would let Snap
# "verify" a list it was never asked about in full.
MAX_QUESTION_CHARS = 1000
VERDICTS: tuple[Verdict, ...] = ("verified", "not_verified", "needs_human")
BINDING_TYPE = "maritime_computer"
JOB_KIND = "browser_task_run"
TAKEOVER_REASON = "Uhlexa needs a person for this step (a sign-in, a check, or a payment)."


# --- planning -------------------------------------------------------------------------------------------------------


def _task_type(task: Task) -> str:
    return task.meta.get("browser_task_type") or LIST_ADD_ITEMS


def _items(task: Task) -> list[str]:
    """The items to add, re-validated here: 1..10 strings of 1..40 characters. Raises InvalidTransition otherwise."""
    items = task.meta.get("items")
    if _task_type(task) == SIGN_IN_CHECK:
        return []
    if not isinstance(items, list) or not items:
        raise InvalidTransition(
            "There are no items to add, so there is nothing for the browser to do.", code="no_items"
        )
    if len(items) > MAX_ITEMS or not all(isinstance(i, str) and 1 <= len(i.strip()) <= MAX_ITEM_CHARS for i in items):
        raise InvalidTransition(
            f"Browser tasks take 1 to {MAX_ITEMS} items of up to {MAX_ITEM_CHARS} characters each.", code="bad_items"
        )
    return [i.strip() for i in items]


def target_url(task_type: str, settings: Settings) -> str:
    """The only two URLs a browser task may ever visit; both come from our configuration, never from a request."""
    if task_type == SIGN_IN_CHECK:
        return f"{settings.public_base_url.rstrip('/')}/demo/sign-in-wall"
    return settings.maritime_safe_target_url.strip()


def _unavailable_reason(settings: Settings, task_type: str) -> str | None:
    status = config_status(settings, "maritime")
    if status.status != "ok":
        return status.detail
    url = target_url(task_type, settings)
    if not url:
        return "MARITIME_SAFE_TARGET_URL is not set"
    if not url.lower().startswith(("http://", "https://")):
        return "The browser target must be an http(s) URL"
    return None


# --- queries --------------------------------------------------------------------------------------------------------


def _visible(db: Session, principal: Principal) -> tuple[Any, ...]:
    """A browser task is as visible as its own thread and its task's thread."""
    return (
        access.visible_conversation_clause(db, principal, BrowserTask.conversation_id),
        access.visible_conversation_clause(db, principal, Task.conversation_id),
    )


def get_for(db: Session, principal: Principal, browser_task_id: uuid.UUID | str) -> BrowserTask:
    bt = db.scalars(
        select(BrowserTask)
        .outerjoin(Task, Task.id == BrowserTask.task_id)
        .where(
            BrowserTask.id == access.as_uuid(browser_task_id),
            BrowserTask.household_id == principal.household.id,
            *_visible(db, principal),
        )
    ).first()
    if bt is None:
        raise NotFound("Browser task not found")
    return bt


get_status = get_for


def list_recent(db: Session, principal: Principal, *, limit: int = 10) -> list[BrowserTask]:
    """Newest first, `result` deferred (it can hold a screenshot): use with `browser_task_out(include_result=False)`."""
    stmt = (
        select(BrowserTask)
        .options(defer(BrowserTask.result))
        .outerjoin(Task, Task.id == BrowserTask.task_id)
        .where(BrowserTask.household_id == principal.household.id, *_visible(db, principal))
        .order_by(BrowserTask.created_at.desc(), BrowserTask.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    return list(db.scalars(stmt))


def active_for_task(db: Session, task_id: uuid.UUID) -> list[BrowserTask]:
    return list(db.scalars(select(BrowserTask).where(BrowserTask.task_id == task_id, BrowserTask.status.in_(ACTIVE))))


def screenshot_for(db: Session, principal: Principal, browser_task_id: uuid.UUID | str) -> tuple[bytes, str]:
    """(bytes, mime type) of the last screenshot, household-scoped. NotFound until a run has produced one."""
    bt = get_for(db, principal, browser_task_id)
    encoded = (bt.result or {}).get(SCREENSHOT_KEY)
    if not encoded:
        raise NotFound("No screenshot yet")
    try:
        return base64.b64decode(encoded, validate=True), (bt.result or {}).get("screenshot_mime", "image/jpeg")
    except (binascii.Error, ValueError):
        raise NotFound("No screenshot yet") from None


# --- creating and starting ------------------------------------------------------------------------------------------


def create_for_task(db: Session, task: Task, principal: Principal) -> BrowserTask:
    """Queue a browser run for an approved task. The run is pinned to the task's requester (not to whoever approved).
    Maritime off or misconfigured: the browser task is `unavailable` straight away and the task stays `approved`."""
    settings = get_settings()
    task_type = _task_type(task)
    items = _items(task)
    agent_id = (
        task.requested_by_agent_id
        or db.scalars(
            select(Agent.id).where(Agent.owner_user_id == task.created_by_user_id, Agent.is_default.is_(True))
        ).first()
    )
    if agent_id is None:
        raise InvalidTransition("The requester has no agent to run this.", code="no_agent")
    bt = BrowserTask(
        household_id=task.household_id,
        user_id=task.created_by_user_id,
        agent_id=agent_id,
        conversation_id=task.conversation_id,
        task_id=task.id,
        task_type=task_type,
        requested_url=target_url(task_type, settings),
        status=B.QUEUED,
        steps=[],
        result={"items": items},
        created_at=tick(),
    )
    db.add(bt)
    db.flush()
    activity_service.record(
        db,
        household_id=bt.household_id,
        event_type="browser_task.queued",
        summary=f"Browser task queued for “{task.title[:80]}”.",
        actor_type="user",
        actor_user_id=principal.user.id,
        task_id=task.id,
        conversation_id=task.conversation_id,
        browser_task_id=bt.id,
        payload={"task_type": task_type, "items": len(items)},
    )
    dispatcher.enqueue(db, JOB_KIND, {"browser_task_id": str(bt.id)}, household_id=bt.household_id)
    if reason := _unavailable_reason(settings, task_type):
        _mark_unavailable(db, bt, reason)
    return bt


def start(db: Session, principal: Principal, task_id: uuid.UUID | str) -> BrowserTask:
    """Run (or retry) the browser step of an approved task. Exactly one active browser task per task."""
    task = task_machine.get(db, principal, task_id)
    if not (task.created_by_user_id == principal.user.id or principal.is_guardian_or_adult):
        raise Forbidden("Only the requester or a grown-up can run this.")
    if task.kind not in task_machine.BROWSER_KINDS:
        raise InvalidTransition("This task does not use a browser.", code="not_a_browser_task")
    if active_for_task(db, task.id):
        raise InvalidTransition("A browser task is already running for this task.", code="already_running")
    if task.status != TaskStatus.APPROVED:
        raise InvalidTransition(
            f"The task has to be approved first (it is {access.enum_value(task.status)}).", code="not_approved"
        )
    _items(task)  # 409 "no items to add" before anything is created
    return create_for_task(db, task, principal)


# --- the worker -----------------------------------------------------------------------------------------------------


def _mark_unavailable(db: Session, bt: BrowserTask, reason: str, *, provider_outage: bool = False) -> None:
    bt.status = B.UNAVAILABLE
    bt.last_error = reason[:500]
    bt.requires_takeover = False
    bt.takeover_url = None
    db.flush()
    if provider_outage:
        activity_service.record(
            db,
            household_id=bt.household_id,
            event_type="provider.unavailable",
            actor_type="provider",
            task_id=bt.task_id,
            conversation_id=bt.conversation_id,
            browser_task_id=bt.id,
            summary="Maritime is unavailable right now.",
            payload={"provider": "maritime", "reason": reason[:200]},
        )
    activity_service.record(
        db,
        household_id=bt.household_id,
        event_type="browser_task.unavailable",
        actor_type="provider" if provider_outage else "system",
        task_id=bt.task_id,
        conversation_id=bt.conversation_id,
        browser_task_id=bt.id,
        summary=f"The browser task is unavailable: {reason[:200]}",
        payload={"reason": reason[:200]},
    )


def _provider_outage(db: Session, bt: BrowserTask, task: Task | None, reason: str) -> None:
    """Maritime failed mid-run: the browser task is `unavailable`; the task returns to `approved` to be retried."""
    _mark_unavailable(db, bt, reason, provider_outage=True)
    if task is not None and task.status == TaskStatus.RUNNING:
        task_machine.transition(db, task, TaskStatus.APPROVED, actor=None, note="Maritime was unavailable")


def _fail(db: Session, bt: BrowserTask, task: Task | None, reason: str) -> None:
    bt.status = B.FAILED
    bt.last_error = reason[:500]
    bt.requires_takeover = False
    bt.takeover_url = None
    if task is not None and task.status in (TaskStatus.RUNNING, TaskStatus.NEEDS_TAKEOVER):
        task_machine.transition(db, task, TaskStatus.FAILED, actor=None, note=reason)
    db.flush()
    activity_service.record(
        db,
        household_id=bt.household_id,
        event_type="browser_task.failed",
        task_id=bt.task_id,
        conversation_id=bt.conversation_id,
        browser_task_id=bt.id,
        summary=f"The browser task failed: {reason[:200]}",
        payload={"reason": reason[:200]},
    )


def _remember_computer(db: Session, bt: BrowserTask, computer_id: str) -> None:
    """Store the user's computer id (provider_bindings) so later runs and takeover polling reuse it."""
    bt.maritime_computer_id = computer_id
    binding = db.scalars(
        select(ProviderBinding).where(
            ProviderBinding.provider == ProviderName.MARITIME,
            ProviderBinding.binding_type == BINDING_TYPE,
            ProviderBinding.user_id == bt.user_id,
        )
    ).first()
    if binding is None:
        binding = ProviderBinding(
            household_id=bt.household_id,
            user_id=bt.user_id,
            agent_id=bt.agent_id,
            provider=ProviderName.MARITIME,
            binding_type=BINDING_TYPE,
            external_id=computer_id,
        )
        db.add(binding)
    binding.external_id = computer_id
    binding.status = BindingStatus.ACTIVE
    binding.last_used_at = tick()
    db.flush()


def _image_mime(data: bytes) -> str:
    """The media type of a screenshot from its magic bytes (Maritime sends JPEG; PNG and WebP are accepted too)."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _store_screenshot(bt: BrowserTask, data: bytes | None) -> None:
    if not data or len(data) > MAX_SCREENSHOT_BYTES:
        return
    mime = _image_mime(data)
    bt.result = {**(bt.result or {}), SCREENSHOT_KEY: base64.b64encode(data).decode(), "screenshot_mime": mime}
    bt.last_screenshot_ref = screenshot_url(bt.id)


def snap_verifier(snap: SnapService) -> Verifier:
    """The screenshot reader the Maritime runner calls: SubQ Snap `verify_screenshot` with the image attached.

    Anything but a clean, in-range answer (Snap off, malformed, unavailable, unsure) is `not_verified`: honest, never a
    guess. Called by the runner on a worker thread, so it must not touch the database session."""
    choices = {
        "verified": "Every requested item is visibly present.",
        "not_verified": "Something is missing or it is unclear.",
        "needs_human": "A login, CAPTCHA, two-factor or payment step is on screen.",
    }

    def verify(image: bytes, question: str) -> Verdict:
        result = snap.decide(
            "verify_screenshot",
            {"check": question[:MAX_QUESTION_CHARS]},
            choices,
            image=image,
            image_mime=_image_mime(image),
        )
        if result.status == "ok":
            for verdict in VERDICTS:
                if result.choice == verdict:
                    return verdict
        return "not_verified"

    return verify


def _execute_plan(
    maritime: MaritimeService,
    bt: BrowserTask,
    computer_id: str,
    url: str,
    items: list[str],
    on_step: Callable[[StepRecord], None],
    verifier: Verifier,
) -> RunOutcome:
    """The two fixed plans, each reading its screenshots through `verifier`."""
    if bt.task_type == SIGN_IN_CHECK:
        return maritime.run_sign_in_check(bt.user_id, computer_id, url, on_step, verifier=verifier)
    return maritime.run_list_add(bt.user_id, computer_id, url, items, on_step, verifier=verifier)


def run(db: Session, browser_task_id: uuid.UUID, *, maritime: MaritimeService, snap: SnapService) -> BrowserTask:
    """Worker entry point: drive the fixed plan for one browser task. Commits as it goes so the UI sees each step.

    `snap` reads the screenshots (`snap_verifier`): `RunOutcome.verified` is true only when Snap said so, and a login,
    CAPTCHA, 2FA or payment step on screen (`needs_human`) turns into a takeover request.
    """
    bt = db.get(BrowserTask, browser_task_id)
    if bt is None:
        raise NotFound("Browser task not found")
    if bt.status not in RUNNABLE:
        return bt  # already finished, cancelled, unavailable or waiting for a person: nothing to do
    task = db.get(Task, bt.task_id)
    provider = maritime.status
    url = target_url(bt.task_type, get_settings())
    if provider.status in ("disabled", "misconfigured") or not url:
        _mark_unavailable(db, bt, provider.detail if provider.status != "ok" else "No browser target is configured")
        db.commit()
        return bt

    bt.status = B.STARTING
    bt.requested_url = url  # always the configured target, whatever was stored when the task was queued
    if task is not None and task.status == TaskStatus.APPROVED:
        task_machine.transition(db, task, TaskStatus.RUNNING, actor=None)
    db.commit()

    def on_step(step: StepRecord) -> None:
        bt.steps.append(
            {
                "index": step.index,
                "action": step.action[:120],
                "ok": step.ok,
                "note": step.note[:MAX_STEP_TEXT],
                "screenshot_ref": step.screenshot_ref,
            }
        )
        activity_service.record(
            db,
            household_id=bt.household_id,
            event_type="browser_task.step",
            task_id=bt.task_id,
            conversation_id=bt.conversation_id,
            browser_task_id=bt.id,
            summary=f"Step {step.index + 1}: {step.action[:80]}" + ("" if step.ok else " (did not work)"),
            payload={"index": step.index, "ok": step.ok},
        )
        dispatcher.heartbeat(db)
        db.commit()

    try:
        computer_id = bt.maritime_computer_id or maritime.ensure_computer(bt.user_id)
        _remember_computer(db, bt, computer_id)
        bt.status = B.RUNNING
        db.commit()
        items = list((bt.result or {}).get("items", []))
        outcome = _execute_plan(maritime, bt, computer_id, url, items, on_step, snap_verifier(snap))
    except ProviderUnavailable as exc:
        db.refresh(bt)
        if bt.status != B.CANCELLED:
            _provider_outage(db, bt, task, exc.reason)
        db.commit()
        return bt
    except Exception as exc:
        logger.exception("browser task %s crashed", bt.id)
        db.rollback()
        db.refresh(bt)
        if bt.status != B.CANCELLED:
            _fail(db, bt, task, f"Unexpected error ({type(exc).__name__}); see the server log.")
        db.commit()
        return bt

    db.refresh(bt)
    if bt.status == B.CANCELLED:  # cancelled while the run was in flight: leave it that way
        db.commit()
        return bt
    _store_screenshot(bt, outcome.last_screenshot)
    if outcome.status == "succeeded":
        bt.status = B.SUCCEEDED
        bt.verified = bool(outcome.verified)
        bt.last_error = None
        db.flush()
        if task is not None and task.status == TaskStatus.RUNNING:
            task_machine.transition(db, task, TaskStatus.COMPLETED, actor=None)
        verified = "and verified" if bt.verified else "but could not be verified"
        activity_service.record(
            db,
            household_id=bt.household_id,
            event_type="browser_task.succeeded",
            task_id=bt.task_id,
            conversation_id=bt.conversation_id,
            browser_task_id=bt.id,
            summary=f"The browser task finished {verified}.",
            payload={"verified": bt.verified, "detail": outcome.detail[:200]},
        )
    elif outcome.status == "needs_takeover":
        try:
            link = outcome.takeover_url or maritime.request_takeover(bt.user_id, computer_id, TAKEOVER_REASON)
        except ProviderUnavailable as exc:
            _provider_outage(db, bt, task, exc.reason)
            db.commit()
            return bt
        bt.status = B.NEEDS_TAKEOVER
        bt.requires_takeover = True
        bt.takeover_url = link
        bt.last_error = outcome.detail[:500] or None
        db.flush()
        if task is not None and task.status == TaskStatus.RUNNING:
            task_machine.transition(db, task, TaskStatus.NEEDS_TAKEOVER, actor=None)
        activity_service.record(  # the link is a secret: it is never put in the log
            db,
            household_id=bt.household_id,
            event_type="browser_task.needs_takeover",
            task_id=bt.task_id,
            conversation_id=bt.conversation_id,
            browser_task_id=bt.id,
            summary="The browser task needs a person to finish a step.",
            payload={"detail": outcome.detail[:200]},
        )
    else:
        _fail(db, bt, task, outcome.detail or "The browser run did not finish.")
    db.commit()
    return bt


def _close_quietly(maritime: MaritimeService, bt: BrowserTask) -> None:
    """The run ended while the desktop was left open for a person: release it (spec section 20). Best effort only."""
    try:
        maritime.close_computer(bt.user_id, bt.maritime_computer_id or "")
    except Exception as exc:  # the outcome is already recorded; a failed close must not undo it
        logger.warning("could not close the Maritime computer for browser task %s (%s)", bt.id, type(exc).__name__)


def resume_after_takeover(
    db: Session, principal: Principal, browser_task_id: uuid.UUID | str, *, maritime: MaritimeService
) -> BrowserTask:
    """Continue after a takeover, once Maritime says control is back with the agent AND the person finished.

    `takeover_state` is read in full: `mode == "agent"` alone also follows an expired takeover (facts file 8.5).
    `expired` / `failed` ends the browser task as failed, never retried; no result yet waits. On `success` the run is
    re-queued from the last unfinished step; for `sign_in_check` the person's step WAS the last one, so it completes
    (not visually verified) instead of loading the wall again and asking for a person forever.
    """
    bt = get_for(db, principal, browser_task_id)
    if not (bt.user_id == principal.user.id or principal.is_guardian_or_adult):
        raise Forbidden("Only the owner or a grown-up can resume this.")
    if bt.status != B.NEEDS_TAKEOVER or not bt.maritime_computer_id:
        raise InvalidTransition("This browser task is not waiting for a takeover.", code="not_waiting")
    state = maritime.takeover_state(bt.user_id, bt.maritime_computer_id)
    if state.mode == "human":
        raise InvalidTransition("A person still has the desktop. Finish there, then resume.", code="human_in_control")
    task = db.get(Task, bt.task_id)
    if state.mode == "agent" and state.result in ("expired", "failed"):
        _fail(db, bt, task, f"The person did not finish the step (takeover {state.result}).")
        _close_quietly(maritime, bt)
        return bt
    if state.mode != "agent" or state.result != "success":
        raise InvalidTransition("Can't confirm control is back with Uhlexa yet.", code="control_unknown")
    bt.requires_takeover = False
    bt.takeover_url = None  # the link dies with the takeover; never keep it
    if task is not None and task.status == TaskStatus.NEEDS_TAKEOVER:
        task_machine.transition(db, task, TaskStatus.RUNNING, actor=principal)
    if bt.task_type == SIGN_IN_CHECK:
        bt.status = B.SUCCEEDED
        bt.verified = False
        bt.last_error = None
        db.flush()
        if task is not None and task.status == TaskStatus.RUNNING:
            task_machine.transition(db, task, TaskStatus.COMPLETED, actor=None)
        activity_service.record(
            db,
            household_id=bt.household_id,
            event_type="browser_task.succeeded",
            summary="A person finished the sign-in step; the browser task is done (not visually verified).",
            actor_type="user",
            actor_user_id=principal.user.id,
            task_id=bt.task_id,
            conversation_id=bt.conversation_id,
            browser_task_id=bt.id,
            payload={"verified": False, "takeover": "success"},
        )
        _close_quietly(maritime, bt)
        return bt
    bt.status = B.QUEUED
    db.flush()
    activity_service.record(
        db,
        household_id=bt.household_id,
        event_type="browser_task.queued",
        summary="Control is back with Uhlexa; the browser task is queued to continue.",
        actor_type="user",
        actor_user_id=principal.user.id,
        task_id=bt.task_id,
        conversation_id=bt.conversation_id,
        browser_task_id=bt.id,
        payload={"resumed": True},
    )
    dispatcher.enqueue(db, JOB_KIND, {"browser_task_id": str(bt.id)}, household_id=bt.household_id)
    return bt


# --- cancelling and giving up ---------------------------------------------------------------------------------------


def cancel_active(db: Session, task: Task, actor: Principal | None) -> None:
    """The task was cancelled: stop its browser tasks and drop their queued jobs."""
    for bt in active_for_task(db, task.id):
        bt.status = B.CANCELLED
        bt.last_error = "Cancelled"
        bt.requires_takeover = False
        bt.takeover_url = None
        activity_service.record(
            db,
            household_id=bt.household_id,
            event_type="browser_task.cancelled",
            actor_type="user" if actor else "system",
            actor_user_id=actor.user.id if actor else None,
            task_id=bt.task_id,
            conversation_id=bt.conversation_id,
            browser_task_id=bt.id,
            summary="The browser task was cancelled.",
        )
    mine = {str(i) for i in db.scalars(select(BrowserTask.id).where(BrowserTask.task_id == task.id))}
    for job in db.scalars(select(Job).where(Job.kind == JOB_KIND, Job.status == JobStatus.QUEUED)):
        if (job.payload or {}).get("browser_task_id") in mine:
            job.status = JobStatus.CANCELLED
    db.flush()


def abandon(db: Session, browser_task_id: uuid.UUID, reason: str) -> None:
    """The job gave up (attempts exhausted): a browser task must never stay `running` forever."""
    bt = db.get(BrowserTask, browser_task_id)
    if bt is None or bt.status not in ACTIVE:
        return
    _fail(db, bt, db.get(Task, bt.task_id), reason)


def job_payload_id(payload: dict[str, Any]) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(payload["browser_task_id"]))
    except (KeyError, ValueError):
        return None
