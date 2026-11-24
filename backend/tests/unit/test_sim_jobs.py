"""The simulator's job wiring (#19): parameters and which handlers the worker gets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from delphi.cli import worker_registry
from delphi.guardian.api import WorkerGuardian
from delphi.guardian.store import Runner
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import SystemClock
from delphi.platform.db import make_session_factory
from delphi.platform.jobs import JobType
from delphi.platform.settings import Settings, SettingsError
from delphi.platform.wiring import Platform
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sensing.api import SensingInference
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
    parts = worker_registry(_platform())
    assert parts.sim_engine is None and JobType.GENERATE_DATASET not in parts.registry.handlers
    assert JobType.TRAIN_SENSING in parts.registry.handlers  # sensing needs no sim pool (#88)
    assert parts.guardian is None and JobType.DEMO_STORY not in parts.registry.handlers
    parts.close()
    parts = worker_registry(
        _platform(sim_database_url=URL.replace("app", "sim")), kb_source=lambda _: _repo_kb()
    )
    try:
        assert JobType.GENERATE_DATASET in parts.registry.handlers
        assert JobType.RETENTION_SWEEP in parts.registry.handlers  # the platform's are kept
        # the WORKER Guardian runner, with the sensing prior, and the story runner (#95, #101)
        assert isinstance(parts.guardian, WorkerGuardian)
        assert isinstance(parts.guardian.runner._service._prior, SensingInference)
        assert parts.guardian.runner._runner is Runner.WORKER
        assert JobType.DEMO_STORY in parts.registry.handlers
    finally:
        assert parts.sim_engine is not None
        parts.close()  # never started: closing is still safe


def test_a_worker_that_should_simulate_needs_the_sim_pool() -> None:
    """The api accepts simulated calls on DELPHI_SIMULATION_ENABLED; a worker with the flag but
    no sim pool would leave them queued for ever, so it refuses to start (#196 review)."""
    with pytest.raises(SettingsError, match="DELPHI_SIM_DATABASE_URL"):
        worker_registry(_platform(simulation_enabled=True))


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
