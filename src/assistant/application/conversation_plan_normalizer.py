"""Benign structured-output variation, repaired without changing meaning (ADR-0035 §4-§6).

Providers legitimately answer a three-branch schema in more than one shape: `operations: null`
where the schema says array, a missing `reply` on a clarification, a null `clarification` on a
direct reply. Refusing those is not safety, it is noise — and noise is what pushed users into
schema errors in real use.

The rule this module follows is narrow on purpose:

* it may **fill in an absence** where the schema already defines the meaning (`missing`/`null`
  `operations` → `[]`, `missing` `reply`/`clarification` → `None`);
* it may never **invent a value, choose an operation, guess an argument or relax a bound**.

Anything else is left exactly as the provider sent it, so the closed schema and the capability
layer still get to refuse it. That is what keeps "normalize benign variation" from becoming
"accept whatever the model felt like saying".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from assistant.domain.conversation_plan import ConversationPlanMode

_MODES = {mode.value for mode in ConversationPlanMode}
_NULLABLE_TEXT_FIELDS = ("reply", "clarification")


def normalize_wire_plan(payload: object) -> tuple[dict[str, Any] | None, str | None]:
    """Return `(normalized, None)` or `(None, issue)`.

    The issue is a short, developer-facing phrase. It is never shown to a user: the caller turns a
    failed normalization into a repair attempt and then into a friendly sentence.
    """
    if not isinstance(payload, Mapping):
        return None, "the answer is not a JSON object"
    mode = payload.get("mode")
    if not isinstance(mode, str) or mode not in _MODES:
        return None, "the answer does not name one of the three modes"
    normalized: dict[str, Any] = dict(payload)
    for field in _NULLABLE_TEXT_FIELDS:
        # An absent field and an explicit null mean the same thing to this mode's branch of the
        # schema, so both are filled in as `None` and nothing else is touched.
        if normalized.get(field) is None:
            normalized[field] = None
    operations = normalized.get("operations")
    if operations is None:
        # `null` where the schema says array is the single most common benign variation.
        normalized["operations"] = []
    elif not isinstance(operations, list):
        return None, "operations is neither a list nor null"
    entries: list[Any] = []
    for entry in normalized["operations"]:
        if isinstance(entry, Mapping):
            candidate = dict(entry)
            if candidate.get("note") is None and "note" not in candidate:
                candidate["note"] = None
            entries.append(candidate)
        else:
            entries.append(entry)
    normalized["operations"] = entries
    return normalized, None


def describe_issue(issue: str) -> str:
    """A compact phrase for the repair request: never a JSON pointer or a validator name."""
    return issue.strip()[:200]


__all__ = ["describe_issue", "normalize_wire_plan"]
