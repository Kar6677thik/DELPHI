"""What Chase can see of the payment network (docs/10 §2): the ledger of every bank up to
``now − visibility_lag``, read through the `bank` tables `delphi.sim.api` re-exports. Never
`sim_truth` (FR-CHS-01).

The ledger is read **outwards from the frontier**, not scanned: only accounts the taint can
have reached matter. Each source's payee is reached when the source executes. Each round reads
the reached accounts' entries from the time they were reached (index ``(account_id, at)``),
then the credits of the payments they made (index ``payment_id``). Each payee becomes reached
at that credit's time, and the next round reads it. The rounds stop when nothing new is reached.
A cash-out payment's payee is not followed: its money left the banking system.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, select

from delphi.chase.tracer import LedgerMove, SourcePayment
from delphi.sim import api as bank

# The channel on which a mule withdraws cash: its taint leaves the banking system (docs/10 §3).
CASH_OUT_CHANNEL = "CASH_OUT"
EXECUTED = "EXECUTED"


def visible_until(now: datetime, lag_minutes: float) -> datetime:
    """docs/10 §2: Chase sees the network up to ``now − visibility_lag``."""
    return now - timedelta(minutes=lag_minutes)


def _us(at: datetime) -> int:
    return (at - datetime(1970, 1, 1, tzinfo=at.tzinfo)) // timedelta(microseconds=1)


def _entries() -> Any:
    e, p = bank.ledger_entries, bank.payments
    return select(
        e.c.id,
        e.c.at,
        e.c.payment_id,
        e.c.account_id,
        e.c.delta_paise,
        e.c.balance_after_paise,
        p.c.channel,
    ).join(p, p.c.id == e.c.payment_id)


def _move(r: Row[Any]) -> LedgerMove:
    return LedgerMove(
        entry_id=r.id,
        at_us=_us(r.at),
        payment_id=str(r.payment_id),
        account_id=str(r.account_id),
        delta_paise=r.delta_paise,
        balance_after_paise=r.balance_after_paise,
        channel=r.channel,
    )


def _earliest(reached: dict[UUID, datetime], found: Iterable[tuple[UUID, datetime]]) -> None:
    for account, at in found:
        if account not in reached or at < reached[account]:
            reached[account] = at


def load_trace_inputs(
    conn: Connection, source_payment_ids: Sequence[UUID], *, until: datetime
) -> tuple[list[SourcePayment], list[LedgerMove]]:
    """The executed source payments, and the visible ledger entries the taint can have touched.
    Sources that were not executed (held, declined) carry no money and are left out."""
    p, e = bank.payments, bank.ledger_entries
    rows = conn.execute(
        select(p.c.id, p.c.payee_account_id, p.c.amount_paise, p.c.executed_at).where(
            p.c.id.in_(source_payment_ids), p.c.status == EXECUTED, p.c.executed_at < until
        )
    ).all()
    sources = [SourcePayment(str(r.id), str(r.payee_account_id), r.amount_paise) for r in rows]
    if not rows:
        return [], []
    reached: dict[UUID, datetime] = {}
    _earliest(reached, ((r.payee_account_id, r.executed_at) for r in rows))
    frontier = dict(reached)
    moves: dict[int, LedgerMove] = {}
    while frontier:
        since = min(frontier.values())
        found = conn.execute(
            _entries().where(e.c.account_id.in_(list(frontier)), e.c.at >= since, e.c.at < until)
        ).all()
        paid: set[UUID] = set()
        for r in found:
            if r.at < frontier[r.account_id]:
                continue  # before the taint could have arrived there
            moves[r.id] = _move(r)
            if r.delta_paise < 0 and r.channel != CASH_OUT_CHANNEL:
                paid.add(r.payment_id)
        credits = (
            conn.execute(
                _entries().where(
                    e.c.payment_id.in_(list(paid)), e.c.delta_paise > 0, e.c.at < until
                )
            ).all()
            if paid
            else []
        )
        before = dict(reached)
        for r in credits:
            moves[r.id] = _move(r)
        _earliest(reached, ((r.account_id, r.at) for r in credits))
        frontier = {a: t for a, t in reached.items() if a not in before or t < before[a]}
    return sources, sorted(moves.values(), key=lambda m: (m.at_us, m.entry_id))
