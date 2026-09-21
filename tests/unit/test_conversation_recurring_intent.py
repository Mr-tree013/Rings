"""Statements, instructions and unsupported recurrence (ADR-0036 §12, §19).

Pure text policy, so it is tested as text: no database, no service, no model. The property that
matters is the asymmetry — a phrase that is *not* in the mutation list costs one extra question,
while a phrase that does not belong in it would write durable state the user only described.
"""

from __future__ import annotations

import pytest

from assistant.application.conversation_recurring_intent import (
    MUTATION_PHRASES,
    UNSUPPORTED_RECURRENCE_PHRASES,
    declares_mutation,
    unsupported_recurrence,
)


@pytest.mark.parametrize(
    "text",
    (
        "我每周一十点到十二点有课。",
        "周一早上十点到十二点是计算机系统基础课",
        "每周三下午两点到四点都有软件工程课",
        "我还有一门课是周二晚上七点到九点",
        "这学期每周五上午有实验",
        "",
        "   ",
    ),
)
def test_a_statement_is_not_an_instruction(text: str) -> None:
    assert declares_mutation(text) is False


@pytest.mark.parametrize(
    "text",
    (
        "每周一十点到十二点有课，记下来。",
        "记录一下，每周三下午两点到四点是软件工程课。",
        "把这门课加到日历里。",
        "帮我放进固定安排：每周五上午十点有实验。",
        "做一下周计划，把我的每周的课放进去。",
        "保存下来：每周二晚上七点到九点有课。",
        "添加到固定安排：每周四下午两点到四点。",
    ),
)
def test_an_instruction_is_an_instruction(text: str) -> None:
    assert declares_mutation(text) is True


@pytest.mark.parametrize("phrase", MUTATION_PHRASES)
def test_every_declared_mutation_phrase_is_recognised(phrase: str) -> None:
    assert declares_mutation(f"每周一十点到十二点有课，{phrase}。")


@pytest.mark.parametrize("phrase", UNSUPPORTED_RECURRENCE_PHRASES)
def test_every_unsupported_phrase_is_named(phrase: str) -> None:
    assert unsupported_recurrence(f"每周一十点到十二点有课，{phrase}。") == phrase


@pytest.mark.parametrize(
    "text",
    (
        "每周一十点到十二点有课，记下来。",
        "每周一和周三下午两点到四点都有软件工程课，记录下来。",
        "周一早上十点到十二点是计算机系统基础课",
    ),
)
def test_supported_weekly_text_names_nothing_unsupported(text: str) -> None:
    assert unsupported_recurrence(text) is None


def test_the_specific_phrase_is_the_one_quoted_back() -> None:
    """「每个月第一周」 is more useful to the user than the fragment 「每月」."""
    assert unsupported_recurrence("每个月第一周的周三下午有课") == "每个月"
    assert unsupported_recurrence("单双周上课") == "单双周"
    assert unsupported_recurrence("考试周除外") == "考试周"
