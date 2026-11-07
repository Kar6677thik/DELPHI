"""The worker process (docs/04 §1, §5.4).

Threads, all sharing one stop event:
- **slots**: each repeatedly claims a job of its lane and runs its handler. The default lane
  has ``worker_default_concurrency`` slots (2), the fast lane ``worker_fast_concurrency`` (1);
- **scheduler**: every ``worker_poll_interval_s`` it enqueues due recurring jobs and fails the
  RUNNING jobs whose worker died on their last attempt;
- **health**: ``GET /health`` on ``metrics_port`` (docs/20 §3), 200 while every loop has beaten
  in the last ``worker_heartbeat_max_age_s`` seconds, else 503.

On SIGTERM a slot finishes the job it is running and then stops (the pod's grace period is
60 s, docs/23 §4). A job cut off by SIGKILL is claimed again after its lease expires.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

from delphi.platform.audit import AuditVerifyJob
from delphi.platform.jobs import Job, JobContext, JobQueue, JobRegistry, JobType, Lane
from delphi.platform.logging import bind_context, clear_context, get_logger
from delphi.platform.retention import RetentionSweep
from delphi.platform.scheduler import Scheduler
from delphi.platform.wiring import Platform

log = get_logger(__name__)

# A lease is extended this many times per lease period while a handler runs.
LEASE_RENEWALS_PER_PERIOD = 3


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
    try:
        result = handler(ctx)
    except Exception as exc:
        log.error("job.handler_failed", exc_info=exc)
        queue.fail(job, worker_id, f"{type(exc).__name__}: {exc}")
    else:
        queue.succeed(job, worker_id, result)
    finally:
        done.set()
        renewer.join()
        clear_context()


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


def _health_server(port: int, heartbeats: Heartbeats, version: str) -> ThreadingHTTPServer:
    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_error(404)
                return
            stale = heartbeats.stale()
            ok = heartbeats.healthy()
            body = json.dumps(
                {"status": "ok" if ok else "unhealthy", "version": version, "stale": stale}
            ).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:  # probes every few seconds: no log noise
            return

    return ThreadingHTTPServer(("0.0.0.0", port), Health)  # nosec B104: inside the pod only


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
    server = _health_server(s.metrics_port, heartbeats, s.version) if serve_health else None
    if server is not None:
        threading.Thread(target=server.serve_forever, name="health", daemon=True).start()

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
