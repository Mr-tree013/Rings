"""Acceptance: supervision, durable retry and cross-instance recovery (ADR-0031).

The daemon's promise is that one broken thing stays one broken thing: a crashing service is
restarted, its siblings keep running, a transient provider failure is retried from durable state
rather than from memory, and a stop event ends every supervisor without leaving a task behind.

Nothing here sleeps for real: the backoff waiter and the poll waiter are injected, so a "restart"
is one awaited call and the clock is a value rather than a wall.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from assistant.application.event_worker import EventWorker, WorkerResult
from assistant.application.manual_input_service import ManualInputService
from assistant.application.retry import RetryPolicy
from assistant.application.scheduler_service import SchedulerRunResult, SchedulerService
from assistant.daemon.app import serve
from assistant.daemon.supervisor import BackoffPolicy, supervise
from assistant.domain.errors import ModelTransientError
from assistant.domain.inbound_event import EventStatus
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.manual_inputs import SqliteManualInputRepository
from assistant.store.migrations import apply_migrations
from assistant.store.scheduler import SqliteSchedulerRepository
from tests.support.fakes import FakeClock
from tests.support.ops import NOW


class _Service:
    """A scripted supervised service: it fails the first `failures` times, then waits."""

    def __init__(self, name: str, *, failures: int = 0, stop_seen: list[str] | None = None):
        self.name = name
        self._failures = failures
        self.starts = 0
        self.stopped = False
        self._seen = stop_seen if stop_seen is not None else []

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        self.starts += 1
        if self.starts <= self._failures:
            raise RuntimeError(f"{self.name} crashed on start {self.starts}")
        await stop_event.wait()
        self.stopped = True
        self._seen.append(self.name)


class _IdleWaiter:
    """A backoff waiter that never sleeps: the test's clock is the only clock."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def wait(self, seconds: float, stop_event: asyncio.Event) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


# ----------------------------------------------------------------- supervision


async def test_a_crashing_service_is_restarted_while_its_sibling_runs() -> None:
    """§50: the crashing service recovers; the sibling is never cancelled."""
    stop_event = asyncio.Event()
    crashing = _Service("index-sync", failures=1)
    healthy = _Service("scheduler")
    waiter = _IdleWaiter()
    policy = BackoffPolicy(base_delay_seconds=0.01, max_delay_seconds=0.02)

    supervisors = [
        asyncio.create_task(
            supervise(crashing, stop_event, policy=policy, waiter=waiter)
        ),
        asyncio.create_task(supervise(healthy, stop_event, policy=policy, waiter=waiter)),
    ]
    for _ in range(100):
        if crashing.starts >= 2 and healthy.starts >= 1:
            break
        await asyncio.sleep(0.005)

    assert crashing.starts == 2  # restarted after the crash
    assert healthy.starts == 1 and not healthy.stopped  # never disturbed
    assert waiter.delays == [0.01]  # one backoff, from the policy

    stop_event.set()
    await asyncio.wait_for(asyncio.gather(*supervisors), timeout=5)

    assert crashing.stopped and healthy.stopped


async def test_a_failing_service_does_not_cancel_its_siblings() -> None:
    """§50: a mail/web/mobile failure stays inside its own supervisor."""
    stop_event = asyncio.Event()
    failing = _Service("mail-sync", failures=5)
    other_failing = _Service("web-watch", failures=5)
    healthy = _Service("event-worker", stop_seen=[])
    waiter = _IdleWaiter()

    async def run() -> None:
        async with asyncio.TaskGroup() as group:
            for service in (failing, other_failing, healthy):
                group.create_task(serve_wrapper(service, stop_event, waiter))

    supervisor = asyncio.create_task(run())
    for _ in range(200):
        if failing.starts >= 3 and healthy.starts == 1:
            break
        await asyncio.sleep(0.005)

    assert failing.starts >= 2 and other_failing.starts >= 2
    assert healthy.starts == 1 and not healthy.stopped
    assert not supervisor.done()

    stop_event.set()
    await asyncio.wait_for(supervisor, timeout=5)
    assert healthy.stopped


async def serve_wrapper(service: _Service, stop_event: asyncio.Event, waiter: _IdleWaiter):
    """Supervise one scripted service with a fast, deterministic backoff."""
    return await supervise(
        service,
        stop_event,
        policy=BackoffPolicy(base_delay_seconds=0.01, max_delay_seconds=0.02),
        waiter=waiter,
    )


async def test_the_stop_event_shuts_every_supervised_service_down() -> None:
    """§50: `serve` returns once every service has seen the stop event, and leaves no task."""
    stop_event = asyncio.Event()
    first = _Service("index-sync")
    second = _Service("scheduler")
    serving = asyncio.create_task(serve(stop_event, [first, second]))
    while not (first.starts and second.starts):
        await asyncio.sleep(0.005)

    stop_event.set()
    await asyncio.wait_for(serving, timeout=5)

    assert first.stopped and second.stopped
    leftovers = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("supervisor:")
    ]
    assert leftovers == []


# --------------------------------------------------------------- durable retry


async def test_a_transient_provider_failure_is_retried_from_durable_state(
    tmp_path: Path,
) -> None:
    """§50: the retry comes from the event's own row, not from a worker's memory."""
    runtime = tmp_path / "data" / "growing-assistant"
    runtime.mkdir(parents=True)
    clock = FakeClock(start=NOW)
    database = Database.at(runtime / "assistant.db")
    apply_migrations(database, clock=clock)
    manual = ManualInputService(
        SqliteManualInputRepository(database), SqliteEventRepository(database, clock), clock
    )
    stored = await manual.create_input("Forwarded: the report is due Friday.")
    from assistant.application.mail_event_handler import InboundEventDispatcher
    from assistant.application.observation_event_handler import MANUAL_EVENT_TYPE
    from assistant.domain.errors import PermanentEventError
    from assistant.domain.inbound_event import InboundEvent
    from assistant.ports.event_handler import EventHandler

    class _Flaky(EventHandler):
        def __init__(self) -> None:
            self.calls = 0

        async def handle(self, event: InboundEvent) -> None:
            self.calls += 1
            if self.calls == 1:
                raise ModelTransientError("the provider is busy")
            del event

    flaky = _Flaky()
    worker = EventWorker(
        SqliteEventRepository(database, clock),
        InboundEventDispatcher({MANUAL_EVENT_TYPE: flaky}),
        clock,
        RetryPolicy(),
        worker_id="acceptance",
    )
    assert await worker.run_once() is WorkerResult.RETRY_SCHEDULED
    failed = await SqliteEventRepository(database, clock).get(stored.event_id)
    assert failed is not None and failed.status is EventStatus.FAILED
    assert failed.next_attempt_at is not None

    # The same durable state is what a restarted worker sees: after the backoff it succeeds.
    clock.advance(600)
    restarted = EventWorker(
        SqliteEventRepository(database, clock),
        InboundEventDispatcher({MANUAL_EVENT_TYPE: flaky}),
        clock,
        RetryPolicy(),
        worker_id="restarted-worker",
    )
    assert await restarted.run_once() is WorkerResult.PROCESSED
    assert flaky.calls == 2
    final = await SqliteEventRepository(database, clock).get(stored.event_id)
    assert final is not None and final.status is EventStatus.PROCESSED
    assert PermanentEventError


# ----------------------------------------------------------- scheduler recovery


async def test_a_scheduler_lease_is_recovered_by_a_new_instance(tmp_path: Path) -> None:
    """§51: a crashed scheduler loses its lease, and the work is done exactly once."""
    from assistant.application.greedy_planner import GreedyPlanner
    from assistant.application.planner_service import PlannerService
    from assistant.application.task_service import CreateTask, TaskService
    from assistant.domain.config import PlanningConfig
    from assistant.store.planning import SqlitePlanningRepository
    from assistant.store.work import SqliteWorkRepository

    runtime = tmp_path / "data" / "growing-assistant"
    runtime.mkdir(parents=True)
    clock = FakeClock(start=NOW)
    database = Database.at(runtime / "assistant.db")
    apply_migrations(database, clock=clock)
    commitments = SqliteCommitmentRepository(database)
    from assistant.domain.config import ReminderConfig

    await TaskService(
        commitments,
        clock,
        reminder_offsets_minutes=ReminderConfig().deadline_offsets_minutes,
    ).create_task(
        CreateTask(title="Write the SE lab report", due_at=NOW + timedelta(minutes=20))
    )
    jobs = SqliteSchedulerRepository(database)
    reminders = [item for item in await jobs.list_jobs() if item.kind.value == "deadline_reminder"]
    assert reminders
    crashed = await jobs.claim_next(
        worker_id="crashed-scheduler",
        claim_token=__import__("uuid").uuid4(),
        now=clock.now(),
        lease_expires_at=clock.now() + timedelta(minutes=5),
    )
    assert crashed is not None

    # A new process, a new scheduler object, the same durable lease.
    clock.advance(6 * 60)
    reopened = Database.at(runtime / "assistant.db")
    scheduler = SchedulerService(
        SqliteSchedulerRepository(reopened),
        SqliteCommitmentRepository(reopened),
        SqliteWorkRepository(reopened),
        PlannerService(
            SqlitePlanningRepository(reopened),
            GreedyPlanner(),
            PlanningConfig(timezone="Asia/Shanghai"),
            clock,
        ),
        clock,
        RetryPolicy(),
        _IdleWaiter(),  # type: ignore[arg-type]
        worker_id="restarted-scheduler",
        lease_duration=timedelta(minutes=5),
    )

    assert await scheduler.run_once() is SchedulerRunResult.COMPLETED
    notifications = await SqliteSchedulerRepository(reopened).list_notifications(limit=None)
    assert len(notifications) == 1  # the dedup index kept the retry from duplicating it
