"""The interactive terminal line editor (ADR-0040).

The defect these tests pin was a real transcript: arrow keys were *submitted* as ``^[[D``/``^[[C``,
CJK deletes left undecodable bytes behind, and the user was told their input contained characters
the build could not read. Nothing here tests a string sanitizer in isolation — every case drives
the real ``prompt_toolkit`` editor through its own pipe input, with a dummy output, and asserts the
text the editor actually returned.

Two boundaries are checked at the same time:

* the editor consumes editing keys, so no escape sequence can be part of a submitted line;
* the text that leaves the editor is validated again, so a hostile or broken terminal still cannot
  put a NUL, a lone surrogate or a raw ESC into a conversation turn.
"""

from __future__ import annotations

import ast
import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import PipeInput, create_pipe_input
from prompt_toolkit.output import DummyOutput

from assistant.application.conversation_input import (
    ConsoleInput,
    InputOutcome,
    InteractiveConversationInput,
    UserInput,
    build_input_source,
    validate_submitted_text,
)

LEFT = "\x1b[D"
RIGHT = "\x1b[C"
HOME = "\x1b[H"
END = "\x1b[F"
DELETE = "\x1b[3~"
BACKSPACE = "\x7f"
UP = "\x1b[A"
DOWN = "\x1b[B"
ENTER = "\r"
CTRL_C = "\x03"
CTRL_D = "\x04"
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"

MAIL_LINE = '发信地址:251880385@smail.nju.edu.cn.收信地址"刘夏芸"<251880557@smail.nju.edu.cn>'


@contextmanager
def editing() -> Iterator[tuple[PipeInput, InteractiveConversationInput]]:
    """A real editor session driven by scripted key events, with no terminal attached."""
    with create_pipe_input() as pipe:
        session: PromptSession[str] = PromptSession(
            input=pipe, output=DummyOutput(), history=InMemoryHistory()
        )
        yield pipe, InteractiveConversationInput(session=session)


def submit(pipe: PipeInput, reader: InteractiveConversationInput, keys: str) -> UserInput:
    """Send one line of keys and return exactly what the editor handed back."""
    pipe.send_text(keys + ENTER)
    return reader.read_line("You > ")


# ------------------------------------------------------------------ cursor movement (§5, §10)


def test_left_arrow_inserts_in_the_middle_of_a_chinese_number() -> None:
    """The documented example: 仙一17 -> left -> 0 -> 仙一107."""
    with editing() as (pipe, reader):
        line = submit(pipe, reader, "仙一17" + LEFT + "0")

    assert line.outcome is InputOutcome.OK
    assert line.text == "仙一107"


def test_right_arrow_moves_back_over_inserted_text() -> None:
    with editing() as (pipe, reader):
        line = submit(pipe, reader, "仙一17" + LEFT + "0" + RIGHT + "X")

    assert line.text == "仙一107X"


def test_home_and_end_reach_both_ends_of_a_mixed_line() -> None:
    with editing() as (pipe, reader):
        line = submit(pipe, reader, "ABC" + HOME + "X" + END + "Y")

    assert line.text == "XABCY"


def test_moving_left_over_ascii_and_inserting_keeps_every_character() -> None:
    """The documented example: ABC -> left left -> X -> AXBC."""
    with editing() as (pipe, reader):
        line = submit(pipe, reader, "ABC" + LEFT + LEFT + "X")

    assert line.text == "AXBC"


def test_an_arrow_escape_sequence_never_becomes_conversation_text() -> None:
    with editing() as (pipe, reader):
        line = submit(pipe, reader, "ics的地点是在仙一107" + LEFT * 4 + RIGHT * 2)

    assert line.text == "ics的地点是在仙一107"
    assert "\x1b" not in line.text
    assert "[D" not in line.text and "[C" not in line.text


# --------------------------------------------------------------- deletion of CJK (§5, §6, §10)


def test_backspace_deletes_one_whole_chinese_character() -> None:
    """The documented example: 诚园三舍X -> backspace -> 诚园三舍."""
    with editing() as (pipe, reader):
        line = submit(pipe, reader, "诚园三舍X" + BACKSPACE)

    assert line.text == "诚园三舍"
    assert "\ufffd" not in line.text


def test_forward_delete_removes_one_whole_chinese_character() -> None:
    with editing() as (pipe, reader):
        line = submit(pipe, reader, "诚园三舍A232" + HOME + DELETE)

    assert line.text == "园三舍A232"


def test_deleting_across_mixed_chinese_and_ascii_leaves_valid_text() -> None:
    with editing() as (pipe, reader):
        over_the_house = submit(pipe, reader, "诚园三舍A232" + LEFT * 4 + BACKSPACE)
        over_the_three = submit(pipe, reader, "诚园三舍A232" + LEFT * 5 + BACKSPACE)

    # Backspace deletes the character *before* the cursor, and it is a whole character.
    assert over_the_house.text == "诚园三A232"
    assert over_the_three.text == "诚园舍A232"
    assert validate_submitted_text(over_the_house.text) is None
    assert validate_submitted_text(over_the_three.text) is None


def test_repeated_backspace_empties_a_chinese_line_completely() -> None:
    with editing() as (pipe, reader):
        line = submit(pipe, reader, "中国近代史纲要" + BACKSPACE * 7)

    assert line.text == ""


# ------------------------------------------------------------------- Unicode payloads (§7)


@pytest.mark.parametrize(
    "text",
    (
        "仙一107",
        "诚园三舍A232",
        "刘夏芸",
        "生成式软件工程",
        "中国近代史纲要",
        MAIL_LINE,
        "每周二 14:00–16:00 · 计算机系统基础（ICS）（仙一107）",
        "他说“可以”，我说‘好’。",
        "emoji 🎉 和全角字符",
    ),
)
def test_a_committed_unicode_line_survives_editing_unchanged(text: str) -> None:
    with editing() as (pipe, reader):
        line = submit(pipe, reader, text)

    assert line.outcome is InputOutcome.OK
    assert line.text == text


def test_bracketed_paste_arrives_as_one_unmodified_line() -> None:
    with editing() as (pipe, reader):
        line = submit(pipe, reader, PASTE_START + MAIL_LINE + PASTE_END)

    assert line.text == MAIL_LINE
    assert "\x1b" not in line.text


def test_editing_a_pasted_line_keeps_the_pasted_body() -> None:
    with editing() as (pipe, reader):
        line = submit(pipe, reader, PASTE_START + "ics的地点是在仙一17" + PASTE_END + LEFT + "0")

    assert line.text == "ics的地点是在仙一107"


def test_a_pasted_tab_is_text_not_a_key_binding() -> None:
    """§8: a tab is allowed by the existing contract, so a pasted one must survive."""
    with editing() as (pipe, reader):
        line = submit(pipe, reader, PASTE_START + "a\tb" + PASTE_END)

    assert line.text == "a\tb"


# ------------------------------------------------------------------------ history (§5, §11)


def test_up_arrow_recalls_the_previous_submitted_prompt() -> None:
    with editing() as (pipe, reader):
        first = submit(pipe, reader, "我今天有什么事")
        second = submit(pipe, reader, UP)

    assert first.text == "我今天有什么事"
    assert second.text == "我今天有什么事"


def test_history_navigates_forward_again_with_down_arrow() -> None:
    with editing() as (pipe, reader):
        submit(pipe, reader, "第一句")
        submit(pipe, reader, "第二句")
        up = submit(pipe, reader, UP)
        up_up = submit(pipe, reader, UP + UP)
        down = submit(pipe, reader, UP + DOWN)

    # Down past the newest entry returns to the empty working line, not to an older prompt.
    assert (up.text, up_up.text, down.text) == ("第二句", "第一句", "")


def test_editing_history_is_never_written_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9/§24: prompts may be private, so the editor keeps its history in memory only."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    with editing() as (pipe, reader):
        submit(pipe, reader, "记住这个私密的句子")
        submit(pipe, reader, UP)

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert after == before, "the editor wrote something to disk"
    assert not [path for path in home.rglob("*") if path.is_file()]


# ------------------------------------------------------------------ Ctrl-C / Ctrl-D (§9, §12)


def test_ctrl_c_discards_the_unfinished_line() -> None:
    with editing() as (pipe, reader):
        pipe.send_text("写了一半" + CTRL_C)
        cancelled = reader.read_line("You > ")
        # The very next prompt is unaffected: Ctrl-C ended the line, not the editor.
        next_line = submit(pipe, reader, "重新开始")

    assert cancelled.outcome is InputOutcome.CANCELLED
    assert cancelled.is_cancelled is True
    assert cancelled.text == ""
    assert next_line.text == "重新开始"


def test_ctrl_d_on_an_empty_prompt_is_a_clean_close() -> None:
    with editing() as (pipe, reader):
        pipe.send_text(CTRL_D)
        closed = reader.read_line("You > ")

    assert closed.outcome is InputOutcome.CLOSED
    assert closed.is_closed is True


# --------------------------------------------------------- post-edit validation (§8, §25)


@pytest.mark.parametrize(
    "text",
    (
        MAIL_LINE,
        "仙一107",
        "他说“可以”",
        '他说"可以"',
        "<251880557@smail.nju.edu.cn>",
        "括号（全角）和 (half width)",
        "tab\tis intentional",
        "emoji 🎉",
    ),
)
def test_ordinary_text_is_never_rejected_for_its_characters(text: str) -> None:
    assert validate_submitted_text(text) is None


@pytest.mark.parametrize(
    "text",
    (
        "a\x00b",
        "a\x1bb",
        "a\x1b[Db",
        "a\x07b",
        "a\x7fb",
        "a\ud800b",
    ),
)
def test_text_that_could_not_have_been_typed_is_rejected(text: str) -> None:
    assert validate_submitted_text(text) is not None


class _ScriptedSession:
    """A session double that hands back one already-returned string."""

    def __init__(self, text: str) -> None:
        self._text = text

    def prompt(self, message: str = "") -> str:
        return self._text


def test_an_editor_that_returns_an_escape_sequence_is_rejected() -> None:
    """Even a hypothetical broken editor cannot smuggle a raw arrow sequence into a turn."""
    reader = InteractiveConversationInput(
        session=_ScriptedSession("ics的地点是在仙一107\x1b[D")  # type: ignore[arg-type]
    )

    line = reader.read_line("You > ")

    assert line.outcome is InputOutcome.DECODE_FAILED
    assert "escape" in (line.detail or "")


def test_an_editor_failure_is_a_value_not_a_traceback() -> None:
    class _Exploding:
        def prompt(self, message: str = "") -> str:
            raise OSError("the terminal went away")

    reader = InteractiveConversationInput(session=_Exploding())  # type: ignore[arg-type]

    line = reader.read_line("You > ")

    assert line.outcome is InputOutcome.DECODE_FAILED
    assert "OSError" in (line.detail or "")


# ------------------------------------------------------------------- TTY detection (§5, §25)


class _FakeTerminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakePipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_two_real_terminals_get_the_line_editor() -> None:
    source = build_input_source(stdin=_FakeTerminal(), stdout=_FakeTerminal())

    assert isinstance(source, InteractiveConversationInput)


@pytest.mark.parametrize(
    ("stdin_stream", "stdout_stream"),
    (
        (_FakePipe(), _FakePipe()),
        (_FakeTerminal(), _FakePipe()),
        (_FakePipe(), _FakeTerminal()),
    ),
)
def test_anything_but_two_terminals_keeps_the_strict_decoder(
    stdin_stream: io.StringIO, stdout_stream: io.StringIO
) -> None:
    source = build_input_source(stdin=stdin_stream, stdout=stdout_stream)

    assert isinstance(source, ConsoleInput)


def test_a_stream_without_isatty_is_not_treated_as_a_terminal() -> None:
    class _NoProbe(io.StringIO):
        isatty = None  # type: ignore[assignment]

    source = build_input_source(stdin=_NoProbe(), stdout=_FakeTerminal())

    assert isinstance(source, ConsoleInput)


def test_the_editor_imports_no_domain_or_service_module() -> None:
    """§25: the editing layer cannot interpret anything, because it cannot reach anything."""
    source_root = Path(__file__).resolve().parents[2] / "src" / "assistant"
    tree = ast.parse((source_root / "application" / "conversation_input.py").read_text("utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)

    for forbidden in ("assistant.store", "assistant.adapters", "assistant.domain"):
        assert not [module for module in modules if module.startswith(forbidden)], forbidden
