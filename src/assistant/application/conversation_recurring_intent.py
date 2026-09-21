"""Stating a weekly commitment is not the same as asking for one (ADR-0036 §19-§20).

Two deterministic checks live here, because both decide whether durable state may be written and
neither is something a model should be trusted with:

```text
"我每周一十点到十二点有课。"          → a statement. Ask before writing; zero rules either way.
"记下来" / "加到日历" / "放进固定安排" → an instruction. The existing local write path may run.
"每两周一次" / "单双周" / "节假日除外" → unsupported. Refuse with zero mutation.
```

This module is pure text policy: no I/O, no repository, no model, no clock. It cannot create
anything, which is why it is safe to run before every recurring write.
"""

from __future__ import annotations

MUTATION_PHRASES: tuple[str, ...] = (
    "记下来",
    "记录下来",
    "记录一下",
    "记录下",
    "记一下",
    "帮我记",
    "替我记",
    "给我记",
    "记进",
    "记到",
    "加到日历",
    "加进日历",
    "加入日历",
    "加到固定安排",
    "加入固定安排",
    "加到我的日历",
    "添加固定",
    "加进",
    "放进",
    "放进去",
    "排进",
    "存下来",
    "保存下来",
    "保存到",
    "存进",
    "添加",
)
"""Ways a user asks for a durable weekly commitment. Deliberately explicit words only.

"有课" is a statement about the world; "记下来" is a request to change the system. The list is
narrow on purpose: a phrase that is not here costs one extra question, and a phrase that does not
belong here would write state the user only described.
"""

UNSUPPORTED_RECURRENCE_PHRASES: tuple[str, ...] = (
    "单双周",
    "单周",
    "双周",
    "隔周",
    "隔一周",
    "每隔一周",
    "每两周",
    "两周一次",
    "每三周",
    "每月",
    "每个月",
    "月末",
    "月底",
    "每月第",
    "每季度",
    "每半年",
    "每年",
    "寒暑假",
    "节假日",
    "法定假",
    "放假",
    "考试周",
)
"""Recurrence v1.1 cannot express. Named rather than approximated (ADR-0036 §12)."""


def declares_mutation(text: str) -> bool:
    """Whether the message itself asks Tree to change durable state.

    Only the user's own words count: the check reads the message, never a model's summary of it.
    """
    cleaned = text.strip()
    if not cleaned:
        return False
    return any(phrase in cleaned for phrase in MUTATION_PHRASES)


def unsupported_recurrence(text: str) -> str | None:
    """The first unsupported recurrence the message names, or `None`.

    The longest matching phrase is returned, so the refusal quotes the specific thing the user
    wrote ("每个月第一周") rather than the shortest fragment of it that also matches.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    matches = [phrase for phrase in UNSUPPORTED_RECURRENCE_PHRASES if phrase in cleaned]
    if not matches:
        return None
    # `max` keeps the first of equal-length matches, so the result is the declaration order.
    return max(matches, key=len)


__all__ = [
    "MUTATION_PHRASES",
    "UNSUPPORTED_RECURRENCE_PHRASES",
    "declares_mutation",
    "unsupported_recurrence",
]
