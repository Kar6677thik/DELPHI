"""The worker process (docs/04 §1, §5.4).

Threads, all sharing one stop event:
- **slots**: each repeatedly claims a job of its lane and runs its handler. The default lane
  has ``worker_default_concurrency`` slots (2), the fast lane ``worker_fast_concurrency`` (1);
- **scheduler**: every ``worker_poll_interval_s`` it enqueues due recurring jobs and fails the
  RUNNING jobs whose worker died on their last attempt;
- **metrics**: on ``metrics_port``, ``GET /metrics`` (docs/20 §2) and ``GET /health``
  (docs/20 §3): 200 while every loop has beaten in the last ``worker_heartbeat_max_age_s``
  seconds, else 503.

On SIGTERM a slot finishes the job it is running and then stops (the pod's grace period is
60 s, docs/23 §4). A job cut off by SIGKILL is claimed again after its lease expires.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

from delphi.platform.audit import AuditVerifyJob
from delphi.platform.jobs import (
    Job,
    JobContext,
    JobQueue,
    JobRegistry,
    JobStatus,
    JobType,
    Lane,
    queued_counts,
)
from delphi.platform.logging import bind_context, clear_context, get_logger
from delphi.platform.metrics import JOB_DURATION, JOBS, JOBS_QUEUED, HealthCheck, metrics_server
from delphi.platform.retention import RetentionSweep
from delphi.platform.scheduler import Scheduler
from delphi.platform.wiring import Platform

log = get_logger(__name__)

# A lease is extended this many times per lease period while a handler runs.
LEASE_RENEWALS_PER_PERIOD = 3
# delphi_jobs_total{status} after a failed attempt, by the job's next status.
_FAIL_OUTCOME = {JobStatus.QUEUED: "RETRY", JobStatus.FAILED: "FAILED"}


def install_stop_handlers(stop: threading.Event) -> None:
    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("worker.stop_requested", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


@dataclass(slots=True)
class Heartbeats:
    """The last time each loop showed it was alive (monotonic seconds)."""

    max_age_s: float
    _beats: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def beat(self, name: str) -> None:
        with self._lock:
            self._beats[name] = time.monotonic()

    def stale(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            return sorted(n for n, t in self._beats.items() if now - t > self.max_age_s)

    def healthy(self) -> bool:
        with self._lock:
            started = bool(self._beats)
        return started and not self.stale()


def run_job(queue: JobQueue, registry: JobRegistry, job: Job, worker_id: str) -> None:
    """Run one claimed job to completion, renewing its lease in the background."""
    handler = registry.handlers[job.type]
    ctx = JobContext(job=job, queue=queue, worker_id=worker_id)
    done = threading.Event()
    period_s = queue.config.lease.total_seconds() / LEASE_RENEWALS_PER_PERIOD

    def renew() -> None:
        while not done.wait(period_s):
            if not queue.heartbeat(job, worker_id):
                return  # lease lost: the job's outcome is no longer ours to write

    renewer = threading.Thread(target=renew, name=f"lease-{job.id}", daemon=True)
    renewer.start()
    bind_context(job_id=str(job.id), job_type=job.type)
    started = time.perf_counter()
    try:
        try:
            result = handler(ctx)
        finally:
            JOB_DURATION.labels(job.type).observe(time.perf_counter() - started)
    except Exception as exc:
        log.error("job.handler_failed", exc_info=exc)
        after = queue.fail(job, worker_id, f"{type(exc).__name__}: {exc}")
        # None: the lease was taken over, so this attempt's outcome is not ours.
        outcome = _FAIL_OUTCOME.get(after, "LOST") if after is not None else "LOST"
    else:
        outcome = "SUCCEEDED" if queue.succeed(job, worker_id, result) else "LOST"
    finally:
        done.set()
        renewer.join()
        clear_context()
    JOBS.labels(job.type, outcome).inc()


def _loop(
    name: str,
    stop: threading.Event,
    heartbeats: Heartbeats,
    poll_s: float,
    step: Callable[[], bool],
) -> None:
    """Call ``step`` until stopped; wait ``poll_s`` whenever it found nothing to do."""
    while not stop.is_set():
        heartbeats.beat(name)
        try:
            busy = step()
        except Exception as exc:  # a database blip must not kill the loop
            log.error("worker.loop_error", loop=name, exc_info=exc)
            busy = False
        if not busy:
            stop.wait(poll_s)


def worker_health(heartbeats: Heartbeats, version: str) -> HealthCheck:
    def check() -> tuple[int, dict[str, Any]]:
        stale = heartbeats.stale()
        ok = heartbeats.healthy()
        body = {"status": "ok" if ok else "unhealthy", "version": version, "stale": stale}
        return (200 if ok else 503), body

    return check


def sample_queue_depth(queue: JobQueue) -> None:
    """Set ``delphi_jobs_queued`` for every job type (0 when none is waiting)."""
    counts = queued_counts(queue.sessions)
    for job_type in JobType:
        JOBS_QUEUED.labels(job_type.value).set(counts.get(job_type.value, 0))


def build_registry(platform: Platform) -> JobRegistry:
    """The job handlers this release ships. Feature modules add theirs as they land."""
    s = platform.settings
    registry = JobRegistry()
    registry.register(
        JobType.RETENTION_SWEEP,
        RetentionSweep(
            platform.sessions,
            platform.clock,
            outbox_retention=s.outbox_retention,
            idempotency_retention=s.idempotency_retention,
        ),
    )
    registry.register(JobType.AUDIT_VERIFY, AuditVerifyJob(platform.sessions, platform.clock))
    return registry


def run_worker(
    platform: Platform,
    stop: threading.Event,
    *,
    registry: JobRegistry | None = None,
    serve_health: bool = True,
) -> None:
    s = platform.settings
    registry = registry or build_registry(platform)
    queue = JobQueue(platform.sessions, platform.clock, s.queue_config)
    scheduler = Scheduler(platform.sessions, platform.clock, registry)
    heartbeats = Heartbeats(max_age_s=s.worker_heartbeat_max_age_s)
    worker = f"{socket.gethostname()}:{os.getpid()}"

    def slot_step(lane: Lane, worker_id: str) -> Callable[[], bool]:
        def step() -> bool:
            job = queue.claim(worker_id, registry.types(lane))
            if job is None:
                return False
            run_job(queue, registry, job, worker_id)
            return True

        return step

    def scheduler_step() -> bool:
        scheduler.tick()
        queue.reap_expired()
        sample_queue_depth(queue)
        return False

    loops: list[tuple[str, Callable[[], bool]]] = [("scheduler", scheduler_step)]
    for lane, count in (
        (Lane.DEFAULT, s.worker_default_concurrency),
        (Lane.FAST, s.worker_fast_concurrency),
    ):
        for i in range(count):
            name = f"{lane.value.lower()}-{i}"
            loops.append((name, slot_step(lane, f"{worker}:{name}")))

    threads = [
        threading.Thread(
            target=_loop,
            args=(name, stop, heartbeats, s.worker_poll_interval_s, step),
            name=name,
            daemon=True,
        )
        for name, step in loops
    ]
    server = (
        metrics_server(s.metrics_port, worker_health(heartbeats, s.version))
        if serve_health
        else None
    )
    if server is not None:
        threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()

    log.info(
        "worker.started",
        version=s.version,
        loops=[n for n, _ in loops],
        handlers=sorted(registry.handlers),
    )
    for thread in threads:
        thread.start()
    try:
        stop.wait()
        for thread in threads:
            thread.join()  # each finishes its current job first
    finally:
        if server is not None:
            server.shutdown()
        platform.close()
        log.info("worker.stopped")
