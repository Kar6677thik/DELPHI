"""The worker process entrypoint (docs/04 §1).

This is the process shell: start-up, a heartbeat, and a graceful stop on SIGTERM/SIGINT. The
job runner (claiming with SKIP LOCKED, lanes, the recurring-job scheduler) and the worker's
:9000 health endpoint are added by #7.
"""

from __future__ import annotations

import signal
import threading
from types import FrameType

from delphi.platform.logging import get_logger
from delphi.platform.wiring import Platform

HEARTBEAT_S = 10.0
log = get_logger(__name__)


def install_stop_handlers(stop: threading.Event) -> None:
    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("worker.stop_requested", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def run_worker(platform: Platform, stop: threading.Event, heartbeat_s: float = HEARTBEAT_S) -> None:
    log.info("worker.started", version=platform.settings.version)
    try:
        while not stop.wait(heartbeat_s):
            log.debug("worker.heartbeat")
    finally:
        platform.close()
        log.info("worker.stopped")
