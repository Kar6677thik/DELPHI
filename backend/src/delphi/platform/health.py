"""Readiness checks (docs/20 §3).

Readiness covers what the process needs to serve traffic: the database answers and the schema
is at the migration head. The ACTIVE-KB check is added with the KB versions table (#15).
Dataset and model readiness are deliberately not readiness conditions, so the first deploy can
become Ready before seeding finishes (docs/23 §4).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from delphi.platform.db import current_revision, ping
from delphi.platform.logging import get_logger

log = get_logger(__name__)


class CheckStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: CheckStatus
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Readiness:
    checks: dict[str, CheckResult]

    @property
    def ready(self) -> bool:
        return all(c.status is CheckStatus.OK for c in self.checks.values())


def check_readiness(engine: Engine, expected_head: str | None, db_timeout_ms: int) -> Readiness:
    try:
        ping(engine, db_timeout_ms)
        with engine.connect() as conn:
            actual = current_revision(conn)
    except SQLAlchemyError as exc:
        # The exception text can carry the DSN; log only its type.
        log.warning("ready.database_unreachable", error=type(exc).__name__)
        failed = CheckResult(CheckStatus.FAILED, "database unreachable")
        return Readiness({"database": failed, "migrations": failed})

    if actual is not None and actual == expected_head:
        migrations = CheckResult(CheckStatus.OK)
    else:
        detail = f"schema at {actual or 'no revision'}, expected {expected_head}"
        migrations = CheckResult(CheckStatus.FAILED, detail)
    return Readiness({"database": CheckResult(CheckStatus.OK), "migrations": migrations})
