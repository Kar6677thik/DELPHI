"""Alembic environment (docs/17 §1). Online migrations only; every migration is hand-written."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool, text

from delphi.platform.settings import load_settings, sqlalchemy_url

# A migration waits at most this long for a table lock, so it cannot stall live traffic behind
# a long queue (expand/contract, docs/17 §1). On timeout it fails and the Job retries.
LOCK_TIMEOUT = "5s"


def _url() -> str:
    configured = context.config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return sqlalchemy_url(load_settings().effective_migrate_url)


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool,
                           connect_args={"options": "-c timezone=UTC"})
    with engine.connect() as connection:
        connection.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        connection.commit()
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (--sql) migrations are not supported")
run_migrations_online()
