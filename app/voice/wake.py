"""Wake gate (spec 22.5): decides from a final transcript whether Uhlexa should answer, and strips the wake phrase.

Matching runs on text, not audio, so it works with any STT. "Alexa" is accepted only because STT often hears "Uhlexa"
that way; the product name is always Uhlexa.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

WakeMode = Literal["wake_word", "follow_up", "always"]
WAKE_MODES: tuple[str, ...] = ("wake_word", "follow_up", "always")

_PHRASES = (
    "uhlexa",
    "hey uhlexa",
    "ok uhlexa",
    "ulexa",
    "u lexa",
    "uh lexa",
    "ooh lexa",
    "hulexa",
    "alexa",
    "hey alexa",
)
# Longest first, so "hey uhlexa" wins over a shorter match.
_PHRASE_WORDS = sorted((tuple(p.split()) for p in _PHRASES), key=len, reverse=True)
_WORD = re.compile(r"[A-Za-z0-9']+")
_LEADING_JUNK = re.compile(r"^[\s\W_]+")


@dataclass(frozen=True)
class WakeDecision:
    respond: bool
    text: str = ""
    bare_wake: bool = False


def _split_phrase(transcript: str) -> tuple[bool, str]:
    """(phrase found at the start, the original text after it)."""
    spans = list(_WORD.finditer(transcript))
    words = [m.group().lower().replace("'", "") for m in spans]
    for phrase in _PHRASE_WORDS:
        if tuple(words[: len(phrase)]) == phrase:
            rest = transcript[spans[len(phrase) - 1].end() :]
            return True, _LEADING_JUNK.sub("", rest).strip()
    return False, transcript.strip()


def canonical_wake(transcript: str) -> str:
    """Spell a leading wake phrase as "Uhlexa" for captions (STT tends to write "Alexa,"). Other text is untouched."""
    found, rest = _split_phrase(transcript)
    if not found:
        return transcript
    return f"Uhlexa, {rest}" if rest else "Uhlexa."


class WakeGate:
    def __init__(self, mode: str, follow_up_s: float = 8.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._mode = self._checked(mode)
        self._follow_up_s = follow_up_s
        self._clock = clock
        self._open_until = float("-inf")

    @staticmethod
    def _checked(mode: str) -> WakeMode:
        if mode not in WAKE_MODES:
            raise ValueError(f"unknown wake mode: {mode!r}")
        return mode  # type: ignore[return-value]

    @property
    def mode(self) -> WakeMode:
        return self._mode

    @property
    def awake(self) -> bool:
        return self._mode == "always" or self._clock() <= self._open_until

    def set_mode(self, mode: str) -> None:
        self._mode = self._checked(mode)

    def open_window(self) -> None:
        """Wake phrase heard or tap-to-talk: listen without a phrase for `follow_up_s`."""
        self._open_until = self._clock() + self._follow_up_s

    def reply_finished(self) -> None:
        """Uhlexa stopped speaking. Follow-up mode keeps listening for a while; wake-word mode goes back to sleep."""
        if self._mode == "follow_up":
            self.open_window()
        elif self._mode == "wake_word":
            self._open_until = float("-inf")

    def decide(self, transcript: str) -> WakeDecision:
        if not _WORD.search(transcript or ""):
            return WakeDecision(False)
        found, text = _split_phrase(transcript)
        if found and not text:
            self.open_window()
            return WakeDecision(True, "", bare_wake=True)
        if found or self.awake:
            return WakeDecision(True, text)
        return WakeDecision(False)
