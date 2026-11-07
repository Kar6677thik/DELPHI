from __future__ import annotations

import os
from pathlib import Path

import pytest

from delphi.engine import CompiledProfile, compile_profile
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.probes.registry import CLASSIFIER_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
KB_DIR = REPO_ROOT / "kb"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUN_SLOW_ENV = "DELPHI_RUN_SLOW"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """`slow` tests (minutes) run only with DELPHI_RUN_SLOW=1."""
    if os.environ.get(RUN_SLOW_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"slow: set {RUN_SLOW_ENV}=1 to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def kb() -> KnowledgeBase:
    return load_and_validate(KB_DIR, CLASSIFIER_NAMES)


@pytest.fixture(scope="session")
def guardian(kb: KnowledgeBase) -> CompiledProfile:
    return compile_profile(kb, "GUARDIAN_PAYMENT")


@pytest.fixture(scope="session")
def navigator_hold(kb: KnowledgeBase) -> CompiledProfile:
    return compile_profile(kb, "CASE_PAYMENT_HOLD")
