"""The api's Guardian runner slot (#95): built from the real parts at start-up, and the app's
lifespan resumes the sessions a restart interrupted (FR-GDN-14) before it serves."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from delphi.api.app import create_app
from delphi.cli import live_guardian_for
from delphi.guardian.api import LiveGuardian
from delphi.guardian.story_prior import StoryPrior
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.platform.clock import SystemClock
from delphi.platform.db import make_session_factory
from delphi.platform.settings import Settings
from delphi.platform.wiring import Platform
from delphi.probes.registry import CLASSIFIER_NAMES
from delphi.sensing.api import SensingInference

URL = "postgresql://delphi_app:x@localhost:5432/delphi"  # never connected to here
KB_DIR = Path(__file__).resolve().parents[3] / "kb"


def _repo_kb(_platform: Platform) -> KnowledgeBase:
    return load_and_validate(KB_DIR, CLASSIFIER_NAMES)


def _platform() -> Platform:
    engine = create_engine("postgresql+psycopg://")
    return Platform(
        settings=Settings(database_url=URL),
        engine=engine,
        sessions=make_session_factory(engine),
        clock=SystemClock(),
        started_at=SystemClock().now(),
        migration_head=None,
    )


def test_the_slot_uses_the_sensing_inference_as_its_prior() -> None:
    live, free_pool = live_guardian_for(_platform(), kb_source=_repo_kb)
    try:
        assert isinstance(live, LiveGuardian)
        # A demo story's payment gets its scripted prior; every other, the sensing inference.
        assert isinstance(live.service._prior, StoryPrior)
        assert isinstance(live.service._prior.inner, SensingInference)
        assert set(live.service._fetchers) == {
            "BENEFICIARY_INTEL",
            "COUNTERPARTY_PHONE_RISK",
            "DEVICE_POSTURE",
            "SESSION_BEHAVIOUR",
            "CALL_STATUS",
        }
    finally:
        free_pool()


class _Runner:
    def __init__(self) -> None:
        self.resumed = 0
        self.stopped = 0

    async def resume_all(self) -> list[UUID]:
        self.resumed += 1
        return [uuid4()]

    async def shutdown(self) -> None:
        self.stopped += 1


class _Live:
    def __init__(self) -> None:
        self.runner = _Runner()


def test_the_app_resumes_interrupted_sessions_at_start_and_stops_them_at_exit() -> None:
    live: Any = _Live()
    app = create_app(_platform(), guardian_runner=live)
    with TestClient(app):
        assert live.runner.resumed == 1  # before the first request is served
    assert live.runner.stopped == 1
