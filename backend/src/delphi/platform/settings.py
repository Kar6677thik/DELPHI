"""Process configuration (docs/04 §6, docs/23 §4).

Every setting comes from an environment variable prefixed ``DELPHI_``. Settings are validated
once at start-up; an invalid or missing value stops the process with a precise message
(fail fast, docs/24 §1).
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from delphi.platform.jobs import QueueConfig

# src/delphi/platform/settings.py → backend/
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_DIR = _BACKEND_DIR.parent
_POSTGRES_SCHEMES = ("postgresql://", "postgresql+psycopg://")

Port = Annotated[int, Field(ge=1, le=65535)]


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    DEMO = "demo"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class SettingsError(Exception):
    """Raised when the environment does not describe a runnable process."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DELPHI_", extra="ignore", frozen=True)

    env: Environment = Environment.DEV
    # The git SHA of the build; the image sets it (docs/23 §2). Returned by /health.
    version: str = "dev"
    log_level: LogLevel = LogLevel.INFO

    # The api and the worker connect as `delphi_app` (docs/17 §2).
    database_url: str
    # The worker's second pool, as `delphi_sim`, for the sim and evaluation code paths.
    # Only the worker needs it; the api never reads `sim_truth` (docs/04 §5.1).
    sim_database_url: str | None = None
    # `delphi migrate` connects as the schema owner, which can create roles and tables.
    # Defaults to `database_url` (local development with a single superuser).
    migrate_database_url: str | None = None
    migrations_dir: Path = _BACKEND_DIR / "migrations"
    # The built frontend (`pnpm build` output), served by the api at `/` (docs/18, ADR-0008).
    # The image sets /app/static (docs/23 §2). If it holds no index.html, nothing is served.
    static_dir: Path = _REPO_DIR / "frontend" / "dist"

    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_connect_timeout_s: int = Field(default=5, ge=1, le=60)
    # Readiness DB ping budget (docs/20 §3).
    ready_db_timeout_ms: int = Field(default=500, ge=50, le=10_000)

    # Job queue and worker (docs/04 §5.4, docs/20 §3).
    job_lease_s: int = Field(default=60, ge=5, le=3600)
    job_retry_backoff_s: int = Field(default=30, ge=1, le=3600)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_default_concurrency: int = Field(default=2, ge=1, le=8)
    worker_fast_concurrency: int = Field(default=1, ge=1, le=4)
    worker_poll_interval_s: float = Field(default=1.0, gt=0, le=60)
    worker_heartbeat_max_age_s: int = Field(default=30, ge=5, le=600)
    # Retention (docs/17 §6).
    outbox_retention_h: int = Field(default=24, ge=1, le=24 * 30)
    idempotency_retention_h: int = Field(default=24, ge=1, le=24 * 30)

    http_host: str = "0.0.0.0"  # nosec B104: binds inside the pod only (docs/23 §4)
    http_port: Port = 8000
    metrics_port: Port = 9000

    @field_validator("database_url", "sim_database_url", "migrate_database_url")
    @classmethod
    def _postgres_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(_POSTGRES_SCHEMES):
            msg = "must be a PostgreSQL URL (postgresql://user:password@host:port/db)"
            raise ValueError(msg)
        return value

    @property
    def queue_config(self) -> QueueConfig:
        return QueueConfig(
            lease=timedelta(seconds=self.job_lease_s),
            retry_backoff=timedelta(seconds=self.job_retry_backoff_s),
            default_max_attempts=self.job_max_attempts,
        )

    @property
    def outbox_retention(self) -> timedelta:
        return timedelta(hours=self.outbox_retention_h)

    @property
    def idempotency_retention(self) -> timedelta:
        return timedelta(hours=self.idempotency_retention_h)

    @property
    def effective_migrate_url(self) -> str:
        return self.migrate_database_url or self.database_url


def load_settings() -> Settings:
    """Read and validate settings from the environment, or raise ``SettingsError``."""
    try:
        return Settings()  # type: ignore[call-arg]  # required fields come from the environment
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "settings"
            problems.append(f"DELPHI_{field.upper()}: {error['msg']}")
        raise SettingsError("invalid configuration:\n  " + "\n  ".join(problems)) from None


def sqlalchemy_url(url: str) -> str:
    """Use the psycopg 3 driver whatever scheme the operator wrote."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def libpq_url(url: str) -> str:
    """The plain libpq form (``postgresql://``) for drivers used without SQLAlchemy."""
    return "postgresql://" + url.split("://", 1)[1]
