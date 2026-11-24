"""The chase module's jobs (docs/04 §5.4): ``CHASE_RETRACE``."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from delphi.chase.tickets import ChaseRetraceJob, simulated_ack_delay
from delphi.platform.clock import Clock
from delphi.platform.jobs import JobRegistry, JobType


def register_chase_jobs(
    registry: JobRegistry,
    *,
    sessions: sessionmaker[Session],
    clock: Clock,
    tickets_per_run: int,
) -> None:
    # The latencies run in real time until the DEMO time mode is wired, as the Guardian's do.
    registry.register(
        JobType.CHASE_RETRACE,
        ChaseRetraceJob(sessions, clock, simulated_ack_delay, tickets_per_run=tickets_per_run),
    )
