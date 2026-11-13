"""The Guardian session store (docs/05 §2, #94 part 1): sessions and their probe runs.

A session is created per payment (``payment_id`` is unique) and pinned to the ACTIVE KB version
at that moment; the pin never changes. Probe runs are numbered per session in the order they
start. The state machine itself (passive probing, planning, the decision) comes with the rest
of #94; this module only reads and writes rows, in the caller's transaction, and relies on the
migration's constraints for the docs/05 §2 invariants.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.kb.store import active_version_id
from delphi.platform.clock import Clock
from delphi.platform.errors import DelphiError, NotFoundError, ProblemCode
from delphi.platform.ids import uuid7


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    PASSIVE_PROBING = "PASSIVE_PROBING"
    PLANNING = "PLANNING"
    AWAITING_CUSTOMER = "AWAITING_CUSTOMER"
    DECIDED = "DECIDED"
    CANCELLED_BY_CUSTOMER = "CANCELLED_BY_CUSTOMER"


class TerminalReason(StrEnum):
    STOP = "STOP"
    FAST_PATH = "FAST_PATH"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    SESSION_TIMEOUT = "SESSION_TIMEOUT"
    ENGINE_ERROR = "ENGINE_ERROR"
    CUSTOMER_CANCELLED = "CUSTOMER_CANCELLED"


class SessionMode(StrEnum):
    LIVE = "LIVE"
    AUTOPLAY = "AUTOPLAY"
    EVAL = "EVAL"


class Runner(StrEnum):
    API = "API"
    WORKER = "WORKER"


class ProbeMode(StrEnum):
    PASSIVE = "PASSIVE"
    ACTIVE = "ACTIVE"


class ProbeRunStatus(StrEnum):
    PENDING = "PENDING"
    AWAITING_CUSTOMER = "AWAITING_CUSTOMER"
    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"
    ERROR = "ERROR"


class SelectedBy(StrEnum):
    GREEDY = "GREEDY"
    EXPLORE = "EXPLORE"


@dataclass(frozen=True, slots=True)
class GuardianSessionRecord:
    id: UUID
    payment_id: UUID
    status: SessionStatus
    decision: str | None
    terminal_reason: TerminalReason | None
    kb_version_id: int
    posterior: Mapping[str, float] | None
    unexplained: bool
    mode: SessionMode
    runner: Runner
    rng_seed: int
    started_at: datetime
    decided_at: datetime | None
    case_id: UUID | None
    version: int

    @staticmethod
    def of(row: Any) -> GuardianSessionRecord:
        return GuardianSessionRecord(
            id=row.id,
            payment_id=row.payment_id,
            status=SessionStatus(row.status),
            decision=row.decision,
            terminal_reason=TerminalReason(row.terminal_reason) if row.terminal_reason else None,
            kb_version_id=row.kb_version_id,
            posterior=row.posterior,
            unexplained=row.unexplained,
            mode=SessionMode(row.mode),
            runner=Runner(row.runner),
            rng_seed=row.rng_seed,
            started_at=row.started_at,
            decided_at=row.decided_at,
            case_id=row.case_id,
            version=row.version,
        )


@dataclass(frozen=True, slots=True)
class ProbeRunRecord:
    id: UUID
    session_id: UUID
    seq: int
    probe_key: str
    mode: ProbeMode
    status: ProbeRunStatus
    outcome: str | None
    started_at: datetime
    completed_at: datetime | None

    @staticmethod
    def of(row: Any) -> ProbeRunRecord:
        return ProbeRunRecord(
            id=row.id,
            session_id=row.session_id,
            seq=row.seq,
            probe_key=row.probe_key,
            mode=ProbeMode(row.mode),
            status=ProbeRunStatus(row.status),
            outcome=row.outcome,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )


class SessionExistsError(DelphiError):
    """The payment already has a Guardian session (one per payment, docs/05 §2)."""

    code = ProblemCode.INVALID_STATE_TRANSITION


def _id(clock: Clock) -> UUID:
    return uuid7(unix_ms=int(clock.now().timestamp() * 1000))


def create_session(
    session: Session,
    clock: Clock,
    *,
    payment_id: UUID,
    mode: SessionMode,
    runner: Runner,
    rng_seed: int,
) -> GuardianSessionRecord:
    """A CREATED session for ``payment_id``, pinned to the ACTIVE KB version (docs/08 §2 step 1)."""
    kb_version_id = active_version_id(session)
    if kb_version_id is None:
        raise DelphiError("No ACTIVE KB version.", code=ProblemCode.NOT_INITIALISED)
    session_id = _id(clock)
    try:
        with session.begin_nested():
            session.execute(
                guardian_sessions.insert().values(
                    id=session_id,
                    payment_id=payment_id,
                    status=SessionStatus.CREATED.value,
                    kb_version_id=kb_version_id,
                    unexplained=False,
                    mode=mode.value,
                    runner=runner.value,
                    rng_seed=rng_seed,
                    started_at=clock.now(),
                    dd_pack_complete=False,
                    version=1,
                )
            )
    except IntegrityError as exc:
        if session_for_payment(session, payment_id) is not None:
            raise SessionExistsError(f"Payment {payment_id} already has a session.") from None
        raise exc
    record = get_session(session, session_id)
    assert record is not None
    return record


def get_session(conn: Connection | Session, session_id: UUID) -> GuardianSessionRecord | None:
    row = conn.execute(
        select(guardian_sessions).where(guardian_sessions.c.id == session_id)
    ).one_or_none()
    return GuardianSessionRecord.of(row) if row is not None else None


def session_for_payment(
    conn: Connection | Session, payment_id: UUID
) -> GuardianSessionRecord | None:
    row = conn.execute(
        select(guardian_sessions).where(guardian_sessions.c.payment_id == payment_id)
    ).one_or_none()
    return GuardianSessionRecord.of(row) if row is not None else None


def add_probe_run(
    session: Session,
    clock: Clock,
    session_id: UUID,
    *,
    probe_key: str,
    mode: ProbeMode,
    status: ProbeRunStatus = ProbeRunStatus.PENDING,
    selected_by: SelectedBy | None = None,
    propensity: float | None = None,
) -> ProbeRunRecord:
    """The session's next probe run (``seq`` = the last one's + 1), in the caller's transaction.
    The session row is locked, so two runs never get the same number."""
    locked = session.execute(
        select(guardian_sessions.c.id).where(guardian_sessions.c.id == session_id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise NotFoundError(f"No Guardian session {session_id}.")
    last = session.execute(
        select(func.max(probe_runs.c.seq)).where(probe_runs.c.session_id == session_id)
    ).scalar_one()
    run_id = _id(clock)
    session.execute(
        probe_runs.insert().values(
            id=run_id,
            session_id=session_id,
            seq=(last or 0) + 1,
            probe_key=probe_key,
            mode=mode.value,
            status=status.value,
            selected_by=selected_by.value if selected_by is not None else None,
            propensity=propensity,
            started_at=clock.now(),
        )
    )
    row = session.execute(select(probe_runs).where(probe_runs.c.id == run_id)).one()
    return ProbeRunRecord.of(row)


def list_probe_runs(conn: Connection | Session, session_id: UUID) -> list[ProbeRunRecord]:
    rows = conn.execute(
        select(probe_runs).where(probe_runs.c.session_id == session_id).order_by(probe_runs.c.seq)
    ).all()
    return [ProbeRunRecord.of(r) for r in rows]
