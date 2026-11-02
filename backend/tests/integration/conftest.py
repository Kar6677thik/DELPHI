"""Integration tests run against a real PostgreSQL server (docs/21 §1).

``DELPHI_TEST_DATABASE_URL`` points at a server where the user may create databases and roles
(CI: the ``postgres:16`` service container; locally: ``docker compose up -d postgres``, or any
disposable server). Each test session creates its own uniquely named databases and drops them
afterwards. Never point it at a database you care about.

Without the variable the tests are skipped, unless ``DELPHI_REQUIRE_INTEGRATION=1`` (set in CI),
which turns the skip into a failure so CI can never pass by silently skipping.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, make_url, text

from delphi.platform.settings import sqlalchemy_url

from ._db import engine_as, upgrade

ADMIN_URL_ENV = "DELPHI_TEST_DATABASE_URL"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in Path(item.path).parents:
            item.add_marker(pytest.mark.integration)


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
