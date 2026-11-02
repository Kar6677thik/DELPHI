from __future__ import annotations

from pathlib import Path

import pytest

from delphi.engine import CompiledProfile, compile_profile
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.probes.registry import CLASSIFIER_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
KB_DIR = REPO_ROOT / "kb"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def kb() -> KnowledgeBase:
    return load_and_validate(KB_DIR, CLASSIFIER_NAMES)


@pytest.fixture(scope="session")
def guardian(kb: KnowledgeBase) -> CompiledProfile:
    return compile_profile(kb, "GUARDIAN_PAYMENT")


@pytest.fixture(scope="session")
def navigator_hold(kb: KnowledgeBase) -> CompiledProfile:
    return compile_profile(kb, "CASE_PAYMENT_HOLD")
