"""Recurring jobs (docs/04 §5.4, "Recurring jobs").

The scheduler enqueues each recurring job once per **slot**, with idempotency key
`<TYPE>:<slot start, ISO 8601 UTC>`. A slot therefore runs once even if the worker restarts or
two workers run the scheduler. After an outage only the current slot is enqueued; missed slots
are not replayed (every recurring job works on "whatever is due now", so catching up once is
enough).

Only types with a registered handler are scheduled. CHASE_RETRACE and MULE_SWEEP have
time-scaled intervals, so they join the schedule with Chase and the time scale (M5–M6).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import Clock
from delphi.platform.jobs import JobRegistry, JobType, enqueue
from delphi.platform.logging import get_logger

IST = timezone(timedelta(hours=5, minutes=30), "IST")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Recurring:
    """Exactly one of ``every`` (a fixed interval, aligned to the Unix epoch) or ``daily_at``
    (a wall-clock time in IST)."""

    job_type: JobType
    every: timedelta | None = None
    daily_at: time | None = None

    def __post_init__(self) -> None:
        if (self.every is None) == (self.daily_at is None):
            raise ValueError(f"{self.job_type}: give exactly one of every / daily_at")
        if self.every is not None and self.every <= timedelta(0):
            raise ValueError(f"{self.job_type}: the interval must be positive")

    def slot_start(self, now: datetime) -> datetime:
        """The start of the slot that contains ``now`` (UTC)."""
        if self.every is not None:
            slots = (now - _EPOCH) // self.every
            return _EPOCH + slots * self.every
        assert self.daily_at is not None
        local = now.astimezone(IST)
        today = datetime.combine(local.date(), self.daily_at, tzinfo=IST)
        start = today if today <= local else today - timedelta(days=1)
        return start.astimezone(UTC)

    def key(self, slot: datetime) -> str:
        return f"{self.job_type.value}:{slot.astimezone(UTC).isoformat()}"


# docs/04 §5.4. SIM_TICK runs only in DEMO time mode; its handler is registered only then.
SCHEDULE: tuple[Recurring, ...] = (
    Recurring(JobType.SIM_TICK, every=timedelta(seconds=10)),
    Recurring(JobType.SESSION_TIMEOUT_SWEEP, every=timedelta(seconds=30)),
    Recurring(JobType.LABEL_DELIVERY, every=timedelta(seconds=60)),
    Recurring(JobType.SCOUT_AUTO_TRIGGER, every=timedelta(minutes=5)),
    Recurring(JobType.PROBE_YIELD_DAILY, daily_at=time(0, 15)),
    Recurring(JobType.RETENTION_SWEEP, daily_at=time(1, 0)),
    Recurring(JobType.AUDIT_VERIFY, daily_at=time(2, 0)),
)


@dataclass(slots=True)
class Scheduler:
    sessions: sessionmaker[Session]
    clock: Clock
    registry: JobRegistry
    schedule: Iterable[Recurring] = SCHEDULE
    _last: dict[JobType, datetime] = field(default_factory=dict)

    def tick(self) -> list[str]:
        """Enqueue every registered recurring job whose slot has changed since the last tick.
        Returns the keys enqueued (new or already present)."""
        now = self.clock.now()
        keys: list[str] = []
        due = [
            (r, r.slot_start(now))
            for r in self.schedule
            if r.job_type in self.registry.handlers
            and self._last.get(r.job_type) != r.slot_start(now)
        ]
        if not due:
            return keys
        with self.sessions.begin() as session:
            for recurring, slot in due:
                key = recurring.key(slot)
                result = enqueue(session, self.clock, recurring.job_type, idempotency_key=key)
                if result.created:
                    log.info("scheduler.enqueued", job_type=recurring.job_type, key=key)
                keys.append(key)
        for recurring, slot in due:
            self._last[recurring.job_type] = slot
        return keys
