"""The composition root (docs/04 §4.2): builds the process's shared resources from settings and
passes them explicitly. There are no hidden singletons."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import Clock, SystemClock
from delphi.platform.db import make_engine, make_session_factory, migration_head
from delphi.platform.settings import Settings, SettingsError


@dataclass(frozen=True, slots=True)
class Platform:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    clock: Clock
    started_at: datetime
    migration_head: str | None

    def close(self) -> None:
        self.engine.dispose()


def build_platform(settings: Settings, *, process: str, clock: Clock | None = None) -> Platform:
    if not settings.migrations_dir.is_dir():
        raise SettingsError(f"DELPHI_MIGRATIONS_DIR: {settings.migrations_dir} is not a directory")
    clock = clock or SystemClock()
    engine = make_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        connect_timeout_s=settings.db_connect_timeout_s,
        application_name=f"delphi-{process}",
    )
    return Platform(
        settings=settings,
        engine=engine,
        sessions=make_session_factory(engine),
        clock=clock,
        started_at=clock.now(),
        migration_head=migration_head(settings.migrations_dir),
    )
