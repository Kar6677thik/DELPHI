"""Scheduler slot arithmetic and the worker's health endpoint (docs/04 §5.4, docs/20 §3)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time

import pytest

from delphi.platform.jobs import FAST_LANE, JobType, Lane, lane_of
from delphi.platform.scheduler import SCHEDULE, Recurring
from delphi.platform.worker import Heartbeats, _health_server


def test_interval_slots_align_to_the_epoch() -> None:
    r = Recurring(JobType.SIM_TICK, every=timedelta(seconds=10))
    assert r.slot_start(datetime(2026, 11, 7, 6, 0, 17, 500, tzinfo=UTC)) == datetime(
        2026, 11, 7, 6, 0, 10, tzinfo=UTC
    )
    assert (
        r.key(datetime(2026, 11, 7, 6, 0, 10, tzinfo=UTC)) == "SIM_TICK:2026-11-07T06:00:10+00:00"
    )


@pytest.mark.parametrize(
    ("now_utc", "slot_utc"),
    [
        # 01:00 IST is 19:30 UTC the day before.
        (datetime(2026, 11, 7, 19, 29, tzinfo=UTC), datetime(2026, 11, 6, 19, 30, tzinfo=UTC)),
        (datetime(2026, 11, 7, 19, 30, tzinfo=UTC), datetime(2026, 11, 7, 19, 30, tzinfo=UTC)),
        (datetime(2026, 11, 7, 23, 0, tzinfo=UTC), datetime(2026, 11, 7, 19, 30, tzinfo=UTC)),
    ],
)
def test_daily_slots_are_ist_wall_clock_times(now_utc: datetime, slot_utc: datetime) -> None:
    r = Recurring(JobType.RETENTION_SWEEP, daily_at=clock_time(1, 0))
    assert r.slot_start(now_utc) == slot_utc


def test_a_recurring_job_needs_exactly_one_rule() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        Recurring(JobType.SIM_TICK)
    with pytest.raises(ValueError, match="exactly one"):
        Recurring(JobType.SIM_TICK, every=timedelta(seconds=1), daily_at=clock_time(1, 0))
    with pytest.raises(ValueError, match="positive"):
        Recurring(JobType.SIM_TICK, every=timedelta(0))


def test_schedule_matches_the_spec_table() -> None:
    by_type = {r.job_type: r for r in SCHEDULE}
    assert by_type[JobType.SIM_TICK].every == timedelta(seconds=10)
    assert by_type[JobType.SESSION_TIMEOUT_SWEEP].every == timedelta(seconds=30)
    assert by_type[JobType.LABEL_DELIVERY].every == timedelta(seconds=60)
    assert by_type[JobType.SCOUT_AUTO_TRIGGER].every == timedelta(minutes=5)
    assert by_type[JobType.PROBE_YIELD_DAILY].daily_at == clock_time(0, 15)
    assert by_type[JobType.RETENTION_SWEEP].daily_at == clock_time(1, 0)
    assert by_type[JobType.AUDIT_VERIFY].daily_at == clock_time(2, 0)


def test_only_sim_tick_and_chase_retrace_use_the_fast_lane() -> None:
    assert {JobType.SIM_TICK, JobType.CHASE_RETRACE} == FAST_LANE
    assert lane_of(JobType.GENERATE_DATASET) is Lane.DEFAULT


def _get(url: str) -> tuple[int, dict[str, object]]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def test_worker_health_reflects_loop_heartbeats() -> None:
    # Generous margins: a loaded CI machine must not turn a fresh beat stale mid-request.
    beats = Heartbeats(max_age_s=1.0)
    server = _health_server(0, beats, "abc123")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/health"
        assert _get(url)[0] == 503  # no loop has started yet
        beats.beat("default-0")
        status, body = _get(url)
        assert status == 200 and body["version"] == "abc123"
        time.sleep(1.5)
        status, body = _get(url)
        assert status == 503 and body["stale"] == ["default-0"]
    finally:
        server.shutdown()
