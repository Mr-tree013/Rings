"""The terminal input boundary (ADR-0035 §4-§6, §10-§11).

`input()` is a trap for a long-lived product surface: when the terminal hands over bytes the
declared encoding cannot decode, it raises `UnicodeDecodeError` out of the middle of the prompt
loop, and the session ends. When the interpreter is configured with `surrogateescape` instead
(which is what this project's WSL reference environment does), the same bytes come back as lone
surrogates — a string that cannot be re-encoded and therefore is *not* what the user typed.

Both shapes are handled the same way here, and neither is allowed to become an intention:

```text
raw bytes ──► strict decode (declared → locale-preferred → UTF-8)
                  │
        ┌─────────┴──────────┐
        ▼                    ▼
   decoded text        no candidate decodes it,
   without surrogates  or surrogates remain
        │                    │
        ▼                    ▼
      OK                DECODE_FAILED   (no turn, no model call, no mutation)
```

Nothing is ever replaced with `U+FFFD`: a repaired byte sequence could mean something the user did
not say, and the caller cannot tell the difference afterwards.
"""

from __future__ import annotations

import locale
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO, Protocol, TextIO, cast


class InputOutcome(StrEnum):
    """What one read produced."""

    OK = "ok"
    DECODE_FAILED = "decode_failed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class UserInput:
    """One line of user input, or the reason there is none."""

    outcome: InputOutcome
    text: str = ""
    detail: str | None = None

    @property
    def is_text(self) -> bool:
        """Whether this is a usable line."""
        return self.outcome is InputOutcome.OK

    @property
    def is_decode_failure(self) -> bool:
        """Whether the terminal produced bytes this build cannot read."""
        return self.outcome is InputOutcome.DECODE_FAILED

    @property
    def is_closed(self) -> bool:
        """Whether the input stream ended."""
        return self.outcome is InputOutcome.CLOSED


def candidate_encodings(stream: TextIO | None = None) -> tuple[str, ...]:
    """The encodings to try, in order, based on what the environment actually declares.

    The stream's own encoding comes first: that is the only evidence about *this* terminal. The
    locale's preferred encoding and UTF-8 follow, so a misdeclared stream still has a documented
    second and third chance. Nothing here guesses a legacy code page.
    """
    declared = [
        getattr(stream, "encoding", None) if stream is not None else None,
        getattr(sys.stdin, "encoding", None),
        locale.getpreferredencoding(False),
        "utf-8",
    ]
    ordered: list[str] = []
    for name in declared:
        if not name:
            continue
        normalised = name.lower().replace("_", "-")
        if normalised in {"utf8", "utf-8", "utf-8-sig"}:
            normalised = "utf-8"
        if normalised not in ordered:
            ordered.append(normalised)
    return tuple(ordered)


class ConsoleInput:
    """Reads one line at a time from a terminal, without ever letting it crash the session."""

    def __init__(
        self,
        *,
        buffer: BinaryIO | None = None,
        stream: TextIO | None = None,
        output: TextIO | None = None,
    ) -> None:
        self._buffer = buffer
        self._stream = stream if stream is not None else sys.stdin
        self._output = output if output is not None else sys.stdout

    def read_line(self, prompt: str) -> UserInput:
        """Show `prompt` and read one line, or explain why it could not be read."""
        self._write_prompt(prompt)
        reader = self._reader()
        try:
            raw = reader.readline()
        except (OSError, ValueError) as exc:  # pragma: no cover - terminal-level failure
            return UserInput(
                outcome=InputOutcome.CLOSED, detail=f"{type(exc).__name__}"
            )
        if raw == b"":
            return UserInput(outcome=InputOutcome.CLOSED)
        text, detail = decode_strictly(raw, stream=self._stream)
        if text is None:
            return UserInput(outcome=InputOutcome.DECODE_FAILED, detail=detail)
        return UserInput(outcome=InputOutcome.OK, text=text.rstrip("\r\n"))

    # ------------------------------------------------------------------------ internals

    def _reader(self) -> BinaryIO | _BytesReader:
        if self._buffer is not None:
            return self._buffer
        buffer = cast("BinaryIO | None", getattr(self._stream, "buffer", None))
        if buffer is not None:
            return buffer
        # A stream without a binary buffer (a test double, a StringIO): read text directly.
        return _TextAsBytes(self._stream)

    def _write_prompt(self, prompt: str) -> None:
        try:
            self._output.write(prompt)
            self._output.flush()
        except (OSError, ValueError):  # pragma: no cover - a closed terminal
            pass


class _BytesReader(Protocol):
    """The tiny binary interface `read_line` needs."""

    def readline(self) -> bytes: ...


class _TextAsBytes:
    """Adapt a text stream to that interface."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def readline(self) -> bytes:
        line = self._stream.readline()
        if line == "":
            return b""
        return line.encode("utf-8", errors="surrogatepass")


def decode_strictly(
    raw: bytes, *, stream: TextIO | None = None
) -> tuple[str | None, str | None]:
    """Decode one line, or explain why it is not usable.

    Returns `(text, None)` on success and `(None, reason)` when no candidate encoding decodes the
    bytes, when the decoded text carries lone surrogates, or when the line contains a NUL byte.
    """
    if b"\x00" in raw:
        return None, "the input contains a NUL byte"
    attempts: list[str] = []
    for encoding in candidate_encodings(stream):
        try:
            text = raw.decode(encoding, errors="strict")
        except (UnicodeDecodeError, LookupError) as exc:
            attempts.append(f"{encoding}: {type(exc).__name__}")
            continue
        if _has_surrogates(text):
            attempts.append(f"{encoding}: lone surrogates")
            continue
        return text, None
    return None, "; ".join(attempts) or "no candidate encoding"


def _has_surrogates(text: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in text)


__all__ = [
    "ConsoleInput",
    "InputOutcome",
    "UserInput",
    "candidate_encodings",
    "decode_strictly",
]
