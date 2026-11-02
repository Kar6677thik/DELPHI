"""Database access (docs/17).

SQLAlchemy 2 with the psycopg 3 driver. Engines are created by the composition root and passed
explicitly; there is no module-level engine. Every connection runs in UTC.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.settings import sqlalchemy_url


def make_engine(
    url: str, *, pool_size: int = 5, connect_timeout_s: int = 5, application_name: str = "delphi"
) -> Engine:
    return create_engine(
        sqlalchemy_url(url),
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": connect_timeout_s,
            "application_name": application_name,
            "options": "-c timezone=UTC",
        },
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any exception."""
    with factory.begin() as session:
        yield session


def ping(engine: Engine, timeout_ms: int) -> None:
    """Raise if the database does not answer ``SELECT 1`` within ``timeout_ms``."""
    with engine.connect() as conn:
        conn.execute(
            text("SELECT set_config('statement_timeout', :t, true)"), {"t": str(timeout_ms)}
        )
        conn.execute(text("SELECT 1"))


def alembic_config(migrations_dir: Path, url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    # ConfigParser interpolation treats % specially (it appears in URL-encoded passwords).
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(url).replace("%", "%%"))
    return cfg


def migration_head(migrations_dir: Path) -> str | None:
    script = ScriptDirectory.from_config(alembic_config(migrations_dir, "postgresql://"))
    return script.get_current_head()


def current_revision(conn: Connection) -> str | None:
    heads = MigrationContext.configure(conn).get_current_heads()
    return heads[0] if len(heads) == 1 else None
