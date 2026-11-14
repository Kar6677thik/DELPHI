"""`delphi.sim.api.history_window` (docs/14 §2, docs/13 §4): what the trainers cut by."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from delphi.sim.api import DatasetInfo, history_window
from delphi.sim.generator_config import load_generator_config

CREATED = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)


def test_demo_ends_its_history_when_it_was_generated() -> None:
    window = history_window(DatasetInfo(uuid4(), "demo", 7, CREATED))
    demo = load_generator_config().profiles["demo"]
    assert window.end == CREATED
    assert window.start == CREATED - timedelta(days=demo.history_days)
    assert window.maturity_days == demo.maturity_days == 60


def test_test_and_eval_end_at_the_fixed_epoch() -> None:
    config = load_generator_config()
    for profile in ("test", "eval"):
        window = history_window(DatasetInfo(uuid4(), profile, 1, CREATED))
        assert window.end == config.fixed_history_end.astimezone(UTC)
        days = config.profiles[profile].history_days
        assert window.end - window.start == timedelta(days=days)
