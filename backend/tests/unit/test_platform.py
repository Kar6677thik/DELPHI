"""Platform building blocks: settings, ids, clock, logging (docs/04 §6, docs/20 §1)."""

from __future__ import annotations

import io
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from delphi.cli import EXIT_CONFIG_ERROR, main
from delphi.platform.clock import FixedClock
from delphi.platform.errors import STATUS_BY_CODE, NotFoundError, ProblemCode
from delphi.platform.ids import uuid7, uuid7_unix_ms
from delphi.platform.logging import (
    REDACTED,
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)
from delphi.platform.settings import Settings, SettingsError, load_settings, sqlalchemy_url

PG = "postgresql://u:p@localhost:5432/delphi"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    import os

    for key in list(os.environ):
        if key.startswith("DELPHI_"):
            monkeypatch.delenv(key)
    return monkeypatch


# ------------------------------------------------------------------ settings


def test_missing_database_url_fails_fast_naming_the_variable(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(SettingsError, match="DELPHI_DATABASE_URL"):
        load_settings()


def test_non_postgres_url_is_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DELPHI_DATABASE_URL", "sqlite:///x.db")
    with pytest.raises(SettingsError, match="PostgreSQL URL"):
        load_settings()


def test_invalid_enum_is_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DELPHI_DATABASE_URL", PG)
    clean_env.setenv("DELPHI_ENV", "prod")
    with pytest.raises(SettingsError, match="DELPHI_ENV"):
        load_settings()


def test_settings_from_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DELPHI_DATABASE_URL", PG)
    clean_env.setenv("DELPHI_VERSION", "0123abc")
    settings = load_settings()
    assert settings.version == "0123abc"
    assert settings.effective_migrate_url == PG  # falls back to the app URL
    assert settings.migrations_dir.is_dir()


def test_migrate_url_overrides_app_url() -> None:
    other = "postgresql://owner:x@db/delphi"
    settings = Settings(database_url=PG, migrate_database_url=other)
    assert settings.effective_migrate_url == other


def test_sqlalchemy_url_selects_psycopg3() -> None:
    assert sqlalchemy_url(PG) == "postgresql+psycopg://u:p@localhost:5432/delphi"
    already = "postgresql+psycopg://u@h/d"
    assert sqlalchemy_url(already) == already


def test_cli_exits_2_on_bad_configuration(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["migrate"]) == EXIT_CONFIG_ERROR
    assert "DELPHI_DATABASE_URL" in capsys.readouterr().err


def test_cli_rejects_missing_migrations_dir(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv("DELPHI_DATABASE_URL", PG)
    clean_env.setenv("DELPHI_MIGRATIONS_DIR", "does/not/exist")
    assert main(["migrate"]) == EXIT_CONFIG_ERROR
    assert "DELPHI_MIGRATIONS_DIR" in capsys.readouterr().err


# ------------------------------------------------------------------ ids


def test_uuid7_layout() -> None:
    value = uuid7(unix_ms=1_700_000_000_123)
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert uuid7_unix_ms(value) == 1_700_000_000_123


def test_uuid7_sorts_by_millisecond() -> None:
    ids = [uuid7(unix_ms=ms) for ms in (5, 1, 3, 2, 4)]
    assert [uuid7_unix_ms(v) for v in sorted(ids)] == [1, 2, 3, 4, 5]


def test_uuid7_is_unique_within_a_millisecond() -> None:
    assert len({uuid7(unix_ms=42) for _ in range(1000)}) == 1000


@pytest.mark.parametrize("ms", [-1, 1 << 48])
def test_uuid7_rejects_out_of_range_timestamps(ms: int) -> None:
    with pytest.raises(ValueError, match="48 bits"):
        uuid7(unix_ms=ms)


# ------------------------------------------------------------------ clock


def test_fixed_clock_advances_forward_only() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FixedClock(start)
    clock.advance(timedelta(minutes=5))
    assert clock.now() == start + timedelta(minutes=5)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))


def test_fixed_clock_requires_utc() -> None:
    with pytest.raises(ValueError, match="UTC"):
        FixedClock(datetime(2026, 1, 1))


# ------------------------------------------------------------------ errors


def test_every_problem_code_has_a_status() -> None:
    assert set(STATUS_BY_CODE) == set(ProblemCode)
    assert NotFoundError("x").status == 404


# ------------------------------------------------------------------ logging


def test_logs_are_json_lines_with_context_and_redaction() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    try:
        bind_context(request_id="r-1")
        get_logger("test").info(
            "user.login",
            username="kamala",
            password="hunter2",
            headers={"Authorization": "Bearer x", "accept": "json"},
        )
        get_logger("test").debug("hidden")  # below the configured level
    finally:
        clear_context()
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["event"] == "user.login"
    assert entry["request_id"] == "r-1"
    assert entry["level"] == "info"
    assert entry["password"] == REDACTED
    assert entry["headers"] == {"Authorization": REDACTED, "accept": "json"}
    assert entry["ts"].endswith("Z")


# ------------------------------------------------------------------ worker shell


def test_worker_stops_on_request_and_releases_the_pool() -> None:
    import threading

    from delphi.platform.wiring import build_platform
    from delphi.platform.worker import run_worker

    settings = Settings(
        database_url="postgresql://nobody:x@127.0.0.1:1/none", worker_poll_interval_s=0.01
    )
    platform = build_platform(settings, process="worker")
    stop = threading.Event()
    timer = threading.Timer(0.05, stop.set)
    timer.start()
    run_worker(platform, stop, serve_health=False)  # returns once stop is set
    assert stop.is_set()
