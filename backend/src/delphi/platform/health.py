"""Readiness checks (docs/20 §3).

Readiness covers what the process needs to serve traffic: the database answers and the schema
is at the migration head, plus the checks feature modules pass in: from M1, an ACTIVE KB
version (``delphi.kb.store.kb_readiness``, #15). Dataset and model readiness are deliberately
not readiness conditions, so the first deploy can become Ready before seeding finishes
(docs/23 §4).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, Engine
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


ExtraCheck = Callable[[Connection], CheckResult]


def check_readiness(
    engine: Engine,
    expected_head: str | None,
    db_timeout_ms: int,
    extra: Mapping[str, ExtraCheck] | None = None,
) -> Readiness:
    """The database answers and the schema is at ``expected_head``. ``extra`` adds checks that
    feature modules own (the platform imports none of them), such as the ACTIVE KB; they run only
    once the schema is at head, because their tables may not exist before."""
    extra = extra or {}
    try:
        ping(engine, db_timeout_ms)
        with engine.connect() as conn:
            actual = current_revision(conn)
            at_head = actual is not None and actual == expected_head
            extras = {name: check(conn) for name, check in extra.items()} if at_head else {}
    except SQLAlchemyError as exc:
        # The exception text can carry the DSN; log only its type.
        log.warning("ready.database_unreachable", error=type(exc).__name__)
        failed = CheckResult(CheckStatus.FAILED, "database unreachable")
        return Readiness({"database": failed, "migrations": failed, **dict.fromkeys(extra, failed)})

    if at_head:
        migrations = CheckResult(CheckStatus.OK)
    else:
        detail = f"schema at {actual or 'no revision'}, expected {expected_head}"
        migrations = CheckResult(CheckStatus.FAILED, detail)
        skipped = CheckResult(CheckStatus.FAILED, "schema not at the migration head")
        extras = dict.fromkeys(extra, skipped)
    return Readiness({"database": CheckResult(CheckStatus.OK), "migrations": migrations, **extras})
