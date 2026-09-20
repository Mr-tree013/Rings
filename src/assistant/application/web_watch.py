"""Observing configured public pages, durably and without surprises (ADR-0029).

```text
configured target ──► resolve + fetch ──► normalize ──► hash
       │                 (adapter: no redirects, public addresses, byte cap)
       ▼
  hash unchanged ──► refresh state only                     (no event)
  first success  ──► baseline observation                  (no event, ever)
  hash changed   ──► observation == state, one transaction
       │
       ▼
  InboundEvent(web.page.changed) named by observation id   (idempotent, repairable)
```

Everything here is built so that a watcher can be trusted to be *boring*. The baseline rule means
turning a watcher on never replays a site's history as "news". Conditional validators are treated
as an optimization, with a periodic unconditional fetch that re-establishes correctness. A failed
target is a result, not an exception that stops its siblings. And the event that leaves this module
names an observation; the page itself never leaves the snapshot store.

The bridge is repaired on every round, like mail synchronization: an observation committed without
its event is ingested late, and an event written without its link is recognised as a duplicate and
linked. Both crash windows close by re-running the same idempotent ingest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum

from assistant.application.event_inbox import (
    EventInbox,
    IngestDisposition,
    IngestEvent,
    IngestResult,
)
from assistant.domain.config import WatchersConfig, WebTargetConfig
from assistant.domain.errors import (
    WebObservationNotFound,
    WebSnapshotStorageError,
    WebTargetNotFound,
    WebWatchRedirectNotAllowed,
    WebWatchRequestFailed,
    WebWatchResponseTooLarge,
    WebWatchUnsafeAddress,
    WebWatchUnsafeUrl,
    WebWatchUnsupportedContentType,
)
from assistant.domain.inbound_event import EventId
from assistant.domain.web_watch import (
    WebObservation,
    WebObservationId,
    WebWatchState,
)
from assistant.ports.clock import Clock
from assistant.ports.event_repository import EventRepository
from assistant.ports.interval_waiter import IntervalWaiter
from assistant.ports.web_source import WebFetchRequest, WebFetchStatus, WebSource
from assistant.ports.web_watch_repository import WebWatchRepository

LOGGER = logging.getLogger("assistant.watchers")

WEB_EVENT_TYPE = "web.page.changed"
"""The `InboundEvent` type a changed observation is bridged as."""

BRIDGE_REPAIR_LIMIT = 50
"""How many unlinked observations one round may repair, so recovery stays bounded."""

OPERATIONAL_ERRORS = (
    WebWatchUnsafeUrl,
    WebWatchUnsafeAddress,
    WebWatchRedirectNotAllowed,
    WebWatchUnsupportedContentType,
    WebWatchResponseTooLarge,
    WebWatchRequestFailed,
    WebSnapshotStorageError,
)
"""Failures that belong to one target: a fetch problem, not a broken program.

The web source raises these for everything the outside world can do to a watcher — an unsafe
address, a redirect, an unsupported type, an oversize body, a timeout, an unwritable snapshot. None
of them may stop the other targets or the rest of the daemon, so `sync_once` records them per
target; anything else (a broken invariant, an unreadable row) propagates.
"""


class TargetOutcome(StrEnum):
    """What one poll did to one target."""

    BASELINE = "baseline"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WebTargetResult:
    """The result of polling one target, whether it worked or not."""

    target_id: str
    url: str
    outcome: TargetOutcome
    observation_id: WebObservationId | None = None
    event_id: EventId | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether this target was observed without an operational failure."""
        return self.outcome is not TargetOutcome.FAILED


class WebWatchService:
    """Fetches configured pages, records what changed, and bridges the changes."""

    name = "web-watch"
    """The daemon service name, so the supervisor can log and restart it independently."""

    def __init__(
        self,
        source: WebSource,
        repository: WebWatchRepository,
        events: EventRepository,
        clock: Clock,
        config: WatchersConfig,
        *,
        waiter: IntervalWaiter | None = None,
        repair_limit: int = BRIDGE_REPAIR_LIMIT,
    ) -> None:
        self._source = source
        self._repository = repository
        self._inbox = EventInbox(events, clock)
        self._clock = clock
        self._config = config
        self._waiter = waiter
        self._repair_limit = repair_limit

    @property
    def targets(self) -> tuple[WebTargetConfig, ...]:
        """The enabled targets this service watches."""
        return self._config.enabled_targets

    # ------------------------------------------------------------------ polling

    async def sync_once(
        self, target_id: str | None = None
    ) -> tuple[WebTargetResult, ...]:
        """Poll one target or every enabled target, then repair the event bridge.

        Raises:
            WebTargetNotFound: `target_id` is not a configured target.
        """
        selected = self._select(target_id)
        results: list[WebTargetResult] = []
        for target in selected:
            try:
                results.append(await self._sync_target(target))
            except OPERATIONAL_ERRORS as exc:
                # One unreachable page is that page's problem. The next target still runs, and so
                # does the rest of the daemon.
                LOGGER.warning("watcher %s failed: %s", target.id, exc)
                results.append(
                    WebTargetResult(
                        target_id=target.id,
                        url=target.url,
                        outcome=TargetOutcome.FAILED,
                        error=str(exc),
                    )
                )
        await self.repair_bridge()
        return tuple(results)

    async def _sync_target(self, target: WebTargetConfig) -> WebTargetResult:
        now = self._clock.now()
        stored = await self._repository.get_state(target.id)
        reset = stored is not None and stored.url != target.url
        state = None if reset or stored is None else stored
        url = stored.url if (stored is not None and not reset) else target.url
        conditional = (
            None
            if state is None
            else state.conditional_headers(
                full_fetch_every=self._config.full_fetch_every, reset=reset
            )
        )
        request = WebFetchRequest(
            target_id=target.id,
            url=target.url,
            max_bytes=self._config.max_response_bytes,
            timeout_seconds=self._config.timeout_seconds,
            etag=None if conditional is None else conditional[0],
            last_modified=None if conditional is None else conditional[1],
        )
        result = await self._source.fetch(request)
        # A full fetch is the periodic proof that the server's validators were telling the truth, so
        # the counter restarts there rather than accumulating; a conditional fetch advances it.
        counters = 0 if conditional is None or state is None else state.checks_since_full + 1
        if result.status is WebFetchStatus.NOT_MODIFIED:
            await self._repository.save_state(
                WebWatchState(
                    target_id=target.id,
                    url=url,
                    content_sha256=None if state is None else state.content_sha256,
                    latest_observation_id=None if state is None else state.latest_observation_id,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    checks_since_full=counters,
                    last_checked_at=now,
                    last_changed_at=None if state is None else state.last_changed_at,
                    updated_at=now,
                )
            )
            return WebTargetResult(
                target_id=target.id, url=url, outcome=TargetOutcome.UNCHANGED
            )
        if result.content_sha256 is None or result.storage_key is None:
            raise WebWatchRequestFailed(
                target.url, "the source returned content without a hash or a storage key"
            )
        if state is not None and state.content_sha256 == result.content_sha256:
            await self._repository.save_state(
                WebWatchState(
                    target_id=target.id,
                    url=url,
                    content_sha256=state.content_sha256,
                    latest_observation_id=state.latest_observation_id,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    checks_since_full=counters,
                    last_checked_at=now,
                    last_changed_at=state.last_changed_at,
                    updated_at=now,
                )
            )
            return WebTargetResult(
                target_id=target.id, url=url, outcome=TargetOutcome.UNCHANGED
            )
        baseline = state is None or not state.has_baseline
        previous_changed_at = None if state is None else state.last_changed_at
        previous_observation_id = (
            None if baseline or state is None else state.latest_observation_id
        )
        observation = WebObservation(
            target_id=target.id,
            url=url,
            content_sha256=result.content_sha256,
            storage_key=result.storage_key,
            previous_observation_id=previous_observation_id,
            is_baseline=baseline,
            fetched_at=now,
        )
        await self._repository.record_observation(
            observation,
            WebWatchState(
                target_id=target.id,
                url=url,
                content_sha256=observation.content_sha256,
                latest_observation_id=observation.id,
                etag=result.etag,
                last_modified=result.last_modified,
                checks_since_full=counters,
                last_checked_at=now,
                last_changed_at=previous_changed_at if baseline else now,
                updated_at=now,
            ),
        )
        if baseline:
            # §18: enabling a watcher establishes a baseline; it does not announce the whole site.
            LOGGER.info("watcher %s baseline recorded", target.id)
            return WebTargetResult(
                target_id=target.id,
                url=url,
                outcome=TargetOutcome.BASELINE,
                observation_id=observation.id,
            )
        event_id, _ = await self._bridge(observation)
        LOGGER.info("watcher %s changed", target.id)
        return WebTargetResult(
            target_id=target.id,
            url=url,
            outcome=TargetOutcome.CHANGED,
            observation_id=observation.id,
            event_id=event_id,
        )

    def _select(self, target_id: str | None) -> tuple[WebTargetConfig, ...]:
        targets = self.targets
        if target_id is None:
            return targets
        for target in targets:
            if target.id == target_id:
                return (target,)
        raise WebTargetNotFound(target_id)

    # ------------------------------------------------------------------- bridge

    async def repair_bridge(self) -> tuple[int, int]:
        """Bridge observations with no `InboundEvent` link yet.

        Returns `(events_created, links_repaired)`. An observation committed just before a crash
        produces its event here; an event written without its link is recognised as a duplicate and
        simply linked.
        """
        unlinked = await self._repository.list_unlinked_observations(
            limit=self._repair_limit
        )
        created = repaired = 0
        for observation in unlinked:
            _, disposition = await self._bridge(observation)
            if disposition is IngestDisposition.CREATED:
                created += 1
            else:
                repaired += 1
        return created, repaired

    async def _bridge(
        self, observation: WebObservation
    ) -> tuple[EventId, IngestDisposition]:
        result = await self._ingest(observation)
        await self._link(observation, result.event.id)
        return result.event.id, result.disposition

    async def _ingest(self, observation: WebObservation) -> IngestResult:
        """Ingest the event that names this observation, without the page content."""
        return await self._inbox.ingest(
            IngestEvent(
                source=f"web:{observation.target_id}",
                event_type=WEB_EVENT_TYPE,
                external_id=f"observation:{observation.id}",
                content=json.dumps(
                    {
                        "observation_id": str(observation.id),
                        "target_id": observation.target_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )

    async def _link(self, observation: WebObservation, event_id: EventId) -> None:
        await self._repository.link_event(
            observation.id, event_id, linked_at=self._clock.now()
        )

    # ------------------------------------------------------------------ reads

    async def observations(
        self, *, target_id: str | None = None, limit: int | None = 20
    ) -> list[WebObservation]:
        """List recorded observations, newest first."""
        return await self._repository.list_observations(
            target_id=target_id, limit=limit
        )

    async def observation(self, reference: str) -> WebObservation:
        """Return one observation by id or unique prefix.

        Raises:
            WebObservationNotFound: nothing matches.
            AmbiguousId: several observations match.
        """
        observation_id = await self._repository.resolve_observation_id(reference)
        observation = await self._repository.get_observation(observation_id)
        if observation is None:  # pragma: no cover - resolve already proved it exists
            raise WebObservationNotFound(observation_id)
        return observation

    async def state(self, target_id: str) -> WebWatchState | None:
        """Return the stored state of one target, or `None` if it was never fetched."""
        return await self._repository.get_state(target_id)

    # --------------------------------------------------------------- lifecycle

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Sync on startup, then once per poll interval until asked to stop."""
        while not stop_event.is_set():
            try:
                results = await self.sync_once()
            except asyncio.CancelledError:
                raise
            except WebWatchRequestFailed as exc:  # pragma: no cover - per-target errors are caught
                LOGGER.warning("watcher round failed: %s", exc)
            else:
                _log_round(results)
            await self._wait(stop_event)

    async def _wait(self, stop_event: asyncio.Event) -> None:
        """Wait one poll interval, returning immediately when shutdown is requested."""
        if self._waiter is not None:
            await self._waiter.wait(self._config.poll_interval_seconds, stop_event)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop_event.wait(), timeout=float(self._config.poll_interval_seconds)
            )


def _log_round(results: tuple[WebTargetResult, ...]) -> None:
    """One line per round: counts, never page content."""
    changed = sum(1 for item in results if item.outcome is TargetOutcome.CHANGED)
    failed = sum(1 for item in results if item.outcome is TargetOutcome.FAILED)
    LOGGER.debug(
        "watcher round: %d target(s), %d change(s), %d failure(s)",
        len(results),
        changed,
        failed,
    )


__all__ = [
    "BRIDGE_REPAIR_LIMIT",
    "WEB_EVENT_TYPE",
    "TargetOutcome",
    "WebTargetResult",
    "WebWatchService",
]
