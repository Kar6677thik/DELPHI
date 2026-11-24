"""Read models of Guardian sessions for the API and the monitor (docs/16 "Guardian", docs/08 §6).

Plain reads over `guardian_sessions` and `probe_runs` (and the payment they decide), newest
first, keyset-paged on ``(started_at, id)``. Nothing here changes a session.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Connection,
    Float,
    Select,
    Text,
    and_,
    cast,
    column,
    func,
    literal,
    null,
    or_,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from delphi.guardian.tables import guardian_sessions, probe_runs
from delphi.sim import api as bank


@dataclass(frozen=True, slots=True)
class SessionCursor:
    started_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class SessionFilter:
    status: str | None = None
    decision: str | None = None
    unexplained: bool | None = None
    mode: str | None = None
    started_from: datetime | None = None  # inclusive
    started_to: datetime | None = None  # exclusive


@dataclass(frozen=True, slots=True)
class SessionRow:
    """One session with everything the monitor shows (the row's columns as stored)."""

    id: UUID
    payment: bank.PaymentView
    status: str
    decision: str | None
    terminal_reason: str | None
    kb_version_id: int
    prior: Mapping[str, float] | None
    p_model: Mapping[str, float] | None
    anomaly_percentile: float | None
    open_mass: float | None
    top_features: Sequence[Mapping[str, Any]]
    posterior: Mapping[str, float] | None
    risk_by_decision: Mapping[str, float] | None
    unexplained: bool
    mode: str
    started_at: datetime
    decided_at: datetime | None
    case_id: UUID | None
    baselines: Mapping[str, str] | None
    dd_pack_complete: bool
    version: int


def _row(row: Any, payment: bank.PaymentView) -> SessionRow:
    return SessionRow(
        id=row.id,
        payment=payment,
        status=row.status,
        decision=row.decision,
        terminal_reason=row.terminal_reason,
        kb_version_id=row.kb_version_id,
        prior=row.prior,
        p_model=row.p_model,
        anomaly_percentile=row.anomaly_percentile,
        open_mass=row.open_mass,
        top_features=row.top_features or (),
        posterior=row.posterior,
        risk_by_decision=row.risk_by_decision,
        unexplained=row.unexplained,
        mode=row.mode,
        started_at=row.started_at,
        decided_at=row.decided_at,
        case_id=row.case_id,
        baselines=row.baseline_decisions,
        dd_pack_complete=row.dd_pack_complete,
        version=row.version,
    )


def list_sessions(
    conn: Connection | Session,
    *,
    limit: int,
    after: SessionCursor | None = None,
    where: SessionFilter = SessionFilter(),  # noqa: B008 - frozen, so a shared default is safe
) -> list[SessionRow]:
    """Newest first. ``after`` continues a page (strictly older than the cursor)."""
    gs = guardian_sessions.c
    query = select(guardian_sessions)
    if where.status is not None:
        query = query.where(gs.status == where.status)
    if where.decision is not None:
        query = query.where(gs.decision == where.decision)
    if where.mode is not None:
        query = query.where(gs.mode == where.mode)
    if where.unexplained is not None:
        query = query.where(gs.unexplained.is_(where.unexplained))
    if where.started_from is not None:
        query = query.where(gs.started_at >= where.started_from)
    if where.started_to is not None:
        query = query.where(gs.started_at < where.started_to)
    if after is not None:
        query = query.where(
            or_(
                gs.started_at < after.started_at,
                and_(gs.started_at == after.started_at, gs.id < after.id),
            )
        )
    rows = conn.execute(query.order_by(gs.started_at.desc(), gs.id.desc()).limit(limit)).all()
    payments = bank.get_payments(conn, [r.payment_id for r in rows])
    return [_row(r, payments[r.payment_id]) for r in rows if r.payment_id in payments]


def get_session_row(conn: Connection | Session, session_id: UUID) -> SessionRow | None:
    row = conn.execute(
        select(guardian_sessions).where(guardian_sessions.c.id == session_id)
    ).one_or_none()
    if row is None:
        return None
    payment = bank.get_payment(conn, row.payment_id)
    return None if payment is None else _row(row, payment)


def probe_run_rows(conn: Connection | Session, session_id: UUID) -> list[Any]:
    """Every probe run of the session in order, with all its stored columns."""
    pr = probe_runs.c
    return list(
        conn.execute(select(probe_runs).where(pr.session_id == session_id).order_by(pr.seq))
    )


def unexplained_sessions_query(since: datetime | None = None) -> Select[Any]:
    """docs/11 §1, the Scout's queue source: decided sessions whose terminal posterior was
    flagged unexplained, as ``(subject_type, subject_id, entered_at, p_unexplained,
    payment_id, account_id)``. A query, so the Scout bounds and pages it in SQL; it uses the
    partial index on ``decided_at WHERE unexplained`` (0017)."""
    gs = guardian_sessions.c
    query = select(
        literal("GUARDIAN_SESSION").label("subject_type"),
        gs.id.label("subject_id"),
        gs.decided_at.label("entered_at"),
        func.coalesce(gs.posterior["UNEXPLAINED"].as_float(), 0.0).label("p_unexplained"),
        gs.payment_id.label("payment_id"),
        null().cast(PG_UUID(as_uuid=True)).label("account_id"),
    ).where(gs.unexplained.is_(True), gs.decided_at.is_not(None))
    if since is not None:
        query = query.where(gs.decided_at >= since)
    return query


def attributed_sessions_query(since: datetime | None = None) -> Select[Any]:
    """docs/11 §2, the Scout's reference set: decided sessions attributed to a known typology
    (the posterior's argmax, taken in SQL, is neither GENUINE nor UNEXPLAINED, and the session
    is not flagged unexplained), with the queue source's columns. The caller bounds it."""
    gs = guardian_sessions.c
    h = (
        func.jsonb_each_text(gs.posterior)
        .table_valued(column("key", Text), column("value", Text))
        .render_derived(name="h")
    )
    top = select(h.c.key).order_by(cast(h.c.value, Float).desc(), h.c.key).limit(1).lateral("top")
    query = (
        select(
            literal("GUARDIAN_SESSION").label("subject_type"),
            gs.id.label("subject_id"),
            gs.decided_at.label("entered_at"),
            func.coalesce(gs.posterior["UNEXPLAINED"].as_float(), 0.0).label("p_unexplained"),
            gs.payment_id.label("payment_id"),
            null().cast(PG_UUID(as_uuid=True)).label("account_id"),
        )
        .select_from(guardian_sessions.join(top, true()))
        .where(
            gs.unexplained.is_(False),
            gs.decided_at.is_not(None),
            gs.posterior.is_not(None),
            top.c.key.not_in(("GENUINE", "UNEXPLAINED")),
        )
    )
    if since is not None:
        query = query.where(gs.decided_at >= since)
    return query


def observed_outcomes(conn: Connection | Session, session_id: UUID) -> dict[str, str]:
    """The session's completed probe outcomes, source → outcome (docs/11 §2)."""
    pr = probe_runs.c
    rows = conn.execute(
        select(pr.probe_key, pr.outcome).where(pr.session_id == session_id, pr.outcome.is_not(None))
    )
    return {r.probe_key: r.outcome for r in rows}
