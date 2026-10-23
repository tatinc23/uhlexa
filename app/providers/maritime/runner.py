"""The fixed plans Uhlexa runs on a Maritime computer: `list_add_items` and `sign_in_check`.

A plan is a hard-coded sequence of allowlisted `computer` actions. Nothing here reads a model's free text: the only
variable inputs are the page URL (server configuration), the list items (a validated list of short strings from an
approved task) and the verifier's three-way answer. Every action returns the screen after it; a step is only trusted
once the next frame shows it (screenshot -> act -> verify).

The plan was written from a live run on 2026-10-22 (docs/research/2026-10-22-maritime-facts.md, "Live run"):

- A fresh Linux computer already has Chromium open and maximised on `about:blank`, address bar focused. The taskbar
  button of the active window MINIMISES it when clicked, so the plan never clicks it; `alt+Tab` is the idempotent way to
  bring the browser to the front (no-op when it already is, restores it when minimised).
- `ctrl+shift+n` opens an Incognito window: clean storage every run (TodoMVC keeps its list in the browser profile,
  so a normal window would pile up "milk, eggs, milk, eggs"), nothing left on the computer, and `ctrl+shift+w` closes
  it again so the desktop ends the way it began. It is only closed once the screenshot confirms it IS an Incognito
  window.
- `ctrl+l`, type the URL, `Return`, `wait 3` loads TodoMVC; its "What needs to be done?" input is at (733, 256) in the
  1200x750 frame. Items go in with `type` + `Return`; the field keeps focus.

Verification is vision-only and injected (`Verifier`): `verified`, `not_verified` or `needs_human` (a login, CAPTCHA,
2FA or payment step is on screen). `needs_human` at any checkpoint requests a takeover and stops. The default verifier
answers `not_verified`, so without a reader a finished run says "done, not visually confirmed", never "verified".
"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

from app.providers.maritime.models import (
    ActionRefused,
    Frame,
    HumanInControl,
    RunOutcome,
    StepRecord,
    Verdict,
    Verifier,
    default_verifier,
)

if TYPE_CHECKING:
    from app.providers.maritime.client import Desktop

logger = logging.getLogger(__name__)

# Geometry of the 1200x750 frame (physical 1280x800) learned from the live run. The frame size is fixed by Maritime.
NEW_ITEM_FIELD = (733, 256)
PAGE_LOAD_WAIT_S = 3.0
SETTLE_WAIT_S = 1.0

MAX_ITEMS = 10
MAX_ITEM_CHARS = 40

NEEDS_HUMAN_HINT = "Answer needs_human if a sign-in, CAPTCHA, 2FA or payment step is on screen."
# The questions handed to the verifier (the service layer wires it to SubQ Snap `verify_screenshot`).
Q_INCOGNITO = "Is this a Chromium Incognito (private) browser window? " + NEEDS_HUMAN_HINT
Q_PAGE_READY = (
    "Is the TodoMVC page loaded, showing the 'todos' heading and an empty 'What needs to be done?' field? "
    + NEEDS_HUMAN_HINT
)
Q_SIGN_IN = (
    "Is a sign-in or login form with a password field on screen that a person must complete? "
    "Answer needs_human if so; answer verified if the page loaded and shows no sign-in, CAPTCHA, 2FA or payment step."
)


def q_items_visible(items: list[str]) -> str:
    listed = ", ".join(f"'{item}'" for item in items)
    return f"Does the todo list on screen show all of these items: {listed}? " + NEEDS_HUMAN_HINT


_URL_RE = re.compile(r"https?://[^\s\x00-\x1f]+")


def valid_target_url(url: object) -> bool:
    return isinstance(url, str) and len(url) <= 2000 and bool(_URL_RE.fullmatch(url))


def valid_items(items: object) -> list[str] | None:
    """The items, stripped, if they are 1..10 short printable single-line strings; else None."""
    if not isinstance(items, list | tuple) or not 1 <= len(items) <= MAX_ITEMS:
        return None
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if not text or len(text) > MAX_ITEM_CHARS or not text.isprintable():
            return None
        cleaned.append(text)
    return cleaned


def precheck_list_add(target_url: object, items: object) -> RunOutcome | None:
    """A `failed` outcome if the inputs break the plan's rules, else None. Run BEFORE any connection is opened."""
    if valid_items(items) is None:
        return RunOutcome(
            "failed",
            False,
            detail=f"Items must be 1-{MAX_ITEMS} single-line strings of at most {MAX_ITEM_CHARS} characters.",
        )
    if not valid_target_url(target_url):
        return RunOutcome("failed", False, detail="The target page is not a plain http(s) URL.")
    return None


def precheck_page(page_url: object) -> RunOutcome | None:
    if not valid_target_url(page_url):
        return RunOutcome("failed", False, detail="The page is not a plain http(s) URL.")
    return None


TakeoverRequester = Callable[[str], Awaitable[str]]


@dataclass
class _Plan:
    """Shared step bookkeeping for both plans."""

    desktop: "Desktop"
    on_step: Callable[[StepRecord], None]
    verifier: Verifier
    request_takeover: TakeoverRequester
    index: int = 0
    last: Frame | None = None
    incognito: bool = False
    notes: list[str] = field(default_factory=list)

    def record(self, action: str, ok: bool, note: str = "", frame: Frame | None = None) -> None:
        frame = frame if frame is not None else self.last
        ref = f"frame:{frame.frame_id}" if frame is not None and frame.frame_id is not None else None
        self.on_step(StepRecord(self.index, action, ok, note, ref))
        self.index += 1

    async def act(self, label: str, action: str, note: str = "", **args: object) -> Frame:
        """One allowlisted action, then the screen after it. The step is logged once the frame is in hand."""
        frame = await self.desktop.act_and_see(action, **args)
        self.last = frame
        self.record(label, True, note, frame)
        return frame

    async def look(self, label: str) -> Frame:
        frame = await self.desktop.see()
        self.last = frame
        self.record(label, True, f"frame {frame.frame_id}, {frame.width}x{frame.height}", frame)
        return frame

    async def verify(self, label: str, question: str) -> Verdict:
        """Ask the verifier about the latest frame. A verifier that fails or answers oddly says `not_verified`."""
        assert self.last is not None
        try:
            verdict = await anyio.to_thread.run_sync(self.verifier, self.last.image, question)
        except Exception as exc:  # a verifier outage must never crash a run or claim success
            logger.warning("maritime: verifier raised %s", type(exc).__name__)
            verdict = "not_verified"
        if verdict not in ("verified", "not_verified", "needs_human"):
            verdict = "not_verified"
        self.record(f"verify: {label}", verdict != "not_verified", verdict)
        return verdict

    @property
    def has_reader(self) -> bool:
        return self.verifier is not default_verifier

    async def hand_to_person(self, reason: str) -> RunOutcome:
        """Login, CAPTCHA, 2FA or payment on screen: ask for a takeover and stop. The computer is left as it is."""
        link = await self.request_takeover(reason)
        self.record("request takeover", True, "waiting for a person (link not logged)")
        return RunOutcome(
            "needs_takeover",
            False,
            takeover_url=link,
            last_screenshot=self.last.image if self.last else None,
            detail=reason,
        )

    def person_already_in_control(self) -> RunOutcome:
        self.record("request takeover", False, "a person already holds the desktop")
        return RunOutcome(
            "needs_takeover",
            False,
            takeover_url=None,
            last_screenshot=self.last.image if self.last else None,
            detail="A person is in control of the computer; wait until they hand it back.",
        )

    def refused(self, why: str) -> RunOutcome:
        self.record("action refused by Maritime", False, why)
        return RunOutcome(
            "failed",
            False,
            last_screenshot=self.last.image if self.last else None,
            detail=f"Maritime refused an action: {why}",
        )

    async def open_page(self, url: str) -> RunOutcome | None:
        """Front the browser, open a private window and load `url`. A takeover outcome if the verifier asks for one."""
        await self.look("screenshot: desktop before starting")
        await self.act("focus browser (alt+Tab)", "key", "no-op when already in front", text="alt+Tab")
        await self.act("open private window (ctrl+shift+n)", "key", text="ctrl+shift+n")
        verdict = await self.verify("private window", Q_INCOGNITO)
        if verdict == "needs_human":
            return await self.hand_to_person("A person is needed on the browser window before I can continue.")
        self.incognito = verdict == "verified"
        await self.act("focus address bar (ctrl+l)", "key", text="ctrl+l")
        await self.act("type address", "type", url, text=url)
        await self.act("load page (Return)", "key", text="Return")
        await self.act("wait for page", "wait", f"{PAGE_LOAD_WAIT_S:g} s", duration=PAGE_LOAD_WAIT_S)
        return None

    async def close_private_window(self) -> None:
        """Only when the screenshot proved it is an Incognito window (closing the only normal window quits Chromium)."""
        if self.incognito:
            await self.act("close private window (ctrl+shift+w)", "key", text="ctrl+shift+w")
            self.incognito = False


async def run_list_add(
    desktop: "Desktop",
    target_url: str,
    items: list[str],
    on_step: Callable[[StepRecord], None],
    verifier: Verifier,
    request_takeover: TakeoverRequester,
) -> RunOutcome:
    """Add `items` to the list on `target_url` (the todo page), then check the result on screen."""
    # The client checks these before it connects; the plan checks again so it is safe on its own.
    if (refused := precheck_list_add(target_url, items)) is not None:
        return refused
    cleaned = valid_items(items)
    assert cleaned is not None

    plan = _Plan(desktop, on_step, verifier, request_takeover)
    try:
        if (stopped := await plan.open_page(target_url)) is not None:
            return stopped

        verdict = await plan.verify("page ready", Q_PAGE_READY)
        if verdict == "not_verified" and plan.has_reader:
            # One more look: the page may simply be slow. Without a reader there is nothing to re-check.
            await plan.act("wait for page", "wait", f"{PAGE_LOAD_WAIT_S:g} s more", duration=PAGE_LOAD_WAIT_S)
            verdict = await plan.verify("page ready (retry)", Q_PAGE_READY)
        if verdict == "needs_human":
            return await plan.hand_to_person(
                "A sign-in, CAPTCHA or payment step is blocking the page I was asked to use."
            )

        await plan.act(
            "focus new-item field (click)", "left_click", f"at {NEW_ITEM_FIELD}", coordinate=list(NEW_ITEM_FIELD)
        )
        for item in cleaned:
            await plan.act(f"type item: {item}", "type", text=item)
            await plan.act(f"add item (Return): {item}", "key", text="Return")
        await plan.act("wait for list", "wait", f"{SETTLE_WAIT_S:g} s", duration=SETTLE_WAIT_S)

        final = await plan.verify("items visible", q_items_visible(cleaned))
        if final == "needs_human":
            return await plan.hand_to_person("A person is needed on the page before I can finish adding the items.")
        screenshot = plan.last.image if plan.last else None
        await plan.close_private_window()
        if final == "verified":
            return RunOutcome(
                "succeeded",
                True,
                last_screenshot=screenshot,
                detail=f"Added {', '.join(cleaned)}; the list on screen shows them.",
            )
        detail = (
            "Every step ran, but the final screenshot was not confirmed: check the screenshot before relying on it."
        )
        return RunOutcome("succeeded", False, last_screenshot=screenshot, detail=detail)
    except HumanInControl:
        return plan.person_already_in_control()
    except ActionRefused as exc:
        return plan.refused(str(exc))


async def run_sign_in_check(
    desktop: "Desktop",
    page_url: str,
    on_step: Callable[[StepRecord], None],
    verifier: Verifier,
    request_takeover: TakeoverRequester,
) -> RunOutcome:
    """Load `page_url`; if a sign-in form is on screen, hand the desktop to a person. Never fills anything in."""
    if (refused := precheck_page(page_url)) is not None:
        return refused

    plan = _Plan(desktop, on_step, verifier, request_takeover)
    try:
        if (stopped := await plan.open_page(page_url)) is not None:
            return stopped
        verdict = await plan.verify("sign-in wall", Q_SIGN_IN)
        if verdict == "needs_human":
            return await plan.hand_to_person("This page needs a person to sign in. I will not type credentials.")
        screenshot = plan.last.image if plan.last else None
        await plan.close_private_window()
        if verdict == "verified":
            return RunOutcome(
                "succeeded", True, last_screenshot=screenshot, detail="The page loaded and shows no sign-in step."
            )
        return RunOutcome(
            "succeeded",
            False,
            last_screenshot=screenshot,
            detail="The page loaded, but I could not tell whether it needs a sign-in.",
        )
    except HumanInControl:
        return plan.person_already_in_control()
    except ActionRefused as exc:
        return plan.refused(str(exc))
