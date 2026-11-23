"""Probe-yield drift, the "coaching adaptation" detector (docs/12 §5, FR-LRN-05, #121).

For each source, the daily mean ``info_yield_bits`` over its completed observations. The alert
``PROBE_YIELD_DEGRADED`` is raised for a source on a day when its rolling 7-day mean falls below
0.7 × its rolling mean over the 28 days before those 7, the 7 days hold at least 30
observations, and the 28 days at least 30 too (one baseline observation is no baseline). Pure:
the caller loads the observations (``probe_runs``, or the harness's probe records) and stores or
shows the alerts. The numbers are settings (``DELPHI_YIELD_*``). A day is a UTC calendar day
(ADR-0014): the caller buckets each observation by its UTC completion date.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from delphi.platform.settings import Settings

ALERT = "PROBE_YIELD_DEGRADED"


@dataclass(frozen=True, slots=True)
class DriftConfig:
    recent_days: int
    baseline_days: int
    ratio: float
    min_recent_count: int
    min_baseline_count: int

    @staticmethod
    def of(settings: Settings) -> DriftConfig:
        return DriftConfig(
            recent_days=settings.yield_recent_days,
            baseline_days=settings.yield_baseline_days,
            ratio=settings.yield_drift_ratio,
            min_recent_count=settings.yield_min_recent_count,
            min_baseline_count=settings.yield_min_baseline_count,
        )


@dataclass(frozen=True, slots=True)
class Observation:
    source_key: str
    day: date  # the UTC date the observation completed (ADR-0014)
    info_yield_bits: float


@dataclass(frozen=True, slots=True)
class DailyYield:
    """A row of ``probe_yield_daily`` (docs/17): one source, one day."""

    source_key: str
    day: date
    n: int
    mean_info_yield_bits: float


@dataclass(frozen=True, slots=True)
class DriftAlert:
    source_key: str
    day: date
    recent_mean: float
    baseline_mean: float
    recent_count: int
    alert: str = ALERT


def daily_yields(observations: Iterable[Observation]) -> list[DailyYield]:
    sums: dict[tuple[str, date], list[float]] = defaultdict(list)
    for o in observations:
        sums[o.source_key, o.day].append(o.info_yield_bits)
    return [
        DailyYield(source, day, len(v), sum(v) / len(v))
        for (source, day), v in sorted(sums.items())
    ]


def _window(rows: Mapping[date, DailyYield], end: date, days: int) -> tuple[int, float]:
    """The count and the observation-weighted mean over ``days`` days ending on ``end``."""
    n = 0
    total = 0.0
    for k in range(days):
        row = rows.get(end - timedelta(days=k))
        if row is not None:
            n += row.n
            total += row.n * row.mean_info_yield_bits
    return n, (total / n if n else 0.0)


def drift_alerts(
    daily: Iterable[DailyYield], config: DriftConfig, *, on: date | None = None
) -> list[DriftAlert]:
    """The alerts for every source and day (or only ``on``), oldest first."""
    by_source: dict[str, dict[date, DailyYield]] = defaultdict(dict)
    for row in daily:
        by_source[row.source_key][row.day] = row
    alerts: list[DriftAlert] = []
    for source, rows in sorted(by_source.items()):
        days = [on] if on is not None else sorted(rows)
        for day in days:
            recent_n, recent = _window(rows, day, config.recent_days)
            base_end = day - timedelta(days=config.recent_days)
            base_n, base = _window(rows, base_end, config.baseline_days)
            if (
                recent_n >= config.min_recent_count
                and base_n >= config.min_baseline_count
                and recent < config.ratio * base
            ):
                alerts.append(DriftAlert(source, day, recent, base, recent_n))
    return alerts
