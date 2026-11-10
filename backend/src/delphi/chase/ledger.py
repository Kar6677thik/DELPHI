"""What Chase can see of the payment network (docs/10 §2): the ledger of every bank up to
``now − visibility_lag``, read through the `bank` tables `delphi.sim.api` re-exports. Never
`sim_truth` (FR-CHS-01).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, select

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


def load_trace_inputs(
    conn: Connection, source_payment_ids: Sequence[UUID], *, until: datetime
) -> tuple[list[SourcePayment], list[LedgerMove]]:
    """The executed source payments, and every ledger entry from the earliest of them up to
    ``until``. Sources that were not executed (held, declined) carry no money and are left out."""
    p, entries = bank.payments, bank.ledger_entries
    rows = conn.execute(
        select(p.c.id, p.c.payee_account_id, p.c.amount_paise, p.c.executed_at).where(
            p.c.id.in_(source_payment_ids), p.c.status == EXECUTED, p.c.executed_at < until
        )
    ).all()
    sources = [SourcePayment(str(r.id), str(r.payee_account_id), r.amount_paise) for r in rows]
    if not rows:
        return [], []
    since = min(r.executed_at for r in rows)
    moves = conn.execute(
        select(
            entries.c.id,
            entries.c.at,
            entries.c.payment_id,
            entries.c.account_id,
            entries.c.delta_paise,
            entries.c.balance_after_paise,
            p.c.channel,
        )
        .join(p, p.c.id == entries.c.payment_id)
        .where(entries.c.at >= since, entries.c.at < until)
        .order_by(entries.c.at, entries.c.id)
    ).all()
    return sources, [
        LedgerMove(
            entry_id=m.id,
            at_us=_us(m.at),
            payment_id=str(m.payment_id),
            account_id=str(m.account_id),
            delta_paise=m.delta_paise,
            balance_after_paise=m.balance_after_paise,
            channel=m.channel,
        )
        for m in moves
    ]
