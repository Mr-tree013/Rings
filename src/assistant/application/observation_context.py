"""The bounded, untrusted context a web/manual analysis is allowed to see (ADR-0029).

Two rules shape this module, and both are about not handing a model more than a person asked it to
look at:

- **a manual input is quoted text.** It is passed as a JSON field, with the source name beside it,
  and nothing else: no knowledge base, no facts, no tasks, no calendar, no mail, no playbooks, no
  actions. Forwarded text is somebody else's words, and the prompt says so.
- **a page change is a diff, not a page.** Sending a whole site to a provider would be unbounded in
  cost and useless in signal, so the context is built deterministically with `difflib`: the lines
  that appeared, the lines that disappeared, and a bounded excerpt of where the page stands now.
  The model never receives a URL to fetch and never needs one — the target id is enough, and a URL
  in a context is an invitation to try.

The fingerprint is a hash of the exact JSON that will be sent, which is what makes an analysis
reusable across EventWorker retries and impossible to confuse with an analysis of different
content.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass

from assistant.domain.manual_input import ManualInput
from assistant.domain.web_watch import WebObservation
from assistant.ports.content_snapshot_store import ContentSnapshotStore
from assistant.ports.web_watch_repository import WebWatchRepository

MANUAL_TEXT_BUDGET = 12_000
"""How much pasted text one analysis may carry."""

ADDED_TEXT_BUDGET = 3_000
REMOVED_TEXT_BUDGET = 3_000
CURRENT_EXCERPT_BUDGET = 6_000
TOTAL_CONTEXT_BUDGET = 12_000
"""The change-context budget: added + removed + excerpt, capped in total as well as in parts."""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def context_fingerprint(payload: dict[str, object]) -> str:
    """The canonical hash of one exactly-bounded context document."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservationContext:
    """The untrusted data document sent to a model, plus its fingerprint."""

    source_kind: str
    payload: dict[str, object]
    fingerprint: str
    source_identities: tuple[str, ...] = ()

    @property
    def document(self) -> str:
        """The payload as the compact JSON the prompt quotes as data."""
        return json.dumps(
            self.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


class ObservationContextBuilder:
    """Builds the bounded context for a manual input or a web change."""

    def __init__(
        self,
        snapshots: ContentSnapshotStore,
        observations: WebWatchRepository,
    ) -> None:
        self._snapshots = snapshots
        self._observations = observations

    async def for_manual(self, manual_input: ManualInput) -> ObservationContext:
        """Context for one pasted input: its source name and its bounded text."""
        payload: dict[str, object] = {
            "source": "manual.input.received",
            "input_source": manual_input.source.value,
            "text": _truncate(manual_input.text, MANUAL_TEXT_BUDGET),
        }
        return ObservationContext(
            source_kind="manual.input.received",
            payload=payload,
            fingerprint=context_fingerprint(payload),
            source_identities=(f"manual-input:{manual_input.id}", manual_input.content_sha256),
        )

    async def for_web_change(self, observation: WebObservation) -> ObservationContext:
        """Context for one page change: a deterministic line diff plus a bounded excerpt."""
        current = await self._snapshots.read_text(observation.storage_key)
        previous_text = ""
        previous_sha: str | None = None
        if observation.previous_observation_id is not None:
            previous = await self._observations.get_observation(
                observation.previous_observation_id
            )
            if previous is not None:
                previous_sha = previous.content_sha256
                previous_text = await self._snapshots.read_text(previous.storage_key)
        added, removed = _diff_lines(previous_text, current)
        payload: dict[str, object] = {
            "source": "web.page.changed",
            "target_id": observation.target_id,
            "previous_sha256": previous_sha,
            "current_sha256": observation.content_sha256,
            "added_text": _truncate("\n".join(added), ADDED_TEXT_BUDGET),
            "removed_text": _truncate("\n".join(removed), REMOVED_TEXT_BUDGET),
            "current_excerpt": _truncate(current, CURRENT_EXCERPT_BUDGET),
        }
        payload = _fit_total_budget(payload)
        identities = [f"observation:{observation.id}", observation.content_sha256]
        if previous_sha is not None:
            identities.append(previous_sha)
        return ObservationContext(
            source_kind="web.page.changed",
            payload=payload,
            fingerprint=context_fingerprint(payload),
            source_identities=tuple(identities),
        )


def _diff_lines(previous: str, current: str) -> tuple[list[str], list[str]]:
    """Deterministic line diff: what appeared, and what disappeared."""
    diff = difflib.unified_diff(
        previous.splitlines(), current.splitlines(), lineterm="", n=0
    )
    added: list[str] = []
    removed: list[str] = []
    for line in diff:
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def _fit_total_budget(payload: dict[str, object]) -> dict[str, object]:
    """Shrink the excerpt first and the diff last, until the whole document fits the budget."""
    fitted = dict(payload)
    total = _total_length(fitted)
    for field in ("current_excerpt", "added_text", "removed_text"):
        if total <= TOTAL_CONTEXT_BUDGET:
            break
        value = str(fitted[field])
        keep = max(0, len(value) - (total - TOTAL_CONTEXT_BUDGET))
        fitted[field] = value[:keep]
        total -= len(value) - keep
    return fitted


def _total_length(payload: dict[str, object]) -> int:
    return sum(
        len(str(payload[field]))
        for field in ("added_text", "removed_text", "current_excerpt")
    )


__all__ = [
    "ADDED_TEXT_BUDGET",
    "CURRENT_EXCERPT_BUDGET",
    "MANUAL_TEXT_BUDGET",
    "REMOVED_TEXT_BUDGET",
    "TOTAL_CONTEXT_BUDGET",
    "ObservationContext",
    "ObservationContextBuilder",
    "context_fingerprint",
]
