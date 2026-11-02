"""The ``Clock`` port (docs/04 §4.2). Everything except the engine reads time through it, so
the evaluation harness can substitute a virtual clock. All times are timezone-aware UTC
(ADR-0014)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A manually advanced clock for tests and virtual-time evaluation."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            raise ValueError("FixedClock needs a UTC datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("time does not run backwards")
        self._now += delta
