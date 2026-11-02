"""Helpers shared by the integration tests."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import Engine, create_engine

from delphi.platform.db import alembic_config

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(MIGRATIONS_DIR, url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(MIGRATIONS_DIR, url), revision)


def engine_as(url: str, role: str | None = None) -> Engine:
    """An engine whose sessions run as ``role`` (via ``SET ROLE`` at connect time), so grants
    are exercised without needing LOGIN roles with passwords."""
    options = "-c timezone=UTC" + (f" -c role={role}" if role else "")
    return create_engine(url, connect_args={"options": options})
