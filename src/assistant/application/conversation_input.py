"""The terminal input boundary (ADR-0035 §4-§6, §10-§11; ADR-0040).

`input()` is a trap for a long-lived product surface: when the terminal hands over bytes the
declared encoding cannot decode, it raises `UnicodeDecodeError` out of the middle of the prompt
loop, and the session ends. When the interpreter is configured with `surrogateescape` instead
(which is what this project's WSL reference environment does), the same bytes come back as lone
surrogates — a string that cannot be re-encoded and therefore is *not* what the user typed.

Both shapes are handled the same way here, and neither is allowed to become an intention:

```text
                 ┌────────────────────────────────────────────┐
                 │ stdin and stdout are real terminals?       │
                 └───────────────┬─────────────────┬──────────┘
                                 │ yes             │ no
                                 ▼                 ▼
                    InteractiveConversationInput  ConsoleInput
                    (prompt_toolkit: arrows,      (strict byte decode:
                     Home/End, Del, CJK width)     declared → locale → UTF-8)
                                 │                 │
                                 └────────┬────────┘
                                          ▼
                                 validate_submitted_text
                                          │
                          ┌───────────────┴───────────────┐
                          ▼                               ▼
                        OK                    DECODE_FAILED / CANCELLED
                                          (no turn, no model call, no mutation)
```

Nothing is ever replaced with `U+FFFD`: a repaired byte sequence could mean something the user did
not say, and the caller cannot tell the difference afterwards.

Two properties of the *interactive* half are worth stating, because ADR-0040 exists for them:

* line editing is a real terminal editor, not this project's own raw-byte loop. Arrow keys are
  editing commands consumed by the editor, never bytes that can reach a conversation turn, and
  Backspace/Delete operate on Unicode text, so deleting one Chinese character deletes one character
  and not one UTF-8 byte;
* the text the editor returns is validated again — no NUL, no lone surrogate, no raw escape
  sequence, no unintended C0 control — before it can become a turn. A rejection is a value, never
  an exception, so a hostile or broken terminal cannot end the session.

The editing history is in-memory only (ADR-0040 §9): a user prompt may contain private data, and
the durable record of what was said is the conversation itself, not a shell history file.
"""

from __future__ import annotations

import locale
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, BinaryIO, Protocol, TextIO, cast

if TYPE_CHECKING:  # pragma: no cover - the import exists for type checkers only
    from prompt_toolkit import PromptSession


class InputOutcome(StrEnum):
    """What one read produced."""

    OK = "ok"
    DECODE_FAILED = "decode_failed"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    """Ctrl-C: the unfinished line is discarded and the session continues (ADR-0040 §11)."""


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

    @property
    def is_cancelled(self) -> bool:
        """Whether the human abandoned the unfinished line."""
        return self.outcome is InputOutcome.CANCELLED


class ConversationInputSource(Protocol):
    """The narrow boundary `cli_chat` reads through, so tests can stay deterministic.

    Two implementations exist and they are deliberately different: the interactive one edits with
    a mature line editor, the scripted one decodes bytes strictly (ADR-0040 §5).
    """

    def read_line(self, prompt: str) -> UserInput: ...


def candidate_encodings(stream: TextIO | None = None) -> tuple[str, ...]:
    """The encodings to try, in order, based on what the environment actually declares.

    The stream's own encoding comes first: that is the only evidence about *this* terminal. The
    locale's preferred encoding and UTF-8 follow, so a misdeclared stream still has a documented
    second and third chance. Nothing here guesses a legacy code page.
    """
    declared = [
        _encoding_of(stream),
        _encoding_of(sys.stdin),
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


def _encoding_of(stream: TextIO | None) -> str | None:
    """The encoding a stream declares, or `None` when it declares none at all."""
    if stream is None:
        return None
    try:
        return stream.encoding
    except AttributeError:  # a test double, not a real stream
        return None


class ConsoleInput:
    """Reads one line at a time from a stream, without ever letting it crash the session.

    This is the non-interactive half of the boundary: piped input, a script's stdin, or the
    `CliRunner`'s injected bytes. It never installs a terminal editor, which is exactly why it
    keeps the Phase-10C fail-closed behaviour unchanged.
    """

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
        try:
            buffer = self._stream.buffer
        except AttributeError:
            # A stream without a binary buffer (a test double, a StringIO): read text directly.
            return _TextAsBytes(self._stream)
        return cast("BinaryIO", buffer)

    def _write_prompt(self, prompt: str) -> None:
        try:
            self._output.write(prompt)
            self._output.flush()
        except (OSError, ValueError):  # pragma: no cover - a closed terminal
            pass


class _BytesReader(Protocol):
    """The tiny binary interface `read_line` needs."""

    def readline(self) -> bytes: ...


class InteractiveConversationInput:
    """Reads one line from a real terminal through `prompt_toolkit` (ADR-0040).

    The editor owns the interesting parts: cursor movement, Home/End, Delete, CJK display width
    (through `wcwidth`), bracketed paste, and a session-local Up/Down history. This class owns the
    boundary around them: turn whatever the terminal does into a `UserInput` value, validate the
    submitted text once more, and never let a terminal-level failure traceback through the UI.

    History is intentionally `InMemoryHistory`: nothing about a user's prompts is written to
    `~/.history`, `~/.rings_history` or any other file (ADR-0040 §9, §24).
    """

    def __init__(self, *, session: PromptSession[str] | None = None) -> None:
        self._session = session

    def read_line(self, prompt: str) -> UserInput:
        """Edit and submit one line, or explain why there is none."""
        try:
            session = self._session or self._open_session()
            text = session.prompt(prompt)
        except KeyboardInterrupt:
            # Ctrl-C is the human's, not a failure: discard the line, keep the session.
            return UserInput(outcome=InputOutcome.CANCELLED)
        except EOFError:
            # Ctrl-D on an empty prompt: leave cleanly.
            return UserInput(outcome=InputOutcome.CLOSED)
        except Exception as exc:  # pragma: no cover - a broken terminal, never a normal path
            return UserInput(
                outcome=InputOutcome.DECODE_FAILED,
                detail=f"terminal editor failed: {type(exc).__name__}",
            )
        reason = validate_submitted_text(text)
        if reason is not None:
            return UserInput(outcome=InputOutcome.DECODE_FAILED, detail=reason)
        return UserInput(outcome=InputOutcome.OK, text=text)

    def _open_session(self) -> PromptSession[str]:
        session = _new_prompt_session()
        self._session = session
        return session


def _new_prompt_session() -> PromptSession[str]:
    """Build the editor session: one per `rings` process, so history is session-local."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    return PromptSession(history=InMemoryHistory())


def build_input_source(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> ConversationInputSource:
    """Choose the input path from what stdin and stdout actually are (ADR-0040 §5).

    Only two real terminals get the editor. A pipe, a redirect, a test double or a closed stream
    keeps the strict deterministic path, so scripted input is never forced through a TTY.
    """
    stream_in = sys.stdin if stdin is None else stdin
    stream_out = sys.stdout if stdout is None else stdout
    if _is_attached_to_a_terminal(stream_in) and _is_attached_to_a_terminal(stream_out):
        return InteractiveConversationInput()
    return ConsoleInput(stream=stream_in, output=stream_out)


def _is_attached_to_a_terminal(stream: TextIO) -> bool:
    """Whether this stream is a real interactive terminal, never a guess from an environment."""
    try:
        probe = stream.isatty
    except AttributeError:  # a test double that does not model a terminal at all
        return False
    try:
        return bool(probe())
    except (OSError, TypeError, ValueError):  # pragma: no cover - closed or detached stream
        return False


_ALLOWED_CONTROL_CHARACTERS = frozenset({"\t"})
"""The one C0 control the existing contract allows through as text (ADR-0040 §8)."""


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


def validate_submitted_text(text: str) -> str | None:
    """The second check every submitted line passes, editor or not (ADR-0040 §8).

    Returns the reason a line is unusable, or `None` when it is. The rule is deliberately narrow:
    Chinese punctuation, quotes, angle brackets, `@`, emoji and tabs are ordinary text and pass.
    What cannot pass is text that is not what the user could have meant:

    * a NUL byte, which no hand types;
    * a lone surrogate, which cannot be re-encoded and therefore cannot be what was said;
    * a raw ESC or any other unintended C0 control, which is a terminal control sequence —
      `^[[D` must never arrive here from the editor, and if it somehow does, it must not become a
      conversation turn.
    """
    if "\x00" in text:
        return "the input contains a NUL byte"
    if _has_surrogates(text):
        return "the input contains a lone surrogate"
    for character in text:
        if character == "\x1b":
            return "the input contains a raw terminal escape sequence"
        code = ord(character)
        is_c0 = code < 0x20 or code == 0x7F
        if is_c0 and character not in _ALLOWED_CONTROL_CHARACTERS:
            return f"the input contains a control character (U+{code:04X})"
    return None


__all__ = [
    "ConsoleInput",
    "ConversationInputSource",
    "InputOutcome",
    "InteractiveConversationInput",
    "UserInput",
    "build_input_source",
    "candidate_encodings",
    "decode_strictly",
    "validate_submitted_text",
]
