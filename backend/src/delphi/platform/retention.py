"""The RETENTION_SWEEP job (docs/17 §6): delete platform rows past their retention.

Covered here: outbox events and idempotency keys (24 h), expired auth sessions, and the request
and response bodies of LLM calls after 30 days (the call rows themselves are kept).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import CursorResult, Delete, Update, delete, null, update
from sqlalchemy.orm import Session, sessionmaker

from delphi.platform.clock import Clock
from delphi.platform.jobs import JobContext
from delphi.platform.tables import auth_sessions, idempotency_keys, llm_calls, outbox_events


@dataclass(frozen=True, slots=True)
class RetentionSweep:
    sessions: sessionmaker[Session]
    clock: Clock
    outbox_retention: timedelta
    idempotency_retention: timedelta
    llm_body_retention: timedelta = timedelta(days=30)  # docs/17 §6

    def __call__(self, _ctx: JobContext) -> Mapping[str, Any]:
        """Deleting by age is idempotent: a re-run deletes nothing new."""
        now = self.clock.now()

        def deleted(session: Session, statement: Delete | Update) -> int:
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
            bodies = deleted(
                session,
                update(llm_calls)
                .where(
                    llm_calls.c.created_at < now - self.llm_body_retention,
                    llm_calls.c.request_redacted.is_not(None) | llm_calls.c.response.is_not(None),
                )
                .values(request_redacted=null(), response=null()),  # SQL NULL, not JSON null
            )
        return {
            "outbox_events": outbox,
            "idempotency_keys": keys,
            "auth_sessions": sessions,
            "llm_call_bodies": bodies,
        }
