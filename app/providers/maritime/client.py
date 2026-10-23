"""Wire layer for Maritime Computers over MCP. Only reached when ENABLE_MARITIME is on and a key is present.

One client serves one end user: it is built from the server-computed pinned URL (`MaritimeService.pinned_mcp_url`) and
authenticates with `MARITIME_COMPUTERS_KEY`, which never leaves this module. Allowlists, enforced in `Session.call` and
`Desktop.act` (the only two doors to the wire):

- tools: get_computer, computer, request_takeover, takeover_status, close_computer
- `computer` actions: screenshot, left_click, double_click, type, key, wait, scroll, zoom

run_shell, read_file, write_file, android and computer_batch are exposed by the same server on the same key and are
never called: a call for any of them raises `ForbiddenOperation` before a byte leaves the process. Nothing here accepts
a tool name, action or argument from model output; the fixed plans live in `runner.py`.

The takeover link is a secret (anyone holding it controls the desktop). It is returned to the caller and nowhere else:
never logged, never part of an error reason, never in a repr.

Source of truth for every wire fact: docs/research/2026-10-22-maritime-facts.md.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from app.errors import ProviderUnavailable
from app.providers.maritime import runner
from app.providers.maritime.models import (
    ActionRefused,
    ForbiddenOperation,
    Frame,
    HumanInControl,
    RunOutcome,
    StepRecord,
    TakeoverMode,
    TakeoverState,
    Verifier,
    default_verifier,
)
from app.settings import Settings

logger = logging.getLogger(__name__)

PROVIDER = "maritime"

ALLOWED_TOOLS = frozenset({"get_computer", "computer", "request_takeover", "takeover_status", "close_computer"})
# Exposed by the same server and the same key, so the "no shell" rule is enforced here, on the client.
FORBIDDEN_TOOLS = frozenset({"run_shell", "read_file", "write_file", "android", "computer_batch"})
ALLOWED_ACTIONS = frozenset({"screenshot", "left_click", "double_click", "type", "key", "wait", "scroll", "zoom"})
# Keyword arguments an action may carry. `computer_id` is set by `Desktop` itself and can never be overridden.
_ACTION_ARGS = frozenset(
    {
        "coordinate",
        "text",
        "duration",
        "scroll_direction",
        "scroll_amount",
        "region",
        "no_screenshot",
        "format",
        "quality",
    }
)

# `get_computer` creates the desktop on first use: 3-8 s normally, up to 60 s cold. Most MCP clients cut a call at 60 s.
GET_COMPUTER_TIMEOUT_S = 120.0
ACTION_TIMEOUT_S = 60.0
TAKEOVER_TIMEOUT_S = 30.0
CLOSE_TIMEOUT_S = 20.0
_HTTP_TIMEOUT = httpx2.Timeout(connect=15.0, read=150.0, write=30.0, pool=15.0)

# Error slugs (facts file section 1.5). These mean "Maritime cannot serve us right now": the browser task is shown as
# `unavailable` with the reason, never as a success and never as a crash.
_UNAVAILABLE_SLUGS = {
    "computer_limit": "computer limit reached or no paid plan (computer_limit)",
    "concurrency": "too many computers awake for the plan, retry shortly (concurrency)",
    "plan_lapsed": "Maritime plan lapsed (plan_lapsed)",
    "no_plan": "no Maritime Computers plan (no_plan)",
    "no_capacity": "Maritime has no capacity right now (no_capacity)",
    "computers_disabled": "Maritime Computers is disabled (computers_disabled)",
    "wake_transient": "the computer failed to wake, retry shortly (wake_transient)",
    "wake_timeout": "the computer timed out waking up (wake_timeout)",
    "not_found": "the computer was not found; it may have been deleted (not_found)",
}
_SLUG_RE = re.compile(r"\b(human_in_control|" + "|".join(_UNAVAILABLE_SLUGS) + r")\b")
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_KEY_RE = re.compile(r"\bmk_[A-Za-z0-9_\-]{6,}")
_MAX_REASON_CHARS = 160


def _tls_verify() -> ssl.SSLContext | bool:
    """Default trust store, or the bundle named by SSL_CERT_FILE (httpx2 does not read it on its own)."""
    cafile = os.environ.get("SSL_CERT_FILE", "")
    if cafile and os.path.isfile(cafile):
        return ssl.create_default_context(cafile=cafile)
    return True


# --- reading tool results --------------------------------------------------------------------------------------------


@dataclass
class ToolReply:
    texts: list[str] = field(default_factory=list)
    images: list[tuple[bytes, str]] = field(default_factory=list)  # (decoded bytes, mime type)
    structured: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


def _read_result(result: Any) -> tuple[ToolReply, bool]:
    reply = ToolReply()
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            reply.texts.append(block.text)
        elif kind == "image":
            try:
                reply.images.append((base64.b64decode(block.data), block.mime_type or "image/jpeg"))
            except ValueError:
                logger.warning("maritime: an image block was not valid base64")
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        reply.structured = structured
    return reply, bool(getattr(result, "is_error", False))


def _json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in `text`, if the whole text or a `{...}` span of it parses."""
    text = text.strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _field(reply: ToolReply, *names: str) -> Any:
    """A named value from structured content, else a JSON text block, else `name: value` / `name=value` prose."""
    sources: list[dict[str, Any]] = []
    if reply.structured:
        sources.append(reply.structured)
    prose: list[str] = []
    for text in reply.texts:
        parsed = _json_object(text)
        if parsed is not None:
            sources.append(parsed)
        else:
            prose.append(text)
    for source in sources:
        for name in names:
            if source.get(name) is not None:
                return source[name]
    # Prose is only searched in text that is NOT a JSON object: a body such as `{"error":null,...}` has already been
    # read exactly above, and the loose pattern below would turn its `null` into a value.
    joined = "\n".join(prose)
    for name in names:
        match = re.search(rf"\b{re.escape(name)}\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.\-]+)", joined, re.IGNORECASE)
        if match and match.group(1).lower() not in ("null", "none"):
            return match.group(1)
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def frame_from_reply(reply: ToolReply) -> Frame | None:
    if not reply.images:
        return None
    image, mime = reply.images[0]
    return Frame(
        image=image,
        mime=mime,
        frame_id=_int_or_none(_field(reply, "frame_id", "frameId")),
        width=_int_or_none(_field(reply, "width")),
        height=_int_or_none(_field(reply, "height")),
    )


# --- turning failures into ProviderUnavailable -----------------------------------------------------------------------


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


class _Translator:
    """Knows the secrets to scrub, and maps wire failures onto `ProviderUnavailable`."""

    def __init__(self, settings: Settings) -> None:
        self._secrets = tuple(s for s in (settings.maritime_computers_key.strip(),) if len(s) >= 6)

    def clean(self, text: str) -> str:
        """Single line, no key, no URL (a takeover link is a URL), capped."""
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        text = _KEY_RE.sub("[redacted]", text)
        text = _URL_RE.sub("[url]", text)
        return " ".join(text.split())[:_MAX_REASON_CHARS] or "unknown error"

    def from_tool_error(self, text: str) -> Exception:
        match = _SLUG_RE.search(text)
        if match and match.group(1) == "human_in_control":
            return HumanInControl("a person is in control of the desktop")
        if match:
            exc = ProviderUnavailable(PROVIDER, _UNAVAILABLE_SLUGS[match.group(1)])
            if match.group(1) == "not_found":
                # Read by `guarded_call`: Maritime replied (that computer is gone), so the provider itself is fine.
                exc.provider_answered = True  # type: ignore[attr-defined]
            return exc
        return ProviderUnavailable(PROVIDER, f"tool error: {self.clean(text)}")

    def from_exception(self, exc: BaseException) -> ProviderUnavailable:
        if isinstance(exc, MCPError):
            return ProviderUnavailable(PROVIDER, f"MCP error {exc.code}: {self.clean(exc.message)}")
        if isinstance(exc, httpx2.HTTPStatusError):
            return ProviderUnavailable(PROVIDER, f"HTTP {exc.response.status_code}")
        if isinstance(exc, TimeoutError | httpx2.TimeoutException):
            return ProviderUnavailable(PROVIDER, "timed out waiting for Maritime")
        # Only the type: the message of a transport error may echo a URL or a header.
        return ProviderUnavailable(PROVIDER, f"connection failed ({type(exc).__name__})")

    @contextlib.contextmanager
    def translated(self):  # noqa: ANN202
        try:
            yield
        except (ProviderUnavailable, HumanInControl, ForbiddenOperation):
            raise
        except (Exception, BaseExceptionGroup) as exc:
            leaf = _leaf(exc)
            if isinstance(leaf, ProviderUnavailable | HumanInControl | ForbiddenOperation):
                raise leaf from None
            if isinstance(leaf, asyncio.CancelledError):
                raise
            raise self.from_exception(leaf) from None


# --- the two doors to the wire ---------------------------------------------------------------------------------------


class Session:
    """One open MCP connection. `call` is the only way a tool request leaves the process."""

    def __init__(self, mcp: Any, translator: _Translator) -> None:
        self._mcp = mcp
        self._translator = translator

    def clean(self, text: str) -> str:
        return self._translator.clean(text)

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float = ACTION_TIMEOUT_S) -> ToolReply:
        if tool not in ALLOWED_TOOLS:
            raise ForbiddenOperation(f"tool {tool!r} is not allowlisted")
        started = time.monotonic()
        with anyio.fail_after(timeout + 10):
            result = await self._mcp.call_tool(tool, arguments, read_timeout_seconds=timeout)
        reply, is_error = _read_result(result)
        elapsed = time.monotonic() - started
        if is_error:
            # The text of an error can name a path or echo input; log the tool and the outcome only.
            logger.info("maritime tool=%s error after %.1fs", tool, elapsed)
            raise self._translator.from_tool_error(reply.text)
        logger.debug("maritime tool=%s ok in %.1fs", tool, elapsed)
        return reply


class Desktop:
    """One computer, driven through the `computer` tool with the action allowlist enforced."""

    def __init__(self, session: Session, computer_id: str) -> None:
        self._session = session
        self._computer_id = computer_id

    async def act(self, action: str, **args: Any) -> Frame | None:
        """Run one allowlisted action; returns the post-action screenshot when the server sent one."""
        if action not in ALLOWED_ACTIONS:
            raise ForbiddenOperation(f"computer action {action!r} is not allowlisted")
        unknown = set(args) - _ACTION_ARGS
        if unknown:
            raise ForbiddenOperation(f"computer argument(s) {sorted(unknown)} are not allowlisted")
        payload = {
            **{k: v for k, v in args.items() if v is not None},
            "computer_id": self._computer_id,
            "action": action,
        }
        reply = await self._session.call("computer", payload, timeout=ACTION_TIMEOUT_S)
        # Maritime reports a refused action in the result body too (`error`, `blocked`, `reason`; all null when fine,
        # and `ok` / `is_error` null too, as recorded live). An explicit `ok: false` or `is_error: true` counts as one.
        refusal = _field(reply, "error") or _field(reply, "blocked")
        if not refusal and (_field(reply, "ok") is False or _field(reply, "is_error", "isError") is True):
            refusal = "the action was not accepted"
        if refusal:
            why = _field(reply, "reason") or refusal
            raise ActionRefused(self._session.clean(str(why)))
        return frame_from_reply(reply)

    async def see(self) -> Frame:
        """A fresh screenshot. Coordinates in the next action refer to this frame."""
        frame = await self.act("screenshot")
        if frame is None:
            raise ProviderUnavailable(PROVIDER, "screenshot returned no image")
        return frame

    async def act_and_see(self, action: str, **args: Any) -> Frame:
        """Run an action and return the screen after it (the post-action frame the server sent, else a fresh one)."""
        frame = await self.act(action, **args)
        return frame if frame is not None else await self.see()


# --- the client ------------------------------------------------------------------------------------------------------

Connector = Callable[[], AbstractAsyncContextManager[Any]]


def _run_sync[T](func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """Run an async operation to completion from sync code: a fresh event loop per call (the server is stateless).

    The worker is sync and has no loop. If a caller does sit inside a running loop, the operation runs on a helper
    thread with its own loop instead of failing with "asyncio.run() cannot be called from a running event loop".
    """
    runner = partial(anyio.run, partial(func, *args, **kwargs))
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return runner()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="maritime") as pool:
        return pool.submit(runner).result()


class MaritimeClient:
    def __init__(
        self,
        settings: Settings,
        pinned_url: str,
        *,
        connect: Connector | None = None,
        verifier: Verifier | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """`connect` and `transport` are test seams: an in-process MCP connector, or a stand-in HTTP transport."""
        self._settings = settings
        self._pinned_url = pinned_url
        self._transport = transport
        self._connect = connect or self._connect_http
        self._verifier: Verifier = verifier or default_verifier
        self._translator = _Translator(settings)

    def __repr__(self) -> str:  # the key lives in settings and in the HTTP client, never here
        return f"MaritimeClient(pinned_url={self._pinned_url!r})"

    # connection ---------------------------------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _connect_http(self) -> AsyncIterator[Client]:
        """Streamable HTTP to the pinned URL with `Authorization: Bearer <computers key>` (SDK 2.x speaks httpx2)."""
        headers = {"Authorization": f"Bearer {self._settings.maritime_computers_key.strip()}"}
        verify = _tls_verify()
        async with httpx2.AsyncClient(
            headers=headers, timeout=_HTTP_TIMEOUT, verify=verify, transport=self._transport
        ) as http:
            transport = streamable_http_client(self._pinned_url, http_client=http, terminate_on_close=False)
            async with Client(transport, read_timeout_seconds=ACTION_TIMEOUT_S) as mcp:
                yield mcp

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[Session]:
        """An open connection with every failure already mapped to `ProviderUnavailable` / `HumanInControl`."""
        with self._translator.translated():
            async with self._connect() as mcp:
                yield Session(mcp, self._translator)

    # operations (async) -------------------------------------------------------------------------------------------

    async def aget_computer(self) -> str:
        """The user's computer id. `user_id` is omitted: the connection is already pinned, a mismatch is refused."""
        async with self.session() as session:
            reply = await session.call("get_computer", {}, timeout=GET_COMPUTER_TIMEOUT_S)
        computer_id = _field(reply, "computer_id", "computerId", "id")
        if not isinstance(computer_id, str) or not computer_id.strip():
            raise ProviderUnavailable(PROVIDER, "get_computer returned no computer id")
        return computer_id.strip()

    async def arequest_takeover(self, computer_id: str, reason: str) -> str:
        async with self.session() as session:
            return await self._request_takeover(session, computer_id, reason)

    async def _request_takeover(self, session: Session, computer_id: str, reason: str) -> str:
        reply = await session.call(
            "request_takeover",
            {"computer_id": computer_id, "reason": reason[:300], "wait": False},
            timeout=TAKEOVER_TIMEOUT_S,
        )
        link = _field(reply, "url", "link", "viewer_url", "takeover_url")
        if not (isinstance(link, str) and link.startswith(("http://", "https://"))):
            match = _URL_RE.search(reply.text)
            link = match.group(0) if match else None
        if not link:
            raise ProviderUnavailable(PROVIDER, "request_takeover returned no control link")
        return link

    async def atakeover_state(self, computer_id: str) -> TakeoverState:
        async with self.session() as session:
            reply = await session.call("takeover_status", {"computer_id": computer_id}, timeout=TAKEOVER_TIMEOUT_S)
        mode = _field(reply, "mode")
        mode = mode.strip().lower() if isinstance(mode, str) else ""
        result = _field(reply, "last_takeover_result", "lastTakeoverResult")
        note = _field(reply, "last_takeover_note", "lastTakeoverNote")
        expires = _field(reply, "takeover_expires_at", "takeoverExpiresAt")
        return TakeoverState(
            mode=mode if mode in ("agent", "human") else "unknown",  # type: ignore[arg-type]
            result=result if result in ("success", "failed", "expired") else None,
            note=self._translator.clean(note) if isinstance(note, str) else None,
            expires_at=expires if isinstance(expires, str) else None,
        )

    async def atakeover_status(self, computer_id: str) -> TakeoverMode:
        return (await self.atakeover_state(computer_id)).mode

    async def aclose_computer(self, computer_id: str) -> None:
        async with self.session() as session:
            await session.call("close_computer", {"computer_id": computer_id}, timeout=CLOSE_TIMEOUT_S)

    # operations (sync, for the worker) ----------------------------------------------------------------------------

    def get_computer(self) -> str:
        """The user's computer id. Omit `user_id` from the tool call: the connection is already pinned."""
        return _run_sync(self.aget_computer)

    def request_takeover(self, computer_id: str, reason: str) -> str:
        """`request_takeover(wait=false)`; returns the control link, which is a secret."""
        return _run_sync(self.arequest_takeover, computer_id, reason)

    def takeover_status(self, computer_id: str) -> TakeoverMode:
        """`human` while the person holds the desktop, `agent` once control is back, `unknown` if unreadable."""
        return _run_sync(self.atakeover_status, computer_id)

    def takeover_state(self, computer_id: str) -> TakeoverState:
        """Mode plus how the last takeover ended. `failed` or `expired` means the person did not finish: no retry."""
        return _run_sync(self.atakeover_state, computer_id)

    def close_computer(self, computer_id: str) -> None:
        _run_sync(self.aclose_computer, computer_id)

    async def _run_plan(
        self,
        computer_id: str,
        plan: Callable[..., Awaitable[RunOutcome]],
        page: str,
        extra: list[str] | None,
        on_step: Callable[[StepRecord], None],
        verifier: Verifier | None,
    ) -> RunOutcome:
        """Open one connection, run a fixed plan on it and close the computer afterwards (unless a person needs it).

        A takeover link dies when the session closes, so a run that ends in `needs_takeover` (or finds a person already
        at the desktop) leaves the computer open; every other ending, including an error, closes it, best effort.
        """
        verifier = verifier or self._verifier
        keep_open = False
        async with self.session() as session:
            desktop = Desktop(session, computer_id)

            async def request_takeover(reason: str) -> str:
                return await self._request_takeover(session, computer_id, reason)

            try:
                args = (page, extra) if extra is not None else (page,)
                outcome = await plan(desktop, *args, on_step, verifier, request_takeover)
                keep_open = outcome.status == "needs_takeover"
                return outcome
            except HumanInControl:
                keep_open = True
                raise
            finally:
                if not keep_open:
                    with contextlib.suppress(Exception):
                        await session.call("close_computer", {"computer_id": computer_id}, timeout=CLOSE_TIMEOUT_S)

    async def arun_list_add(
        self,
        computer_id: str,
        target_url: str,
        items: list[str],
        on_step: Callable[[StepRecord], None],
        verifier: Verifier | None = None,
    ) -> RunOutcome:
        if (refused := runner.precheck_list_add(target_url, items)) is not None:
            return refused  # bad input never opens a connection, let alone touches the computer
        return await self._run_plan(computer_id, runner.run_list_add, target_url, items, on_step, verifier)

    async def arun_sign_in_check(
        self,
        computer_id: str,
        page_url: str,
        on_step: Callable[[StepRecord], None],
        verifier: Verifier | None = None,
    ) -> RunOutcome:
        if (refused := runner.precheck_page(page_url)) is not None:
            return refused
        return await self._run_plan(computer_id, runner.run_sign_in_check, page_url, None, on_step, verifier)

    def run_list_add(
        self,
        computer_id: str,
        target_url: str,
        items: list[str],
        on_step: Callable[[StepRecord], None],
        verifier: Verifier | None = None,
    ) -> RunOutcome:
        """The fixed `list_add_items` plan: screenshot -> act -> verify per step, then close the computer.

        Login, CAPTCHA, 2FA or payment on screen => `request_takeover` and `RunOutcome(status="needs_takeover")`.
        `verifier` defaults to the one given at construction, which defaults to "never verified".
        """
        return _run_sync(self.arun_list_add, computer_id, target_url, items, on_step, verifier)

    def run_sign_in_check(
        self,
        computer_id: str,
        page_url: str,
        on_step: Callable[[StepRecord], None],
        verifier: Verifier | None = None,
    ) -> RunOutcome:
        """Load a page; if a sign-in form is shown, request a takeover. Never types credentials."""
        return _run_sync(self.arun_sign_in_check, computer_id, page_url, on_step, verifier)
