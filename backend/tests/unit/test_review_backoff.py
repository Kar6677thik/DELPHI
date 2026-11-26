"""The ACCOUNT_REVIEW sweep's backoff for a candidate whose review fails to open (#290)."""

from __future__ import annotations

from datetime import timedelta

from delphi.navigator.reviews import Backoff, SweepLimits
from delphi.platform.settings import Settings

BACKOFF = Backoff(base=timedelta(seconds=60), cap=timedelta(hours=1))


def test_the_wait_doubles_with_each_failure_up_to_the_cap() -> None:
    waits = [BACKOFF.after(n).total_seconds() for n in range(1, 9)]
    assert waits == [60, 120, 240, 480, 960, 1920, 3600, 3600]


def test_a_long_run_of_failures_stays_at_the_cap() -> None:
    assert BACKOFF.after(10_000) == timedelta(hours=1)


def test_the_backoff_comes_from_the_settings() -> None:
    settings = Settings(
        database_url="postgresql://unused/db", review_retry_base_s=30, review_retry_max_s=600
    )
    assert SweepLimits.of(settings).backoff == Backoff(
        base=timedelta(seconds=30), cap=timedelta(seconds=600)
    )
