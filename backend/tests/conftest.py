from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text

from delphi.engine import CompiledProfile, compile_profile
from delphi.kb.loader import load_and_validate
from delphi.kb.model import KnowledgeBase
from delphi.platform.settings import sqlalchemy_url
from delphi.probes.registry import CLASSIFIER_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
KB_DIR = REPO_ROOT / "kb"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUN_SLOW_ENV = "DELPHI_RUN_SLOW"
# A PostgreSQL server for integration and golden tests (see tests/integration/conftest.py).
ADMIN_URL_ENV = "DELPHI_TEST_DATABASE_URL"


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


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get(ADMIN_URL_ENV)
    if not url:
        if os.environ.get("DELPHI_REQUIRE_INTEGRATION") == "1":
            pytest.fail(f"{ADMIN_URL_ENV} is not set but DELPHI_REQUIRE_INTEGRATION=1")
        pytest.skip(f"{ADMIN_URL_ENV} is not set")
    return sqlalchemy_url(url)


@pytest.fixture(scope="session")
def create_database(admin_url: str) -> Iterator[Callable[[], str]]:
    """Factory for empty, uniquely named databases; all are dropped at the end of the session."""
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    created: list[str] = []

    def make() -> str:
        name = f"delphi_test_{secrets.token_hex(4)}"
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return make_url(admin_url).set(database=name).render_as_string(hide_password=False)

    yield make
    with admin.connect() as conn:
        for name in created:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()
