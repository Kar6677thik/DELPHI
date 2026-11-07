"""The RETENTION_SWEEP job (docs/17 §6): delete platform rows past their retention.

Covered here: outbox events and idempotency keys (24 h) and expired auth sessions. The
30-day nulling of LLM call bodies joins this job when `llm_calls` exists (M7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import CursorResult, Delete, delete
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import Clock
from delphi.platform.jobs import JobContext
from delphi.platform.tables import auth_sessions, idempotency_keys, outbox_events


@dataclass(frozen=True, slots=True)
class RetentionSweep:
    sessions: sessionmaker[Session]
    clock: Clock
    outbox_retention: timedelta
    idempotency_retention: timedelta

    def __call__(self, _ctx: JobContext) -> Mapping[str, Any]:
        """Deleting by age is idempotent: a re-run deletes nothing new."""
        now = self.clock.now()

        def deleted(session: Session, statement: Delete) -> int:
            result: CursorResult[Any] = session.execute(statement)  # type: ignore[assignment]
            return result.rowcount

        with self.sessions.begin() as session:
            outbox = deleted(
                session,
                delete(outbox_events).where(outbox_events.c.at < now - self.outbox_retention),
            )
            keys = deleted(
                session,
                delete(idempotency_keys).where(
                    idempotency_keys.c.created_at < now - self.idempotency_retention
                ),
            )
            sessions = deleted(
                session, delete(auth_sessions).where(auth_sessions.c.expires_at < now)
            )
        return {"outbox_events": outbox, "idempotency_keys": keys, "auth_sessions": sessions}
