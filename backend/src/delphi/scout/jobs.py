"""The Scout's jobs (docs/04 §5.4): ``RUN_SCOUT`` and ``SCOUT_AUTO_TRIGGER``."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import Clock
from delphi.platform.jobs import JobRegistry, JobType
from delphi.scout.run import RunScoutJob, ScoutAutoTriggerJob


def register_scout_jobs(
    registry: JobRegistry, *, sessions: sessionmaker[Session], clock: Clock
) -> None:
    registry.register(JobType.RUN_SCOUT, RunScoutJob(sessions, clock))
    registry.register(JobType.SCOUT_AUTO_TRIGGER, ScoutAutoTriggerJob(sessions, clock))
