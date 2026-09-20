"""Debounced rolling-replan requests (ADR-0016).

A commitment mutation does not replan synchronously — it records *intent*: "the plan is
probably out of date". Repeated changes inside the debounce window coalesce into one active
job, because one job whose `due_at` keeps moving forward is exactly a trailing debounce.

Two properties matter here:

- the request never touches plan blocks; it only asks for a proposal to be generated later;
- a failure to record the request is logged and swallowed. Rolling replanning is a convenience
  derived from commitment state — a lost request delays the next proposal until the next
  mutation, while failing the user's `pw task add` because of it would be worse.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from assistant.domain.scheduled_job import ScheduledJob, ScheduledJobKind, canonical_payload_json
from assistant.domain.scheduler_payloads import ROLLING_REPLAN_DEDUP_KEY, RollingReplanPayload
from assistant.ports.clock import Clock
from assistant.ports.scheduler_repository import SchedulerRepository

LOGGER = logging.getLogger("assistant.replan")


class RollingReplanRequester:
    """Schedules (or pushes forward) the single active rolling-replan job."""

    def __init__(
        self,
        scheduler: SchedulerRepository,
        clock: Clock,
        *,
        debounce_seconds: int,
        timezone: str | None,
    ) -> None:
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds must not be negative")
        self._scheduler = scheduler
        self._clock = clock
        self._debounce = timedelta(seconds=debounce_seconds)
        self._timezone = timezone

    async def request(self) -> ScheduledJob | None:
        """Record one replan request; returns the (possibly coalesced) job.

        Returns `None` when planning is not configured: there is no timezone to plan in, so
        nothing is scheduled at all.
        """
        if self._timezone is None:
            return None
        now = self._clock.now()
        payload = RollingReplanPayload(timezone=self._timezone)
        job = ScheduledJob(
            kind=ScheduledJobKind.ROLLING_REPLAN,
            due_at=now + self._debounce,
            dedup_key=ROLLING_REPLAN_DEDUP_KEY,
            payload_json=canonical_payload_json(payload.to_payload()),
            created_at=now,
            updated_at=now,
        )
        try:
            return await self._scheduler.schedule_or_replace(job)
        except Exception as exc:
            LOGGER.warning("could not record a rolling replan request: %s", type(exc).__name__)
            return None


__all__ = ["RollingReplanRequester"]
