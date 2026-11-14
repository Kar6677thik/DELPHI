"""Integration tests run against a real PostgreSQL server (docs/21 §1).

``DELPHI_TEST_DATABASE_URL`` points at a server where the user may create databases and roles
(CI: the ``postgres:16`` service container; locally: ``docker compose up -d postgres``, or any
disposable server). Each test session creates its own uniquely named databases and drops them
afterwards. Never point it at a database you care about.

Without the variable the tests are skipped, unless ``DELPHI_REQUIRE_INTEGRATION=1`` (set in CI),
which turns the skip into a failure so CI can never pass by silently skipping. The database
fixtures themselves (``admin_url``, ``create_database``) are in ``tests/conftest.py``, so the
golden tests can train on a real bank too (#36).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from ._db import engine_as, upgrade


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in Path(item.path).parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def migrated_url(create_database: Callable[[], str]) -> str:
    """A database at the migration head, shared by tests that only read the schema."""
    url = create_database()
    upgrade(url)
    return url


@pytest.fixture
def owner_engine(migrated_url: str) -> Iterator[Engine]:
    engine = engine_as(migrated_url)
    yield engine
    engine.dispose()
