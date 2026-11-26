"""The Scout's jobs (docs/04 §5.4): ``RUN_SCOUT``, ``SCOUT_AUTO_TRIGGER`` and ``RUN_BACKTEST``."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import Clock
from delphi.platform.jobs import JobRegistry, JobType
from delphi.scout.backtest import RunBacktestJob
from delphi.scout.run import RunScoutJob, ScoutAutoTriggerJob


def register_scout_jobs(
    registry: JobRegistry,
    *,
    sessions: sessionmaker[Session],
    clock: Clock,
    max_items: int,
    failure_backoff: timedelta,
    backtest_max_items: int,
) -> None:
    registry.register(JobType.RUN_SCOUT, RunScoutJob(sessions, clock, max_items))
    registry.register(JobType.RUN_BACKTEST, RunBacktestJob(sessions, clock, backtest_max_items))
    registry.register(
        JobType.SCOUT_AUTO_TRIGGER, ScoutAutoTriggerJob(sessions, clock, failure_backoff)
    )
