"""The simulator's job wiring (#19): parameters and which handlers the worker gets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from delphi.cli import worker_registry
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import SystemClock
from delphi.platform.db import make_session_factory
from delphi.platform.jobs import JobType
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sim.generator_config import load_generator_config
from delphi.sim.jobs import DatasetParams

URL = "postgresql://delphi_app:x@localhost:5432/delphi"  # never connected to here
KB_DIR = Path(__file__).resolve().parents[3] / "kb"


def _repo_kb() -> KnowledgeBase:
    # The worker reads the ACTIVE KB from the database; with no database here, use KB v1.
    return load_and_validate(KB_DIR, CLASSIFIER_NAMES)


def _platform(**settings: Any) -> Platform:
    engine = create_engine("postgresql+psycopg://")
    return Platform(
        settings=Settings(database_url=URL, **settings),
        engine=engine,
        sessions=make_session_factory(engine),
        clock=SystemClock(),
        started_at=SystemClock().now(),
        migration_head=None,
    )


def test_the_worker_registers_generate_dataset_only_with_its_sim_pool() -> None:
    registry, sim = worker_registry(_platform())
    assert sim is None and JobType.GENERATE_DATASET not in registry.handlers
    registry, sim = worker_registry(
        _platform(sim_database_url=URL.replace("app", "sim")), kb_source=lambda _: _repo_kb()
    )
    try:
        assert JobType.GENERATE_DATASET in registry.handlers
        assert JobType.RETENTION_SWEEP in registry.handlers  # the platform's are kept
    finally:
        assert sim is not None
        sim.dispose()


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"profile": "demo"}, DatasetParams("demo")),
        ({"profile": "test", "seed": 7, "personas": True}, DatasetParams("test", 7, True)),
    ],
)
def test_dataset_params(params: dict[str, Any], expected: DatasetParams) -> None:
    assert DatasetParams.of(params, load_generator_config()) == expected


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"profile": "huge"}, "unknown profile"),
        ({}, "unknown profile"),
        ({"profile": "test", "seed": -1}, "seed must be"),
        ({"profile": "test", "seed": True}, "seed must be"),
        ({"profile": "test", "personas": "yes"}, "personas must be"),
    ],
)
def test_bad_dataset_params(params: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DatasetParams.of(params, load_generator_config())


def test_a_seed_replaces_only_its_profiles() -> None:
    config = load_generator_config()
    seeded = DatasetParams("test", seed=42).config(config)
    assert seeded.profiles["test"].seed == 42
    assert seeded.profiles["demo"] == config.profiles["demo"]
    assert DatasetParams("test").config(config) is config
