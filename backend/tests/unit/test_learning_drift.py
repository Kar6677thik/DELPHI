"""The probe-yield drift detector (docs/12 §5, FR-LRN-05, #121), with injected drift."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from delphi.learning.drift import (
    ALERT,
    DailyYield,
    DriftConfig,
    Observation,
    daily_yields,
    drift_alerts,
)
from delphi.platform.settings import Settings

DAY0 = date(2026, 6, 1)
CONFIG = DriftConfig.of(Settings(database_url="postgresql://unused/db"))


def _days(source: str, means: list[float], per_day: int = 10) -> list[DailyYield]:
    return [DailyYield(source, DAY0 + timedelta(days=i), per_day, m) for i, m in enumerate(means)]


def test_a_steady_source_raises_nothing() -> None:
    assert drift_alerts(_days("SPELL_BREAKER_DIGITAL_ARREST", [1.0] * 40), CONFIG) == []


def test_injected_drift_raises_the_alert_once_the_recent_week_has_dropped() -> None:
    # 35 days at 1.0 bits, then the yield halves (coaching): 0.5 < 0.7 × 1.0.
    rows = _days("SPELL_BREAKER_DIGITAL_ARREST", [1.0] * 35 + [0.5] * 10)
    alerts = drift_alerts(rows, CONFIG)
    first = alerts[0]
    assert first.alert == ALERT and first.source_key == "SPELL_BREAKER_DIGITAL_ARREST"
    # The 7-day mean crosses 0.7 × 1.0 on the 5th low day: (2·1.0 + 5·0.5)/7 = 0.64.
    assert first.day == DAY0 + timedelta(days=39)
    assert first.recent_mean < 0.7 * first.baseline_mean and first.recent_count >= 30


def test_too_few_recent_observations_raise_nothing() -> None:
    rows = _days("CALL_STATUS", [1.0] * 35 + [0.1] * 10, per_day=4)  # 28 in 7 days < 30
    assert drift_alerts(rows, CONFIG) == []


def test_no_baseline_raises_nothing_and_on_selects_one_day() -> None:
    rows = _days("CALL_STATUS", [0.1] * 10)
    assert drift_alerts(rows, CONFIG) == []
    drifted = _days("X", [1.0] * 35 + [0.2] * 10)
    assert [a.day for a in drift_alerts(drifted, CONFIG, on=DAY0 + timedelta(days=44))] == [
        DAY0 + timedelta(days=44)
    ]


def test_daily_yields_aggregate_per_source_and_day() -> None:
    obs = [
        Observation("A", DAY0, 1.0),
        Observation("A", DAY0, 0.5),
        Observation("B", DAY0, 2.0),
    ]
    assert daily_yields(obs) == [DailyYield("A", DAY0, 2, 0.75), DailyYield("B", DAY0, 1, 2.0)]


def test_a_thin_baseline_raises_nothing() -> None:
    """One baseline day of 2 observations is no baseline: under the minimum of 30, no alert."""
    rows = [*_days("PURPOSE_QUESTION", [1.0], per_day=2)]
    rows += [
        DailyYield("PURPOSE_QUESTION", DAY0 + timedelta(days=CONFIG.recent_days + k), 10, 0.1)
        for k in range(1, CONFIG.recent_days + 1)
    ]
    assert drift_alerts(rows, CONFIG) == []
    loose = replace(CONFIG, min_baseline_count=1)
    assert drift_alerts(rows, loose)  # the same rows alert with no minimum
